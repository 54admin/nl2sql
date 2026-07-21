"""元数据读取 + 逻辑关系（table_relations）CRUD。P1a。
表清单走 PG；字段懒加载（点表展开时连业务库实时拉，不存 PG）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.storage.models import MetadataTable, TableRelation
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
    async def read_metadata(datasource_id: int) -> dict:
        """读某数据源的表清单（不含字段——字段点表展开时按需拉 /api/admin/metadata/tables/{id}/columns）。"""
        async with AsyncSessionFactory() as s:
            tables = (await s.execute(MetadataTable.__table__.select().where(
                MetadataTable.datasource_id == datasource_id))).all()
            out = [{"id": t.id, "table_name": t.table_name, "table_comment": t.table_comment,
                    "source": t.source, "kind": t.kind, "enabled": t.enabled}
                   for t in tables]
            return {"tables": out}

    @router.get("/api/admin/metadata/tables/{table_id}/columns")
    async def get_table_columns(table_id: int) -> dict:
        """点表展开时实时拉该表字段（连业务库，字段懒加载，不存 PG）。"""
        from src.datasource.manager import DataSourceManager
        from src.datasource.metadata_sync import fetch_table_columns
        async with AsyncSessionFactory() as s:
            t = await s.get(MetadataTable, table_id)
        if t is None:
            raise HTTPException(404, "表不存在")
        engine = await DataSourceManager().get_engine(t.datasource_id)
        cols = await fetch_table_columns(engine, t.table_name)
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
