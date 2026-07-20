"""元数据读取 + 逻辑关系（table_relations）CRUD。P1a。
纯 PG 操作，不依赖 DataSourceManager。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.storage.models import MetadataColumn, MetadataTable, TableRelation
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
        """读某数据源的元数据（表 + 字段），供 P1b query_metadata 调用。"""
        async with AsyncSessionFactory() as s:
            tables = (await s.execute(MetadataTable.__table__.select().where(
                MetadataTable.datasource_id == datasource_id))).all()
            out = []
            for t in tables:
                cols = (await s.execute(MetadataColumn.__table__.select().where(
                    MetadataColumn.table_id == t.id))).all()
                out.append({
                    "table_name": t.table_name, "table_comment": t.table_comment,
                    "source": t.source,
                    "columns": [{"column_name": c.column_name, "column_comment": c.column_comment,
                                 "data_type": c.data_type, "is_primary": c.is_primary,
                                 "role_tag": c.role_tag, "source": c.source} for c in cols]})
            return {"tables": out}

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
