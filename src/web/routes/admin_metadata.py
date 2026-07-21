"""元数据读取 + 逻辑关系（table_relations）CRUD + dashboard 总览。P1a。
表清单走 PG；字段懒加载（点表展开时连业务库实时拉，不存 PG）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.storage.models import Datasource, MetadataTable, TableRelation
from src.storage.pg_client import AsyncSessionFactory


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

    @router.put("/api/admin/metadata/tables/{table_id}")
    async def set_table_enabled(table_id: int, req: dict) -> dict:
        """勾选/取消表的参与问数开关。"""
        enabled = req.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(400, "enabled 必须是 bool")
        async with AsyncSessionFactory() as s:
            row = await s.get(MetadataTable, table_id)
            if row is None:
                raise HTTPException(404, "表不存在")
            row.enabled = enabled
            await s.commit()
            return {"ok": True}

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
            mt_rows = (await s.execute(MetadataTable.__table__.select())).all()

        # 按 datasource_id → schema_name 分组
        groups: dict[int, dict[str, list]] = {d.id: {} for d in ds_rows}
        for t in mt_rows:
            key = t.schema_name or ""    # 空作为默认组（老数据无 schema_name）
            groups.setdefault(t.datasource_id, {}).setdefault(key, []).append({
                "id": t.id, "table_name": t.table_name,
                "table_comment": t.table_comment, "kind": t.kind, "enabled": t.enabled})

        return {"datasources": [
            {**ds_map[did], "schemas": [
                {"schema_name": sk or "(默认库)", "tables": tbls}
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
