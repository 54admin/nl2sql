"""admin LLM 配置路由：模型 CRUD（列表/新建/改/删/启停）。
一行=一个模型（base_url+key+model），用途是 purposes 多选（analysis/attribution 子集）。
启用互斥：启用某模型时，把它每个用途从其他 enabled 模型的 purposes 移除——同一用途同时只一个 enabled 模型覆盖。
ponytail: 鉴权层 P5 管理后台再补。"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory

PURPOSES = ("analysis", "attribution")

# 向量模型过滤：本系统不做 embedding（知识库走外部 RAGFlow，自带向量化），
# embed 类模型不能做对话/归因，发现时直接过滤掉，不返回给前端。
_EMBED_RE = re.compile(r"embed", re.I)


def _is_embed_model(model_id: str) -> bool:
    return bool(_EMBED_RE.search(model_id or ""))


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
        """新建/更新（upsert by id）。用途互斥：本模型用途变更时，把它的新用途从其他模型的 purposes 移除（模型不删，只移除冲突用途）。
        purposes 允许空（空=暂不参与任何场景，配置保留，如编辑网关地址时）——前端 toggle 仍拦「至少留一个」防误清空。
        只在用途真变了才跑互斥：编辑网关地址/key/协议（用途未变）不动其他模型，否则会误清别的网关模型的用途。"""
        if not all(p in PURPOSES for p in payload.purposes):
            raise HTTPException(400, f"purposes 必须是 {PURPOSES} 的子集")
        async with AsyncSessionFactory() as s:
            row = await s.get(LlmConfigRow, cfg_id)
            purposes_changed = row is None or set(row.purposes or []) != set(payload.purposes)
            if purposes_changed:
                # 用途互斥（一 purpose 一模型）：把本模型新用途从其他模型的 purposes 移除
                others = (await s.execute(select(LlmConfigRow).where(
                    LlmConfigRow.id != cfg_id))).scalars().all()
                for o in others:
                    overlap = set(o.purposes or []) & set(payload.purposes)
                    if overlap:
                        o.purposes = [p for p in (o.purposes or []) if p not in overlap]
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
            # 过滤向量模型：本系统不做 embedding（知识库走外部 RAGFlow），embed 模型不能做对话/归因
            models = sorted({m.id for m in resp.data if not _is_embed_model(m.id)})
            return {"models": models}
        except Exception as e:
            raise HTTPException(400, f"拉模型列表失败: {e}")

    return router
