"""execute_sql 工具：在业务库执行 LLM 生成的 SQL → 全量结果旁路（result_id）→ 摘要回灌 LLM。

SQL 安全护栏：validate_sql 用 sqlglot 解析，只放行 SELECT，拦 DDL/DML；
_execute 加执行超时（30s）+ 行数上限（10000）。执行失败不抛异常，返回 error 摘要让 LLM 自愈重试。"""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.storage.models import MetadataColumn
from src.storage.pg_client import AsyncSessionFactory
from src.storage.query_results import save_result


def validate_sql(sql: str) -> str | None:
    """SQL 安全护栏：sqlglot 解析，只允许 SELECT（含 WITH...SELECT）；拦 DDL/DML。
    返回错误信息（拦截）或 None（放行）。"""
    import sqlglot
    from sqlglot import exp
    try:
        parsed = sqlglot.parse(sql)
    except Exception:
        return "SQL 语法解析失败（sqlglot 无法解析），请检查语法"
    for stmt in parsed:
        if stmt is None:
            continue
        # 只放行只读查询：SELECT / UNION / INTERSECT / EXCEPT（都是只读；CTE 顶层是 Select）
        # 拦 DDL/DML：CREATE/DROP/UPDATE/DELETE/INSERT/ALTER 等
        if not isinstance(stmt, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            return f"仅允许只读 SELECT 查询，拦截 {type(stmt).__name__} 操作"
    return None


MAX_ROWS = 10000   # 行数上限（防 LLM 生成无 LIMIT 大查询拖垮业务库）
EXEC_TIMEOUT = 30  # 单次执行超时（秒）


async def _execute(engine, sql: str) -> tuple[list, list]:
    """在业务库执行 SQL，返回 (columns, rows)。
    行数封顶 MAX_ROWS（fetchmany 避免大结果集打满内存）；执行超时 EXEC_TIMEOUT 秒。"""
    async def _run():
        async with engine.connect() as conn:
            result = await conn.execute(text(sql))
            columns = list(result.keys())
            rows = [dict(r._mapping) for r in result.fetchmany(MAX_ROWS)]
        return columns, rows
    return await asyncio.wait_for(_run(), EXEC_TIMEOUT)


def _preview(rows: list, n: int = 5) -> list:
    """前 n 行预览回灌 LLM，全量走 result_id 旁路。"""
    return rows[:n]


async def _sensitive_columns(datasource_id: int) -> set[str]:
    """读所有 role_tag='sensitive' 的字段名。
    ponytail: 全库级匹配（同名列一处标 sensitive 就全部过滤），不按 datasource/表细分；
    多源共用列名产生歧义时再换按 datasource_id+table 精确过滤。datasource_id 当前未用，留位。"""
    async with AsyncSessionFactory() as s:
        rows = (await s.execute(MetadataColumn.__table__.select().where(
            MetadataColumn.role_tag == "sensitive"))).all()
    return {r.column_name for r in rows}


async def execute_sql(args: dict, ctx: LoopContext,
                      cancel_token: CancelToken) -> ToolResult:
    """工具 handler。
    args: {sql, datasource_id?}。datasource_id 缺省取第一个数据源（单源场景）。
    失败不抛——错误信息回灌让 LLM 改 SQL 重试（自愈，spec 第 7 章）。"""
    from src.datasource.manager import DataSourceManager

    sql = args.get("sql", "").strip()
    if not sql:
        return ToolResult(summary="错误：未提供 SQL。")

    err = validate_sql(sql)
    if err:
        return ToolResult(summary=f"SQL 被拦截: {err}")

    mgr = DataSourceManager()
    ds_id = args.get("datasource_id")
    session_id = getattr(ctx, "session_id", "unknown")
    try:
        if ds_id is None:
            rows_ds = await mgr.list_datasources()
            if not rows_ds:
                return ToolResult(summary="错误：无可用数据源。")
            ds_id = rows_ds[0]["id"]
        engine = await mgr.get_engine(int(ds_id))
        columns, rows = await _execute(engine, sql)
    except Exception as e:
        # 自愈：不抛异常，错误信息回灌让 LLM 改 SQL 重试
        return ToolResult(summary=f"SQL 执行失败: {e}。请检查表名/字段/语法后重试。")

    # 全量结果先旁路（save_result 拿完整列，前端要完整数据）
    result_id = await save_result(session_id, columns, rows, datasource_id=int(ds_id))

    # 摘要过滤 sensitive 字段：role_tag=sensitive 的列不回灌 LLM（spec 第 7.5）
    sensitive = await _sensitive_columns(int(ds_id))
    if sensitive:
        columns = [c for c in columns if c not in sensitive]
        rows = [{k: v for k, v in r.items() if k not in sensitive} for r in rows]

    summary = {"result_id": result_id, "rows": len(rows), "columns": columns,
               "preview": _preview(rows)}
    return ToolResult(summary=json.dumps(summary, ensure_ascii=False, default=str))


EXECUTE_SQL = ToolDefinition(
    name="execute_sql",
    description=(
        "在业务库执行只读 SQL 查询，返回结果摘要（行数/列名/前 5 行预览）+ result_id。"
        "写 SQL 必须依据对话里已有的 query_metadata 结果（表名/字段类型/格式/表级规则）；"
        "上下文里没有该表元数据时再 query_metadata 查一次，已有就不必重复查。"
        "禁止凭印象猜字段格式——YYYY-MM 年月字符串字段用 = / IN / 范围，不得用 LIKE。"
        "只查业务数据（数值/统计/对比/明细/趋势）；查文档/政策/口径走 knowledge_search。"
        "\n"
        "【宽表多指标·一次查完，禁止逐列反复 SELECT】当问题涉及多个指标里找最值/排名/对比/"
        "表现最差最好（如「各省分公司哪个指标完成率最低」「哪个指标得分最差」），表是宽表"
        "（每个指标一列，如 swdl_score/xdl_score/issu_score…），必须用 CROSS JOIN 指标字典 + CASE "
        "把宽表列转行(unpivot)，一条 SQL 拉成一行一指标再排序取最值。示例："
        "SELECT t.主体, ind.指标, CASE ind.指标 WHEN 'A' THEN t.A列 WHEN 'B' THEN t.B列 END AS 值 "
        "FROM 宽表 t CROSS JOIN (VALUES ('A'),('B'),…) AS ind(指标) WHERE … ORDER BY 值。"
        "先调 get_sql_template 取「宽表列转行(unpivot)」完整样板按 usage 改。"
        "严禁对每个指标分别 SELECT 一遍——那会跑十几条查询、极慢且口径不一致。"
    ),
    parameters={"type": "object",
                "properties": {"sql": {"type": "string", "description": "要执行的只读 SQL"},
                               "datasource_id": {"type": "integer", "description": "数据源 ID（可选，缺省取第一个）"}},
                "required": ["sql"]},
    handler=execute_sql,
)
