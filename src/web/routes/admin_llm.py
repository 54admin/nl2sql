"""admin LLM 配置路由：模型 CRUD（列表/新建/改/删/启停）。
一行=一个模型（base_url+key+model），用途是 purposes 多选（analysis/embedding/attribution 子集）。
启用互斥：启用某模型时，把它每个用途从其他 enabled 模型的 purposes 移除——同一用途同时只一个 enabled 模型覆盖。
ponytail: 鉴权层 P5 管理后台再补。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory

PURPOSES = ("analysis", "embedding", "attribution")


class LlmConfigPayload(BaseModel):
    purposes: list[str]
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


class DiscoverIn(BaseModel):
    """模型发现：传网关 base_url+api_key，后端调 /v1/models 列出支持的模型。"""
    base_url: str
    api_key: str
    protocol: str = "openai"


def _row_to_dict(r) -> dict:
    return {"id": r.id, "purposes": r.purposes or [], "model": r.model, "base_url": r.base_url,
            "api_key": r.api_key, "temperature": r.temperature,
            "timeout": r.timeout, "max_context": r.max_context,
            "protocol": r.protocol or "openai",
            "rpm_limit": r.rpm_limit, "concurrency": r.concurrency,
            "enabled": r.enabled, "version": r.version}


def _apply_payload(row, payload: LlmConfigPayload) -> None:
    proto = (payload.protocol or "openai").lower()
    if proto not in ("openai", "anthropic"):
        proto = "openai"
    row.purposes = payload.purposes
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
            rows = (await s.execute(select(LlmConfigRow).order_by(
                LlmConfigRow.base_url, LlmConfigRow.model))).scalars().all()
        return {"configs": [_row_to_dict(r) for r in rows]}

    @router.put("/api/admin/llm-config/{cfg_id}")
    async def upsert_config(cfg_id: str, payload: LlmConfigPayload) -> dict:
        """新建/更新（upsert by id）。启用互斥：启用本模型时，把它用途从其他 enabled 模型的 purposes 移除（模型不删，只移除冲突用途）。"""
        if not payload.purposes or not all(p in PURPOSES for p in payload.purposes):
            raise HTTPException(400, f"purposes 必须是 {PURPOSES} 的非空子集")
        async with AsyncSessionFactory() as s:
            # 互斥（一用途一启用）：启用本模型时，把它每个用途从其他 enabled 模型的 purposes 移除
            if payload.enabled:
                others = (await s.execute(select(LlmConfigRow).where(
                    LlmConfigRow.enabled.is_(True), LlmConfigRow.id != cfg_id))).scalars().all()
                for o in others:
                    overlap = set(o.purposes or []) & set(payload.purposes)
                    if overlap:
                        o.purposes = [p for p in (o.purposes or []) if p not in overlap]
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

    @router.post("/api/admin/llm-config/discover")
    async def discover_models(req: DiscoverIn) -> dict:
        """调网关 GET /v1/models 列出支持的模型（openai 兼容）。配置页"发现+导入"用。"""
        from openai import AsyncOpenAI
        base = (req.base_url or "").rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        try:
            client = AsyncOpenAI(api_key=req.api_key, base_url=base, timeout=15)
            resp = await client.models.list()
            return {"models": sorted({m.id for m in resp.data})}
        except Exception as e:
            raise HTTPException(400, f"拉模型列表失败: {e}")

    return router
