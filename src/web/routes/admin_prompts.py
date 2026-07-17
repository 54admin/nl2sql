"""admin prompt 管理路由：CRUD /api/admin/prompts。
ponytail: 鉴权层 P5 管理后台再补；P0b 暴露路由供页面调试。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.core.prompt_store import PromptStore


class PromptPayload(BaseModel):
    scene: str
    content: str
    enabled: bool = True


def build_admin_prompts_router(store: PromptStore) -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/prompts")
    async def list_prompts() -> dict:
        return {"prompts": await store.list_all()}

    @router.get("/api/admin/prompts/{scene}")
    async def get_prompt(scene: str) -> dict:
        content = await store.get(scene)
        return {"scene": scene, "content": content}

    @router.post("/api/admin/prompts")
    async def create_prompt(payload: PromptPayload) -> dict:
        version = await store.upsert(payload.scene, payload.content, payload.enabled)
        return {"ok": True, "scene": payload.scene, "version": version}

    @router.put("/api/admin/prompts/{scene}")
    async def update_prompt(scene: str, payload: PromptPayload) -> dict:
        version = await store.upsert(scene, payload.content, payload.enabled)
        return {"ok": True, "scene": scene, "version": version}

    @router.delete("/api/admin/prompts/{scene}")
    async def delete_prompt(scene: str) -> dict:
        deleted = await store.delete(scene)
        return {"ok": True, "deleted": deleted}

    return router
