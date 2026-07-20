"""数据源管理路由：CRUD + 连通性测试 + 元数据同步触发。P1a。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.datasource.manager import DataSourceManager
from src.datasource.metadata_sync import sync_metadata


class DatasourceIn(BaseModel):
    name: str
    type: str = "starrocks"
    host: str
    port: int
    db_name: str
    username: str
    password: str
    sync_scope: str | None = None
    enabled: bool = True


class DatasourcePatch(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    db_name: str | None = None
    username: str | None = None
    password: str | None = None
    sync_scope: str | None = None
    enabled: bool | None = None


def build_datasource_router(mgr: DataSourceManager) -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/datasources")
    async def list_ds() -> dict:
        return {"datasources": await mgr.list_datasources()}

    @router.post("/api/admin/datasources")
    async def create_ds(req: DatasourceIn) -> dict:
        ds_id = await mgr.create_datasource(req.model_dump())
        return {"id": ds_id}

    @router.put("/api/admin/datasources/{ds_id}")
    async def update_ds(ds_id: int, req: DatasourcePatch) -> dict:
        ok = await mgr.update_datasource(ds_id, req.model_dump(exclude_none=True))
        if not ok:
            raise HTTPException(404, "数据源不存在")
        return {"ok": True}

    @router.delete("/api/admin/datasources/{ds_id}")
    async def delete_ds(ds_id: int) -> dict:
        ok = await mgr.delete_datasource(ds_id)
        if not ok:
            raise HTTPException(404, "数据源不存在")
        return {"ok": True}

    @router.post("/api/admin/datasources/{ds_id}/test")
    async def test_connection(ds_id: int) -> dict:
        try:
            await mgr.test_connection(ds_id)
            return {"ok": True}
        except KeyError:
            raise HTTPException(404, "数据源不存在")
        except Exception as e:
            raise HTTPException(400, f"连接失败: {e}")

    @router.post("/api/admin/datasources/{ds_id}/sync")
    async def sync_ds(ds_id: int) -> dict:
        try:
            sync_scope = await mgr.get_sync_scope(ds_id)
        except KeyError:
            raise HTTPException(404, "数据源不存在")
        engine = await mgr.get_engine(ds_id)
        return await sync_metadata(ds_id, engine, sync_scope)

    return router
