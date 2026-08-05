"""admin prompt 管理路由：CRUD /api/admin/prompts（skill 单一真相源）。
写操作（POST/PUT/DELETE）后热刷新：失效 PromptStore 装配缓存 + 重建 registry 调 loop.reload_registry。
loop_ref 可选——单测/无 AgentLoop 时跳过热刷新（写库仍成功，下次请求走新装配）。"""""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.core.prompt_store import PromptStore


class PromptPayload(BaseModel):
    scene: str = ""
    content: str = ""
    tools: list[str] = []
    mode: str = "always_on"
    order: int = 99
    enabled: bool = True


def build_admin_prompts_router(store: PromptStore, loop_ref=None) -> APIRouter:
    """store=PromptStore；loop_ref=AgentLoop（_Lazy），写操作后热刷新工具集。"""
    router = APIRouter()

    async def _hot_reload() -> None:
        """写库后：失效装配缓存 + 重建 registry + loop.reload_registry。失败不回滚（重启兜底）。"""
        if loop_ref is None:
            return
        try:
            from src.tools.catalog import build_registry
            from src.tools.sql_template import build_template_desc, list_enabled_templates
            desc = build_template_desc(await list_enabled_templates())
            reg = await build_registry(sql_template_desc=desc, prompt_store=store)
            loop_ref.reload_registry(reg)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "skill 热刷新失败（写库已成功，重启后生效）: %s", e)

    @router.get("/api/admin/prompts")
    async def list_prompts() -> dict:
        return {"prompts": await store.list_all()}

    @router.get("/api/admin/prompts/{scene}")
    async def get_prompt(scene: str) -> dict:
        content = await store.get(scene)
        return {"scene": scene, "content": content}

    @router.post("/api/admin/prompts")
    async def create_prompt(payload: PromptPayload) -> dict:
        version = await store.upsert(payload.scene, payload.content,
                                     tools=payload.tools, mode=payload.mode,
                                     order=payload.order, enabled=payload.enabled)
        await _hot_reload()
        return {"ok": True, "scene": payload.scene, "version": version}

    @router.put("/api/admin/prompts/{scene}")
    async def update_prompt(scene: str, payload: PromptPayload) -> dict:
        version = await store.upsert(scene, payload.content,
                                     tools=payload.tools, mode=payload.mode,
                                     order=payload.order, enabled=payload.enabled)
        await _hot_reload()
        return {"ok": True, "scene": scene, "version": version}

    @router.delete("/api/admin/prompts/{scene}")
    async def delete_prompt(scene: str) -> dict:
        deleted = await store.delete(scene)
        await _hot_reload()
        return {"ok": True, "deleted": deleted}

    return router
