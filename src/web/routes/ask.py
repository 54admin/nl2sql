"""POST /api/ask/sse 流式路由（spec 6.8 双模式过滤在路由层做）。
ponytail: 不引 sse-starlette，原生 StreamingResponse + format_sse 已是 SSE 文本。"""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.web.sse import ViewerMode, filter_event, format_sse


class AskRequest(BaseModel):
    user_id: str
    session_id: str
    text: str
    # 字段类型直接用枚举，pydantic 收到非法字符串自动 422
    mode: ViewerMode = ViewerMode.USER


def build_ask_router(orchestrator) -> APIRouter:
    router = APIRouter()

    @router.post("/api/ask/sse")
    async def ask_sse(req: AskRequest):
        mode = req.mode  # 已是 ViewerMode 枚举
        trace_id = uuid4().hex

        async def stream():
            async for evt in orchestrator.handle_message(
                req.user_id, req.session_id, req.text, mode, trace_id,
            ):
                filtered = filter_event(evt, mode)
                if filtered is not None:
                    yield format_sse(filtered)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return router
