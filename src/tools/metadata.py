"""query_metadata 工具：返回某数据源里 enabled=True 的表清单（表名/注释 + 实时拉的字段）+ 已配逻辑关联，
供 LLM 选表 / 生成多表 JOIN。白名单衔接配置页：只有勾选参与的表才返回。

字段懒加载：同步只存表名清单，这里对每张 enabled 表实时连业务库 fetch_table_columns 拉字段。"""
from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.datasource.metadata_sync import fetch_table_columns
from src.storage.models import MetadataTable, TableRelation
from src.storage.pg_client import AsyncSessionFactory


async def _list_enabled_tables(datasource_id: int, engine: AsyncEngine) -> list[dict]:
    """读 enabled=True 的表（表名+注释），实时拉每张表的字段。
    返回 [{table_name, table_comment, columns:[{name,comment,type}]}]。
    ponytail: 每表一次连业务库拉字段，白名单表数 ≤10 规模可接受；表多了再换并发或缓存。"""
    async with AsyncSessionFactory() as s:
        tables = (await s.execute(MetadataTable.__table__.select().where(
            MetadataTable.datasource_id == datasource_id,
            MetadataTable.enabled.is_(True)))).all()
    out = []
    for t in tables:
        cols = await fetch_table_columns(engine, t.table_name)
        out.append({
            "table_name": t.table_name,
            "table_comment": t.table_comment or "",
            "columns": [{"name": c["name"], "comment": c["comment"], "type": c["type"]}
                        for c in cols],
        })
    return out


async def _list_relations(datasource_id: int) -> list[dict]:
    """读已配逻辑关联（table_relations）。join_keys_json 解成 list。
    ponytail: 本期只消费已配关联；无配置时 LLM 依字段注释自行推导 JOIN + 标 inferred 转正留后续。"""
    async with AsyncSessionFactory() as s:
        rels = (await s.execute(TableRelation.__table__.select().where(
            TableRelation.datasource_id == datasource_id))).all()
        return [{
            "main_table": r.main_table,
            "rel_table": r.rel_table,
            "join_keys": json.loads(r.join_keys_json),
            "join_type": r.join_type,
            "business_note": r.business_note or "",
        } for r in rels]


async def query_metadata(args: dict, ctx: LoopContext,
                         cancel_token: CancelToken) -> ToolResult:
    """工具 handler。args 可带 datasource_id；缺省取第一个数据源（单源场景，多源绑源留 P1c）。
    返回 {tables:[...], relations:[...]}：tables=白名单表（字段实时拉），relations=已配 JOIN 口径。"""
    from src.datasource.manager import DataSourceManager
    ds_id = args.get("datasource_id")
    mgr = DataSourceManager()
    if ds_id is None:
        rows = await mgr.list_datasources()
        if not rows:
            return ToolResult(summary="无可用数据源，请先在配置页添加。")
        ds_id = rows[0]["id"]
    engine = await mgr.get_engine(int(ds_id))
    tables = await _list_enabled_tables(int(ds_id), engine)
    if not tables:
        return ToolResult(summary="该数据源没有勾选参与问数的表，请在配置页勾选表后再问。")
    relations = await _list_relations(int(ds_id))
    return ToolResult(summary=json.dumps({"tables": tables, "relations": relations},
                                         ensure_ascii=False))


QUERY_METADATA = ToolDefinition(
    name="query_metadata",
    description=("查看当前数据源里可以查询的表清单（表名/中文注释/字段）。"
                 "先调它了解有哪些表，再决定查哪张表。无需参数。"),
    parameters={"type": "object", "properties": {}, "required": []},
    handler=query_metadata,
)
