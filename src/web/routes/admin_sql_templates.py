"""SQL 模板 CRUD（人工录入）。清单拼进 get_sql_template 工具 description，LLM 按需调工具取。
纯 PG 操作，不依赖 DataSourceManager。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.storage.models import SqlTemplate
from src.storage.db_client import AsyncSessionFactory


class SqlTemplateIn(BaseModel):
    name: str
    sql_template: str
    usage: str | None = None
    enabled: bool = True


class SqlTemplatePatch(BaseModel):
    name: str | None = None
    sql_template: str | None = None
    usage: str | None = None
    enabled: bool | None = None


def build_sql_templates_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/sql-templates")
    async def list_templates() -> dict:
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(SqlTemplate.__table__.select())).all()
        return {"templates": [{"id": r.id, "name": r.name,
                               "sql_template": r.sql_template, "usage": r.usage,
                               "enabled": r.enabled, "version": r.version} for r in rows]}

    @router.post("/api/admin/sql-templates")
    async def create_template(req: SqlTemplateIn) -> dict:
        async with AsyncSessionFactory() as s:
            t = SqlTemplate(**req.model_dump())
            s.add(t); await s.commit()
            return {"id": t.id, "version": t.version}

    @router.put("/api/admin/sql-templates/{tpl_id}")
    async def update_template(tpl_id: int, req: SqlTemplatePatch) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(SqlTemplate, tpl_id)
            if row is None:
                raise HTTPException(404, "模板不存在")
            for k, v in req.model_dump(exclude_none=True).items():
                setattr(row, k, v)
            row.version += 1
            await s.commit()
            return {"ok": True, "version": row.version}

    @router.delete("/api/admin/sql-templates/{tpl_id}")
    async def delete_template(tpl_id: int) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(SqlTemplate, tpl_id)
            if row is None:
                raise HTTPException(404, "模板不存在")
            await s.delete(row); await s.commit()
            return {"ok": True}

    return router
