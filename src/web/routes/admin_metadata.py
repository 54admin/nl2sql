"""元数据读取 + 逻辑关系（table_relations）CRUD + dashboard 总览。P1a。
表清单实时连业务库拉（fetch_objects，不依赖 PG 同步缓存），左连 PG 显示 enabled/手写注释。
PG metadata_tables 只存「用户配置过的表」：勾选白名单（enabled=true）+ 手写注释（source=manual）。
字段懒加载（点表展开时连业务库实时拉，不存 PG）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from src.storage.models import Datasource, MetadataTable, TableRelation
from src.storage.db_client import AsyncSessionFactory


class TableRelationIn(BaseModel):
    datasource_id: int
    main_table: str
    rel_table: str
    join_keys_json: str
    join_type: str = "inner"
    business_note: str | None = None


def build_metadata_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/metadata")
    async def read_metadata(datasource_id: int,
                            schema_name: str | None = None) -> dict:
        """读某数据源的表清单（不含字段——字段点表展开时按需拉）。
        schema_name 非空=按库过滤；空=该数据源下全部表（兼容老前端）。"""
        async with AsyncSessionFactory() as s:
            q = MetadataTable.__table__.select().where(
                MetadataTable.datasource_id == datasource_id)
            if schema_name is not None:
                q = q.where(MetadataTable.schema_name == schema_name)
            tables = (await s.execute(q)).all()
            out = [{"id": t.id, "schema_name": t.schema_name,
                    "table_name": t.table_name, "table_comment": t.table_comment,
                    "source": t.source, "kind": t.kind, "enabled": t.enabled}
                   for t in tables]
            return {"tables": out}

    @router.get("/api/admin/metadata/enabled-tables")
    async def enabled_tables() -> dict:
        """所有参与问数（enabled=true）的表全限定名列表。
        业务规则页「表名」下拉用——只让用户选已加入查询的表。"""
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(MetadataTable.__table__.select().where(
                MetadataTable.enabled.is_(True)))).all()
        names = [f"{r.schema_name}.{r.table_name}" if r.schema_name else r.table_name
                 for r in rows]
        return {"tables": names}

    @router.get("/api/admin/metadata/tables/{table_id}/columns")
    async def get_table_columns(table_id: int) -> dict:
        """点表展开时实时拉该表字段（连业务库，字段懒加载，不存 PG）。
        schema_name 存在时按库拉字段（跨库元数据查询）。"""
        from src.datasource.manager import DataSourceManager
        from src.datasource.metadata_sync import fetch_table_columns
        async with AsyncSessionFactory() as s:
            t = await s.get(MetadataTable, table_id)
        if t is None:
            raise HTTPException(404, "表不存在")
        engine = await DataSourceManager().get_engine(t.datasource_id)
        cols = await fetch_table_columns(engine, t.table_name, t.schema_name)
        return {"columns": [{"name": c["name"], "type": c["type"], "comment": c["comment"]}
                            for c in cols]}

    @router.get("/api/admin/datasources/{ds_id}/schemas/{schema}/objects")
    async def list_objects(ds_id: int, schema: str) -> dict:
        """实时连业务库拉该库的表+视图，左连 PG 显示 enabled/手写注释。
        表清单永远最新（不读 PG 缓存——业务库新加/删的表立即可见）；
        PG 只贡献 enabled 标志 + source=manual 的手写注释。PG 没记录的表 enabled=false。"""
        from src.datasource.manager import DataSourceManager
        from src.datasource.metadata_sync import fetch_objects
        try:
            engine = await DataSourceManager().get_engine(ds_id)
        except KeyError:
            raise HTTPException(404, "数据源不存在")
        try:
            tables = await fetch_objects(engine, schema)
        except Exception as e:
            raise HTTPException(400, f"拉表清单失败: {e}")
        # 左连 PG metadata_tables（ds+schema）拿 enabled + 手写注释
        async with AsyncSessionFactory() as s:
            pg_rows = (await s.execute(select(MetadataTable).where(
                MetadataTable.datasource_id == ds_id,
                MetadataTable.schema_name == schema))).scalars().all()
        pg = {r.table_name: r for r in pg_rows}
        objects = []
        for t in tables:
            row = pg.get(t["name"])
            # 注释优先：PG 手写（source=manual）> 业务库实时注释（fetch_objects 拉）。
            cmt = (row.table_comment if row and row.source == "manual" else None) \
                or t.get("comment") or ""
            objects.append({"name": t["name"], "kind": t["kind"], "comment": cmt,
                            "enabled": bool(row.enabled) if row else False,
                            "id": row.id if row else None})
        return {"objects": objects}

    @router.get("/api/admin/datasources/{ds_id}/schemas/{schema}/tables/{table_name}/columns")
    async def get_columns_by_key(ds_id: int, schema: str, table_name: str) -> dict:
        """按 ds+schema+table_name 实时拉字段（无需 PG 行）。
        配置页点任意表（含未勾选、未进 PG 的）查字段用。"""
        from src.datasource.manager import DataSourceManager
        from src.datasource.metadata_sync import fetch_table_columns
        try:
            engine = await DataSourceManager().get_engine(ds_id)
        except KeyError:
            raise HTTPException(404, "数据源不存在")
        try:
            cols = await fetch_table_columns(engine, table_name, schema)
        except Exception as e:
            raise HTTPException(400, f"拉字段失败: {e}")
        return {"columns": [{"name": c["name"], "type": c["type"], "comment": c["comment"]}
                            for c in cols]}

    @router.put("/api/admin/metadata/tables/{table_id}")
    async def set_table_enabled(table_id: int, req: dict) -> dict:
        """勾选/取消表的参与问数开关。
        table_id > 0 → 更新该 PG 行；
        table_id = 0 → 用 body 的 datasource_id+schema_name+table_name upsert（PG 无该行则新建）。
        enabled=false 不删行（保留可能的手写注释）；PG 无该行又取消勾选则空操作。"""
        enabled = req.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(400, "enabled 必须是 bool")
        async with AsyncSessionFactory() as s:
            if table_id:
                row = await s.get(MetadataTable, table_id)
                if row is None:
                    raise HTTPException(404, "表不存在")
            else:
                # upsert by ds+schema+table（实时拉的表 PG 可能没行）
                ds_id = req.get("datasource_id")
                schema_name = req.get("schema_name")
                tname = req.get("table_name")
                if not (ds_id and tname):
                    raise HTTPException(400, "id=0 时需要 datasource_id+table_name（schema_name 可空）")
                # schema_name 为 None 时用 is_(None) 匹配（SQL = NULL 不命中）
                schema_filter = (MetadataTable.schema_name.is_(None) if schema_name is None
                                 else MetadataTable.schema_name == schema_name)
                row = (await s.execute(select(MetadataTable).where(
                    MetadataTable.datasource_id == ds_id,
                    schema_filter,
                    MetadataTable.table_name == tname))).scalar_one_or_none()
                if row is None:
                    if not enabled:
                        return {"ok": True, "id": None}    # 无行又取消勾选——空操作
                    # 新勾选——写一行（comment/kind 从 body 带的实时业务库值）
                    row = MetadataTable(datasource_id=ds_id, schema_name=schema_name,
                                        table_name=tname, table_comment=req.get("comment"),
                                        source="synced", kind=req.get("kind", "table"))
                    s.add(row)
            row.enabled = enabled
            await s.commit()
            return {"ok": True, "id": row.id}

    @router.get("/api/admin/dashboard")
    async def dashboard() -> dict:
        """总览：所有数据源 × 每个源下按库分组的表清单（含 enabled/kind）。
        配置页 dashboard 视图用——一眼看到连了哪些源、接了哪些库、勾了哪些表参与问数。"""
        async with AsyncSessionFactory() as s:
            ds_rows = (await s.execute(Datasource.__table__.select())).all()
            ds_map = {d.id: {"id": d.id, "name": d.name, "type": d.type,
                             "host": d.host, "port": d.port, "db_name": d.db_name,
                             "enabled": d.enabled}
                      for d in ds_rows}
            mt_rows = (await s.execute(
                MetadataTable.__table__.select().where(MetadataTable.enabled == True))).all()

        # 按 datasource_id → schema_name 分组
        groups: dict[int, dict[str, list]] = {d.id: {} for d in ds_rows}
        for t in mt_rows:
            key = t.schema_name or ""    # 空作为默认组（老数据无 schema_name）
            groups.setdefault(t.datasource_id, {}).setdefault(key, []).append({
                "id": t.id, "table_name": t.table_name,
                "table_comment": t.table_comment, "kind": t.kind, "enabled": t.enabled})

        return {"datasources": [
            {**ds_map[did], "schemas": [
                # ponytail: schema 空就空字符串（前端显示空），不发明"默认库"概念
                {"schema_name": sk, "tables": tbls}
                for sk, tbls in sorted(schemas.items())]}
            for did, schemas in groups.items()
        ]}

    @router.get("/api/admin/table-relations")
    async def list_relations(datasource_id: int) -> dict:
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(TableRelation.__table__.select().where(
                TableRelation.datasource_id == datasource_id))).all()
        return {"relations": [{"id": r.id, "datasource_id": r.datasource_id,
                               "main_table": r.main_table, "rel_table": r.rel_table,
                               "join_keys_json": r.join_keys_json, "join_type": r.join_type,
                               "business_note": r.business_note} for r in rows]}

    @router.post("/api/admin/table-relations")
    async def create_relation(req: TableRelationIn) -> dict:
        async with AsyncSessionFactory() as s:
            rel = TableRelation(**req.model_dump())
            s.add(rel); await s.commit()
            return {"id": rel.id}

    @router.delete("/api/admin/table-relations/{rel_id}")
    async def delete_relation(rel_id: int) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(TableRelation, rel_id)
            if row is None:
                raise HTTPException(404, "关系不存在")
            await s.delete(row); await s.commit()
            return {"ok": True}

    return router
