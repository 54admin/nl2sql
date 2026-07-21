"""数据源管理路由：CRUD + 连通性测试 + 库列表 + 按库元数据同步。P1a。

DBeaver 层级范式：
- 数据源 = 连接（实例）；db_name 可空（建源不指定库）。
- GET /schemas 拉库列表；POST /sync body 带 schema_name 同步指定库。
"""
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
    db_name: str | None = None       # 空=连实例（多库导航）；非空=兼容老数据连指定库
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


class SyncIn(BaseModel):
    schema_name: str | None = None   # 空=老行为（连的什么库拉什么库）；非空=拉指定库


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

    @router.get("/api/admin/datasources/{ds_id}/schemas")
    async def list_schemas(ds_id: int) -> dict:
        """拉数据源下所有业务库（schema）名——DBeaver 层级第一跳。"""
        try:
            schemas = await mgr.list_schemas(ds_id)
        except KeyError:
            raise HTTPException(404, "数据源不存在")
        except Exception as e:
            raise HTTPException(400, f"拉库列表失败: {e}")
        return {"schemas": schemas}

    @router.post("/api/admin/datasources/{ds_id}/sync")
    async def sync_ds(ds_id: int, req: SyncIn | None = None) -> dict:
        """同步指定库（body schema_name）的表+视图元数据。schema_name 空=兼容老行为。"""
        try:
            sync_scope = await mgr.get_sync_scope(ds_id)
        except KeyError:
            raise HTTPException(404, "数据源不存在")
        schema_name = req.schema_name if req else None
        try:
            engine = await mgr.get_engine(ds_id)
            return await sync_metadata(ds_id, engine, sync_scope, schema_name)
        except KeyError:
            raise HTTPException(404, "数据源不存在")
        except Exception as e:
            raise HTTPException(400, f"同步失败: {e}")

    return router
