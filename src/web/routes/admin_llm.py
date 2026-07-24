"""admin LLM 配置路由：模型 CRUD（列表/新建/改/删/启停）。
每个模型有 purpose（analysis/embedding/attribution）+ 自定义 id；同 purpose 可多个（备用切换），
LLMService 按 purpose 取 enabled 的（version 最新）。
ponytail: 鉴权层 P5 管理后台再补。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory

PURPOSES = ("analysis", "embedding", "attribution")


class LlmConfigPayload(BaseModel):
    purpose: str
    model: str
    base_url: str
    api_key: str = ""
    temperature: float = 0.0
    timeout: int = 60
    max_context: int = 32000
    protocol: str = "openai"
    rpm_limit: int | None = None
    concurrency: int | None = None
    enabled: bool = True


def _row_to_dict(r) -> dict:
    return {"id": r.id, "purpose": r.purpose, "model": r.model, "base_url": r.base_url,
            "api_key": r.api_key, "temperature": r.temperature,
            "timeout": r.timeout, "max_context": r.max_context,
            "protocol": r.protocol or "openai",
            "rpm_limit": r.rpm_limit, "concurrency": r.concurrency,
            "enabled": r.enabled, "version": r.version}


def _apply_payload(row, payload: LlmConfigPayload) -> None:
    proto = (payload.protocol or "openai").lower()
    if proto not in ("openai", "anthropic"):
        proto = "openai"
    row.purpose = payload.purpose
    row.model = payload.model
    row.base_url = payload.base_url
    row.api_key = payload.api_key
    row.temperature = payload.temperature
    row.timeout = payload.timeout
    row.max_context = payload.max_context
    row.protocol = proto
    row.rpm_limit = payload.rpm_limit
    row.concurrency = payload.concurrency
    row.enabled = payload.enabled


def build_admin_llm_router(llm_service=None) -> APIRouter:
    """构造 admin LLM 配置路由。llm_service: 改完调 reset_dynamic 热更新。"""
    router = APIRouter()

    @router.get("/api/admin/llm-config")
    async def list_configs() -> dict:
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(LlmConfigRow.__table__.select())).all()
        return {"configs": [_row_to_dict(r) for r in rows]}

    @router.put("/api/admin/llm-config/{cfg_id}")
    async def upsert_config(cfg_id: str, payload: LlmConfigPayload) -> dict:
        """新建/更新（upsert by id）。同 purpose 多个时取 version 最新的 enabled 生效。"""
        if payload.purpose not in PURPOSES:
            raise HTTPException(400, f"purpose 必须是 {PURPOSES} 之一")
        async with AsyncSessionFactory() as s:
            row = await s.get(LlmConfigRow, cfg_id)
            if row is None:
                row = LlmConfigRow(id=cfg_id, version=0)
                _apply_payload(row, payload)
                row.version = 1
                s.add(row)
                version = 1
            else:
                _apply_payload(row, payload)
                row.version += 1
                version = row.version
            await s.commit()
        if llm_service is not None:
            llm_service.reset_dynamic()
        return {"ok": True, "id": cfg_id, "version": version}

    @router.delete("/api/admin/llm-config/{cfg_id}")
    async def delete_config(cfg_id: str) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(LlmConfigRow, cfg_id)
            if row is None:
                raise HTTPException(404, "配置不存在")
            await s.delete(row)
            await s.commit()
        if llm_service is not None:
            llm_service.reset_dynamic()
        return {"ok": True}

    return router
