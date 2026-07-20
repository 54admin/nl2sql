"""execute_sql 工具：在业务库执行 LLM 生成的 SQL → 全量结果旁路（result_id）→ 摘要回灌 LLM。

SQL 护栏本期推迟（validate_sql 占位 pass-through），生产前补（见 spec 第 9 章）。
执行失败不抛异常，返回 error 摘要让 LLM 自愈重试。"""
from __future__ import annotations

import json

from sqlalchemy import text

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.storage.query_results import save_result


def validate_sql(sql: str) -> str | None:
    """SQL 安全护栏。本期占位 pass-through（用户决定先不做）。
    ponytail: 生产前补——sqlglot 解析拦 DDL/DML + 超时 + 行数限制。返回错误信息或 None。"""
    return None


async def _execute(engine, sql: str) -> tuple[list, list]:
    """在业务库执行 SQL，返回 (columns, rows)。
    ponytail: 用 result.keys() + row._mapping；若遇到奇怪行对象再换 result.mappings().all()。"""
    async with engine.connect() as conn:
        result = await conn.execute(text(sql))
        columns = list(result.keys())
        rows = [dict(r._mapping) for r in result.fetchall()]
    return columns, rows


def _preview(rows: list, n: int = 5) -> list:
    """前 n 行预览回灌 LLM，全量走 result_id 旁路。"""
    return rows[:n]


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

    result_id = await save_result(session_id, columns, rows, datasource_id=int(ds_id))
    summary = {"result_id": result_id, "rows": len(rows), "columns": columns,
               "preview": _preview(rows)}
    return ToolResult(summary=json.dumps(summary, ensure_ascii=False, default=str))


EXECUTE_SQL = ToolDefinition(
    name="execute_sql",
    description=("在业务库执行只读 SQL 查询，返回结果摘要（行数/列名/前 5 行预览）+ result_id。"
                 "调用前先用 query_metadata 了解可查的表与字段。"),
    parameters={"type": "object",
                "properties": {"sql": {"type": "string", "description": "要执行的只读 SQL"},
                               "datasource_id": {"type": "integer", "description": "数据源 ID（可选，缺省取第一个）"}},
                "required": ["sql"]},
    handler=execute_sql,
)
