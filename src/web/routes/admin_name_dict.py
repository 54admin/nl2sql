"""名称纠错别名字典 CRUD（P2）。normalizer dict_fn/fuzzy_fn 消费 enabled 的别名。
纯 PG，照 admin_business_rules 模式。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.storage.models import NameDict
from src.storage.pg_client import AsyncSessionFactory


class NameDictIn(BaseModel):
    alias: str               # 别名/错写
    standard: str            # 标准名（纠错目标）
    category: str = "table"  # table/column/metric
    enabled: bool = True


class NameDictPatch(BaseModel):
    standard: str | None = None
    category: str | None = None
    enabled: bool | None = None


def build_name_dict_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/name-dict")
    async def list_items(category: str | None = None) -> dict:
        async with AsyncSessionFactory() as s:
            stmt = NameDict.__table__.select()
            if category:
                stmt = stmt.where(NameDict.category == category)
            rows = (await s.execute(stmt)).all()
        return {"items": [{"id": r.id, "alias": r.alias, "standard": r.standard,
                           "category": r.category, "enabled": r.enabled,
                           "version": r.version} for r in rows]}

    @router.post("/api/admin/name-dict")
    async def create_item(req: NameDictIn) -> dict:
        async with AsyncSessionFactory() as s:
            item = NameDict(**req.model_dump())
            s.add(item); await s.commit()
            return {"id": item.id, "version": item.version}

    @router.put("/api/admin/name-dict/{item_id}")
    async def update_item(item_id: int, req: NameDictPatch) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(NameDict, item_id)
            if row is None:
                raise HTTPException(404, "别名不存在")
            for k, v in req.model_dump(exclude_none=True).items():
                setattr(row, k, v)
            row.version += 1
            await s.commit()
            return {"ok": True, "version": row.version}

    @router.delete("/api/admin/name-dict/{item_id}")
    async def delete_item(item_id: int) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(NameDict, item_id)
            if row is None:
                raise HTTPException(404, "别名不存在")
            await s.delete(row); await s.commit()
            return {"ok": True}

    return router
