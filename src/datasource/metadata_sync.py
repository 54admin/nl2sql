"""元数据同步：从业务库（StarRocks）Inspector 拉表+视图**名清单**（只表名/注释/kind，不拉字段），
写系统 PG metadata_tables。字段按需拉（fetch_table_columns）——点表展开 / query_metadata 时调。
保留 source=manual 的手写覆盖，不被同步冲掉。同步只写 comment/kind，不碰 is_primary/role_tag。"""
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from src.logging import get_logger
from src.storage.models import MetadataTable
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)

# 系统库/表集合：sync_scope 空=全部业务表时，自动过滤掉这些（StarRocks/MySQL/PG 通用）
_SYSTEM_SCHEMAS = frozenset({"information_schema", "mysql", "performance_schema", "sys"})


def _is_system_name(name: str) -> bool:
    """库名/表名命中系统库集合则跳过。支持 schema.table 形式（任一段命中即视为系统对象）。"""
    return any(p in _SYSTEM_SCHEMAS for p in name.lower().split("."))


def _in_scope(table_name: str, sync_scope: str | None) -> bool:
    """sync_scope 为空=全要；非空=表名匹配任一前缀/全名才要。"""
    if not sync_scope:
        return True
    prefixes = [p.strip() for p in sync_scope.split(",") if p.strip()]
    return any(table_name == p or table_name.startswith(p) for p in prefixes)


def _collect_one(insp, name: str, kind: str, schema: str | None = None) -> dict | None:
    """收集单张表/视图的元数据（仅表名+注释+kind，**不拉字段**——字段按需 fetch_table_columns）。
    系统对象返回 None 跳过。kind: table/view。schema 用于 get_table_comment 限定库。"""
    if _is_system_name(name):
        return None
    try:
        # schema 非空时传给 get_table_comment（跨库注释查询）
        kw = {"schema": schema} if schema else {}
        tcomment = (insp.get_table_comment(name, **kw) or {}).get("text") or ""
    except Exception:
        tcomment = ""
    return {"table": name, "kind": kind, "comment": tcomment}


def _collect_sync(sync_conn, schema: str | None = None) -> list[dict]:
    """同步函数（被 engine.run_sync 调用，在同步连接上跑 Inspector）。
    只拉表/视图名清单 + 注释，**不拉字段**（字段懒加载，同步快）。
    schema 非空时拉指定库（get_table_names(schema=...)）；空=老行为（连的什么库拉什么库）。
    自动过滤系统库/表（information_schema/mysql/performance_schema/sys），
    与 sync_scope 无关——系统对象永远不进问数元数据。"""
    insp = inspect(sync_conn)
    kw = {"schema": schema} if schema else {}
    out = []
    for name in insp.get_table_names(**kw):
        item = _collect_one(insp, name, "table", schema)
        if item:
            out.append(item)
    # 视图 try/except：某些库/方言不支持 get_view_names
    try:
        for name in insp.get_view_names(**kw):
            item = _collect_one(insp, name, "view", schema)
            if item:
                out.append(item)
    except Exception:
        pass
    return out


async def fetch_table_columns(engine: AsyncEngine, table_name: str,
                              schema: str | None = None) -> list[dict]:
    """连业务库按需拉单张表的字段（点表展开 / query_metadata 时调）。
    schema 非空时拉指定库的字段（跨库元数据查询）。
    返回 [{name, type, comment}]。拉失败抛异常（调用方 catch）。"""
    def _get_cols(sync_conn):   # run_sync 要求同步函数（在同步连接上跑 Inspector）
        insp = inspect(sync_conn)
        kw = {"schema": schema} if schema else {}
        return [{"name": c["name"], "type": str(c["type"]),
                 "comment": c.get("comment") or ""}
                for c in insp.get_columns(table_name, **kw)]
    async with engine.connect() as conn:
        return await conn.run_sync(_get_cols)


async def fetch_objects(engine: AsyncEngine, schema: str | None) -> list[dict]:
    """实时连业务库拉指定库的表+视图清单（仅名/kind/注释），**不写 PG**。
    配置页点库时调——永远拿最新业务库状态（业务库新加/删的表立即可见，不依赖 PG 同步缓存）。
    复用 _collect_sync（同名清单收集逻辑）。返回 [{name, kind, comment}]。"""
    async with engine.connect() as conn:
        fetched = await conn.run_sync(_collect_sync, schema)
    return [{"name": t["table"], "kind": t["kind"], "comment": t["comment"]} for t in fetched]


async def sync_metadata(ds_id: int, engine: AsyncEngine, sync_scope: str | None,
                        schema_name: str | None = None) -> dict:
    """同步一个数据源**指定库**的元数据（**只拉表名清单**，不拉字段）。
    schema_name 空=老行为（兼容老调用 / SQLite 测试）；非空=拉指定库的表+视图。
    返回 {added, updated, skipped}。

    ponytail: 字段不再同步时存——按需 fetch_table_columns 实时拉；
    库里已删的表本期不清理（避免误删手写），后续可加。"""
    async with engine.connect() as conn:
        fetched = await conn.run_sync(_collect_sync, schema_name)
    added = updated = skipped = 0
    async with AsyncSessionFactory() as s:
        for t in fetched:
            if not _in_scope(t["table"], sync_scope):
                continue
            row = (await s.execute(MetadataTable.__table__.select().where(
                MetadataTable.datasource_id == ds_id,
                MetadataTable.schema_name == schema_name,
                MetadataTable.table_name == t["table"]))).first()
            if row is None:
                s.add(MetadataTable(datasource_id=ds_id, schema_name=schema_name,
                                    table_name=t["table"],
                                    table_comment=t["comment"], source="synced",
                                    kind=t.get("kind", "table")))
                added += 1
            elif row.source == "synced":
                await s.execute(MetadataTable.__table__.update().where(
                    MetadataTable.id == row.id).values(
                        table_comment=t["comment"], kind=t.get("kind", "table")))
                updated += 1
            else:
                skipped += 1                  # 整表 manual
        await s.commit()
    log.info("元数据同步 ds=%s schema=%s added=%s updated=%s skipped=%s",
             ds_id, schema_name, added, updated, skipped)
    return {"added": added, "updated": updated, "skipped": skipped}
