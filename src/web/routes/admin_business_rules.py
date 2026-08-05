"""业务规则 CRUD（人工录入，表级）。运行时由 query_metadata 按表附给 LLM。
纯 PG 操作，不依赖 DataSourceManager。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.storage.models import BusinessRule
from src.storage.pg_client import AsyncSessionFactory


class BusinessRuleIn(BaseModel):
    table_name: str    # 表全限定名（必填）
    key: str
    value_json: str
    enabled: bool = True


class BusinessRulePatch(BaseModel):
    table_name: str | None = None
    value_json: str | None = None
    enabled: bool | None = None


def build_business_rules_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/business-rules")
    async def list_rules() -> dict:
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(BusinessRule.__table__.select())).all()
        return {"rules": [{"id": r.id, "table_name": r.table_name,
                           "key": r.key,
                           "value_json": r.value_json, "enabled": r.enabled,
                           "version": r.version} for r in rows]}

    @router.post("/api/admin/business-rules")
    async def create_rule(req: BusinessRuleIn) -> dict:
        async with AsyncSessionFactory() as s:
            rule = BusinessRule(**req.model_dump())
            s.add(rule); await s.commit()
            return {"id": rule.id, "version": rule.version}

    @router.put("/api/admin/business-rules/{rule_id}")
    async def update_rule(rule_id: int, req: BusinessRulePatch) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(BusinessRule, rule_id)
            if row is None:
                raise HTTPException(404, "规则不存在")
            for k, v in req.model_dump(exclude_none=True).items():
                setattr(row, k, v)
            row.version += 1
            await s.commit()
            return {"ok": True, "version": row.version}

    @router.delete("/api/admin/business-rules/{rule_id}")
    async def delete_rule(rule_id: int) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(BusinessRule, rule_id)
            if row is None:
                raise HTTPException(404, "规则不存在")
            await s.delete(row); await s.commit()
            return {"ok": True}

    return router
