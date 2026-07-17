"""admin LLM 配置路由：GET/PUT /api/admin/llm-config。
ponytail: 鉴权层 P5 管理后台再补；P0b 暴露路由供页面调试。
协议固定 OpenAI 兼容（base_url 可换），不做 Anthropic/Gemini 原生协议适配。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory

DEFAULT_ID = "default"


class LlmConfigPayload(BaseModel):
    model: str
    base_url: str
    api_key: str = ""
    temperature: float = 0.0
    timeout: int = 60
    enabled: bool = True


def build_admin_llm_router(llm_service=None) -> APIRouter:
    """构造 admin LLM 配置路由。
    llm_service: 可选 LLMService 实例，PUT 成功后调其 reset_dynamic 触发热更新。"""
    router = APIRouter()

    @router.get("/api/admin/llm-config")
    async def get_llm_config() -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(LlmConfigRow, DEFAULT_ID)
        if row is None:
            return {"source": "yaml", "dynamic": None}
        return {
            "source": "dynamic",
            "dynamic": {
                "model": row.model, "base_url": row.base_url,
                "api_key": row.api_key, "temperature": row.temperature,
                "timeout": row.timeout, "enabled": row.enabled,
                "version": row.version,
            },
        }

    @router.put("/api/admin/llm-config")
    async def put_llm_config(payload: LlmConfigPayload) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(LlmConfigRow, DEFAULT_ID)
            if row is None:
                s.add(LlmConfigRow(
                    id=DEFAULT_ID, model=payload.model, base_url=payload.base_url,
                    api_key=payload.api_key, temperature=payload.temperature,
                    timeout=payload.timeout, enabled=payload.enabled, version=1))
                version = 1
            else:
                row.model = payload.model
                row.base_url = payload.base_url
                row.api_key = payload.api_key
                row.temperature = payload.temperature
                row.timeout = payload.timeout
                row.enabled = payload.enabled
                row.version += 1
                version = row.version
            await s.commit()
        if llm_service is not None:
            llm_service.reset_dynamic()
        return {"ok": True, "version": version}

    return router
