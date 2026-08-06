"""execute_sql 工具：在业务库执行 LLM 生成的 SQL → 全量结果旁路（result_id）→ 摘要回灌 LLM。

SQL 安全护栏：validate_sql 用 sqlglot 解析，只放行 SELECT，拦 DDL/DML；
_execute 加执行超时（30s）+ 行数上限（10000）。执行失败不抛异常，返回 error 摘要让 LLM 自愈重试。"""
from __future__ import annotations

import asyncio
import json
import time

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


# ---- 执行前字段校验：根治"按规律猜字段名"幻觉（如以为每组指标都有 _score 列）----
_COL_CACHE: dict[tuple[int, str, str], tuple[float, set[str]]] = {}
_COL_CACHE_TTL = 300.0   # 列清单缓存 5 分钟（一次会话内同表不重复拉）


async def _real_columns(engine, datasource_id: int, schema: str | None, table: str) -> set[str] | None:
    """带 TTL 缓存地从业务库拉单表真实列名（information_schema）。拉失败返回 None（跳过校验，不阻塞执行）。"""
    key = (datasource_id, schema or "", table)
    now = time.monotonic()
    hit = _COL_CACHE.get(key)
    if hit and now - hit[0] < _COL_CACHE_TTL:
        return hit[1]
    try:
        from src.datasource.metadata_sync import fetch_table_columns
        cols = await fetch_table_columns(engine, table, schema)
        names = {c["name"] for c in cols}
    except Exception:
        return None   # best-effort：拉不到列就不校验，绝不阻塞正常执行
    _COL_CACHE[key] = (now, names)
    return names


async def _validate_columns(sql: str, engine, datasource_id: int) -> str | None:
    """执行前字段校验：解析 SQL 里【带表前缀】的 alias.col 引用，对照业务库真实列。
    发现不存在的列 → 返回精确错误（含该表真实列清单），让 LLM 照着一次改对，SQL 压根不打到数据库。
    None=放行。只校验能归属到 FROM 真实表的带前缀列（子查询别名/裸列不校验，避免误报）。"""
    import sqlglot
    from sqlglot import exp
    try:
        parsed = sqlglot.parse(sql)
    except Exception:
        return None
    alias_map: dict[str, tuple[str | None, str]] = {}
    tables: list[tuple[str | None, str]] = []
    for stmt in parsed:
        if stmt is None:
            continue
        for tb in stmt.find_all(exp.Table):
            schema = tb.db or None
            name = tb.name
            if not name:
                continue
            tables.append((schema, name))
            al = tb.alias
            if al and al != name:
                alias_map[al] = (schema, name)
    if not tables:
        return None
    bad_by_table: dict[str, dict] = {}
    for stmt in parsed:
        if stmt is None:
            continue
        for col in stmt.find_all(exp.Column):
            ref = col.table
            cname = col.name
            if not ref or not cname:
                continue
            if ref in alias_map:
                schema, table = alias_map[ref]
            else:
                m = [(sc, tn) for sc, tn in tables if tn == ref]
                if not m:
                    continue   # 子查询别名等，无法归属真实表，跳过
                schema, table = m[0]
            full = f"{schema}.{table}" if schema else table
            entry = bad_by_table.setdefault(full, {"real": None, "checked": False, "bad": set()})
            if not entry["checked"]:
                entry["real"] = await _real_columns(engine, datasource_id, schema, table)
                entry["checked"] = True
            real = entry["real"]
            if real is None:
                continue
            if cname.lower() not in {c.lower() for c in real}:
                entry["bad"].add(cname)
    issues = [(f, e["bad"], e["real"]) for f, e in bad_by_table.items() if e["bad"]]
    if not issues:
        return None
    lines = ["SQL 引用了不存在的字段（执行前校验拦截，未打到数据库，请照真实字段重写）："]
    for full, bad, real in issues:
        lines.append(f"表 {full} 不存在字段：{', '.join(sorted(bad))}")
        if real:
            lines.append(f"表 {full} 真实字段（{len(real)} 个）：{', '.join(sorted(real))}")
        lines.append("缺的字段（如某组指标没有 _score 列）直接去掉对应 CASE 分支，别按规律外推列名。")
    return "\n".join(lines)


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
        col_err = await _validate_columns(sql, engine, int(ds_id))
        if col_err:
            return ToolResult(summary=col_err)
        columns, rows = await _execute(engine, sql)
    except Exception as e:
        # 自愈：不抛异常，错误信息回灌让 LLM 改 SQL 重试
        msg = str(e)
        hint = ""
        import re
        is_col_error = bool(re.search(r"cannot be resolved", msg) or
                              re.search(r"Unknown column", msg, re.I))
        if is_col_error:
            hint = (" ——字段不存在。别按规律猜字段名（如以为每个指标都有 _score 列，"
                    "实际含补贴电价/不含补贴电价这组就没有 _score）。"
                    "对照 query_metadata 返回的真实列名，用存在的列重写，缺的列别查。")
        return ToolResult(summary=f"SQL 执行失败: {msg}{hint}")

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
        "\n"
        "【禁止重查已有数据】写新 SQL 前先看上方对话：本对话已 execute_sql 查回的列/行（preview + "
        "result_id 对应全量行）就是事实来源。归因/解释阶段需要实际值/计划值/完成率等列时，"
        "优先复用已查回的结果（上面没有的列才需要新查）；已在上方查回整行的，禁止再 SELECT 同一行同一批列重查一遍。"
        "一次查询就把后续归因要用的列（实际/计划/完成率/得分）一起带出来，别查完得分再补查明细。"
    ),
    parameters={"type": "object",
                "properties": {"sql": {"type": "string", "description": "要执行的只读 SQL"},
                               "datasource_id": {"type": "integer", "description": "数据源 ID（可选，缺省取第一个）"}},
                "required": ["sql"]},
    handler=execute_sql,
)
