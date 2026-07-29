"""admin 飞书通道配置路由：单配置 GET/PUT，改完调 adapter.reload() 热重连。

对齐 admin_llm 的动态配置范式：配置走数据库 feishu_config 表，admin 后台改，
热重连（不重启服务）。单行配置（id=default），不搞多行。
ponytail: 鉴权层 P5 管理后台再补。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.logging import get_logger
from src.storage.models import FeishuConfigRow
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)

DEFAULT_ID = "default"


class FeishuConfigPayload(BaseModel):
    app_id: str = ""
    app_secret: str = ""
    whitelist: list[str] = []
    card_throttle_ms: int = 300
    enabled: bool = False


def _row_to_dict(r) -> dict:
    return {"id": r.id, "app_id": r.app_id, "app_secret": r.app_secret,
            "whitelist": r.whitelist or [], "card_throttle_ms": r.card_throttle_ms,
            "enabled": r.enabled, "version": r.version}


def build_admin_feishu_router(adapter=None) -> APIRouter:
    """构造 admin 飞书配置路由。adapter：改完调 reload() 热重连（_Lazy 注入，可 None）。"""
    router = APIRouter()

    @router.get("/api/admin/feishu-config")
    async def get_config() -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(FeishuConfigRow, DEFAULT_ID)
        return _row_to_dict(row) if row else {
            "id": DEFAULT_ID, "app_id": "", "app_secret": "", "whitelist": [],
            "card_throttle_ms": 300, "enabled": False, "version": 0}

    @router.put("/api/admin/feishu-config")
    async def save_config(payload: FeishuConfigPayload) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(FeishuConfigRow, DEFAULT_ID)
            if row is None:
                row = FeishuConfigRow(id=DEFAULT_ID, version=0)
                s.add(row)
            row.app_id = payload.app_id.strip()
            row.app_secret = payload.app_secret.strip()
            row.whitelist = payload.whitelist
            row.card_throttle_ms = payload.card_throttle_ms
            row.enabled = payload.enabled
            row.version += 1
            await s.commit()
            version = row.version
        if adapter is not None:
            try:
                await adapter.reload()    # 热重连：stop 老 ws + 读新配置 start
            except Exception as e:
                log.warning("飞书热重连跳过（通道未启用或失败，配置已存库）: %s", e)
        return {"ok": True, "version": version}

    return router
