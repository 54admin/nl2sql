"""元数据同步：从业务库（StarRocks）Inspector 拉表+视图/字段，写系统 PG metadata_*。
保留 source=manual 的手写覆盖，不被同步冲掉。同步只写 comment/type，不碰 is_primary/role_tag。"""
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from src.logging import get_logger
from src.storage.models import MetadataColumn, MetadataTable
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


def _collect_one(insp, name: str, kind: str) -> dict | None:
    """收集单张表/视图的元数据。系统对象返回 None 跳过。kind: table/view。"""
    if _is_system_name(name):
        return None
    try:
        tcomment = (insp.get_table_comment(name) or {}).get("text") or ""
    except Exception:
        tcomment = ""
    try:
        cols = [{"name": c["name"], "type": str(c["type"]),
                 "comment": c.get("comment") or ""}
                for c in insp.get_columns(name)]
    except Exception:
        cols = []   # 视图拉字段失败兜底空
    return {"table": name, "kind": kind, "comment": tcomment, "columns": cols}


def _collect_sync(sync_conn) -> list[dict]:
    """同步函数（被 engine.run_sync 调用，在同步连接上跑 Inspector）。

    自动过滤系统库/表（information_schema/mysql/performance_schema/sys），
    与 sync_scope 无关——系统对象永远不进问数元数据。"""
    insp = inspect(sync_conn)
    out = []
    for name in insp.get_table_names():
        item = _collect_one(insp, name, "table")
        if item:
            out.append(item)
    # 视图 try/except：某些库/方言不支持 get_view_names
    try:
        for name in insp.get_view_names():
            item = _collect_one(insp, name, "view")
            if item:
                out.append(item)
    except Exception:
        pass
    return out


async def sync_metadata(ds_id: int, engine: AsyncEngine, sync_scope: str | None) -> dict:
    """同步一个数据源的元数据。返回 {added, updated, skipped}。

    ponytail: 库里已删的表/字段本期不清理（避免误删手写），后续可加。"""
    async with engine.connect() as conn:
        fetched = await conn.run_sync(_collect_sync)
    added = updated = skipped = 0
    async with AsyncSessionFactory() as s:
        for t in fetched:
            if not _in_scope(t["table"], sync_scope):
                continue
            row = (await s.execute(MetadataTable.__table__.select().where(
                MetadataTable.datasource_id == ds_id,
                MetadataTable.table_name == t["table"]))).first()
            if row is None:
                mt = MetadataTable(datasource_id=ds_id, table_name=t["table"],
                                   table_comment=t["comment"], source="synced",
                                   kind=t.get("kind", "table"))
                s.add(mt); await s.flush()
                for c in t["columns"]:
                    s.add(MetadataColumn(table_id=mt.id, column_name=c["name"],
                                         column_comment=c["comment"], data_type=c["type"],
                                         source="synced"))
                added += 1 + len(t["columns"])
            elif row.source == "synced":
                await s.execute(MetadataTable.__table__.update().where(
                    MetadataTable.id == row.id).values(
                        table_comment=t["comment"], kind=t.get("kind", "table")))
                for c in t["columns"]:
                    crow = (await s.execute(MetadataColumn.__table__.select().where(
                        MetadataColumn.table_id == row.id,
                        MetadataColumn.column_name == c["name"]))).first()
                    if crow is None:
                        s.add(MetadataColumn(table_id=row.id, column_name=c["name"],
                                             column_comment=c["comment"], data_type=c["type"],
                                             source="synced"))
                        added += 1
                    elif crow.source == "synced":
                        await s.execute(MetadataColumn.__table__.update().where(
                            MetadataColumn.id == crow.id).values(
                                column_comment=c["comment"], data_type=c["type"]))
                        updated += 1
                    else:
                        skipped += 1          # manual 字段不动
                updated += 1
            else:
                skipped += 1                  # 整表 manual
        await s.commit()
    log.info("元数据同步 ds=%s added=%s updated=%s skipped=%s",
             ds_id, added, updated, skipped)
    return {"added": added, "updated": updated, "skipped": skipped}
