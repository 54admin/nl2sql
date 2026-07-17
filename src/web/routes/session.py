"""会话列表/删除路由。薄层：只调一层 SessionManager。
ponytail: 用户隔离仅靠 user_id 查询参数，鉴权层 P5 再补。"""
from __future__ import annotations

from fastapi import APIRouter

from src.memory.session import SessionManager


def build_session_router(session_mgr: SessionManager) -> APIRouter:
    router = APIRouter()

    @router.get("/api/session")
    async def list_sessions(user_id: str):
        return {"sessions": await session_mgr.list_sessions(user_id)}

    @router.delete("/api/session/{sid}")
    async def delete_session(sid: str):
        await session_mgr.delete_session(sid)  # 幂等
        return {"ok": True}

    return router
