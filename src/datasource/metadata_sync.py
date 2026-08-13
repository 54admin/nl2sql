"""元数据同步：从业务库（StarRocks）Inspector 拉表+视图**名清单**（只表名/注释/kind，不拉字段），
写系统 PG metadata_tables。字段按需拉（fetch_table_columns）——点表展开 / query_metadata 时调。
保留 source=manual 的手写覆盖，不被同步冲掉。同步只写 comment/kind，不碰 is_primary/role_tag。"""
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from src.logging import get_logger
from src.storage.models import MetadataTable
from src.storage.db_client import AsyncSessionFactory

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
    返回 [{name, type, comment}]。拉失败抛异常（调用方 catch）。

    ponytail: 走 _fast_columns —— information_schema.columns **一次查询**拉
    name+type+comment（1 查询，快 + 有注释；StarRocks 上字段注释只有这条通道能拿到：
    Inspector.get_columns 不填 comment，SHOW FULL COLUMNS 也不返 Comment 列）；
    information_schema 不可用 / 返空时 fallback Inspector（保底）。"""
    async with engine.connect() as conn:
        return await conn.run_sync(_fast_columns, table_name, schema)


def _fast_columns(sync_conn, table_name: str, schema: str | None) -> list[dict]:
    """fetch_table_columns 的同步主体（被 run_sync 调用）。
    优先 information_schema.columns **一次查询**拉 name+type+comment
    （快 + 有注释，与 _fast_objects 对称：表注释靠 information_schema.tables.table_comment，
    字段注释靠 information_schema.columns.column_comment）；
    information_schema 不可用 / 返空时 fallback Inspector → SHOW FULL COLUMNS → SELECT * LIMIT 0。

    表和视图同路径：information_schema.columns 同时覆盖两者。"""
    from sqlalchemy import text
    # 优先：information_schema.columns 一次拉字段名+类型+注释（1 查询，快 + 有注释）
    try:
        sql = ("SELECT column_name, column_type, data_type, column_comment "
               "FROM information_schema.columns WHERE table_name = :t")
        params: dict = {"t": table_name}
        if schema:
            sql += " AND table_schema = :s"
            params["s"] = schema
        sql += " ORDER BY ordinal_position"
        rows = sync_conn.execute(text(sql), params).fetchall()
        out = []
        for r in rows:
            d = r._mapping if hasattr(r, "_mapping") else r
            name = d.get("column_name") or d.get("COLUMN_NAME")
            if not name:
                continue
            # column_type 更完整（如 VARCHAR(32)），data_type 兜底（如 varchar）
            col_type = (d.get("column_type") or d.get("COLUMN_TYPE")
                        or d.get("data_type") or d.get("DATA_TYPE") or "")
            cmt = d.get("column_comment") or d.get("COLUMN_COMMENT") or ""
            out.append({"name": name, "type": str(col_type), "comment": str(cmt)})
        if out:
            return out
    except Exception:
        pass
    # fallback：Inspector（information_schema 不可用时，多方言保底）
    return _inspector_columns(sync_conn, table_name, schema)


def _inspector_columns(sync_conn, table_name: str, schema: str | None) -> list[dict]:
    """_fast_columns 的兜底：Inspector.get_columns 拿字段（多方言兼容），
    空（StarRocks 视图等）时再 fallback SHOW FULL COLUMNS / SELECT * LIMIT 0。"""
    insp = inspect(sync_conn)
    kw = {"schema": schema} if schema else {}
    cols = list(insp.get_columns(table_name, **kw))
    if cols:
        return [{"name": c["name"], "type": str(c["type"]),
                 "comment": c.get("comment") or ""}
                for c in cols]
    # 再 fallback：视图/Inspector 不支持时，SHOW FULL COLUMNS 或 SELECT * LIMIT 0 兜底
    return _fallback_columns(sync_conn, table_name, schema)


def _fallback_columns(sync_conn, table_name: str, schema: str | None) -> list[dict]:
    """Inspector 拉不到（StarRocks 视图等）时的兜底：SHOW FULL COLUMNS 拿字段名/类型/注释，
    再不行 SELECT * LIMIT 0 拿列名。返回 [{name, type, comment}]。拿不到就空 list（不报错）。"""
    from sqlalchemy import text
    full = f"{schema}.{table_name}" if schema else table_name
    try:
        rows = sync_conn.execute(text(f"SHOW FULL COLUMNS FROM {full}")).fetchall()
        out = []
        for r in rows:
            d = r._mapping if hasattr(r, "_mapping") else r
            # MySQL/StarRocks SHOW FULL COLUMNS: Field/Type/.../Comment（别名大小写依方言）
            name = d.get("Field") or d.get("field")
            col_type = d.get("Type") or d.get("type") or ""
            comment = d.get("Comment") or d.get("comment") or ""
            if name:
                out.append({"name": name, "type": str(col_type), "comment": str(comment)})
        return out
    except Exception:
        # SHOW 也失败 → SELECT * LIMIT 0 拿列名（类型/注释无）
        try:
            result = sync_conn.execute(text(f"SELECT * FROM {full} LIMIT 0"))
            return [{"name": c, "type": "", "comment": ""} for c in result.keys()]
        except Exception:
            return []


def _fast_objects(sync_conn, schema: str | None) -> list[dict]:
    """fetch_objects 的同步主体（被 run_sync 调用）。
    优先 information_schema.tables **一次查询**拉 name+type+comment
    （快 + 有注释，不逐表 get_table_comment——615 表省 615 次查询）；
    information_schema 不可用 / 返空时 fallback Inspector（保底但无注释）。"""
    from sqlalchemy import text
    # 优先：information_schema.tables 一次拉表名+类型+注释（1 查询，快 + 有注释）
    try:
        sql = "SELECT table_name, table_type, table_comment FROM information_schema.tables"
        params = {}
        if schema:
            sql += " WHERE table_schema = :s"
            params["s"] = schema
        rows = sync_conn.execute(text(sql), params).fetchall()
        out = []
        for r in rows:
            d = r._mapping if hasattr(r, "_mapping") else r
            name = d.get("table_name") or d.get("TABLE_NAME")
            if not name or _is_system_name(name):
                continue
            ttype = (d.get("table_type") or d.get("TABLE_TYPE") or "").upper()
            kind = "view" if "VIEW" in ttype else "table"
            cmt = d.get("table_comment") or d.get("TABLE_COMMENT") or ""
            out.append({"name": name, "kind": kind, "comment": cmt})
        if out:
            return out
    except Exception:
        pass
    # fallback：Inspector（information_schema 不可用时，无注释但保底）
    insp = inspect(sync_conn)
    kw = {"schema": schema} if schema else {}
    out = []
    for n in insp.get_table_names(**kw):
        if not _is_system_name(n):
            out.append({"name": n, "kind": "table", "comment": ""})
    try:
        for n in insp.get_view_names(**kw):
            if not _is_system_name(n):
                out.append({"name": n, "kind": "view", "comment": ""})
    except Exception:
        pass
    return out


async def fetch_objects(engine: AsyncEngine, schema: str | None) -> list[dict]:
    """实时连业务库拉指定库的表+视图清单（名/类型/注释），不写 PG。
    配置页点库时调：永远拿最新业务库状态，不依赖 PG 缓存。
    ponytail: 走 _fast_objects（information_schema 一次查询，快 + 有注释）。返回 [{name, kind, comment}]。"""
    async with engine.connect() as conn:
        return await conn.run_sync(_fast_objects, schema)


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
