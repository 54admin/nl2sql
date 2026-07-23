"""会话列表/删除/改名路由。薄层：只调一层 SessionManager。
ponytail: 用户隔离仅靠 user_id 查询参数，鉴权层 P5 再补。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.memory.session import SessionManager


class RenameIn(BaseModel):
    title: str


def build_session_router(session_mgr: SessionManager) -> APIRouter:
    router = APIRouter()

    @router.get("/api/session")
    async def list_sessions(user_id: str):
        return {"sessions": await session_mgr.list_sessions(user_id)}

    @router.post("/api/session")
    async def create_session(user_id: str, channel: str = "web",
                             title: str | None = None):
        """新建会话，返回 session_id。title 可选（首问时由 ask 自动填）。"""
        sid = await session_mgr.create_session(user_id, channel, title=title)
        return {"session_id": sid, "user_id": user_id, "channel": channel}

    @router.get("/api/session/{sid}/messages")
    async def get_messages(sid: str):
        """取某会话历史消息（切会话时前端回填对话区用）。"""
        return {"messages": await session_mgr.get_messages(sid)}

    @router.patch("/api/session/{sid}")
    async def rename_session(sid: str, req: RenameIn):
        """改会话标题（侧边栏重命名）。"""
        if not req.title.strip():
            raise HTTPException(400, "标题不能为空")
        ok = await session_mgr.rename_session(sid, req.title.strip())
        if not ok:
            raise HTTPException(404, "会话不存在或已删除")
        return {"ok": True}

    @router.delete("/api/session/{sid}")
    async def delete_session(sid: str):
        """逻辑删除：返回是否真的删了。"""
        ok = await session_mgr.delete_session(sid)
        return {"ok": ok}

    return router
