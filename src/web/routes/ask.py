"""POST /api/ask/sse 流式路由（spec 6.8 双模式过滤在路由层做）。
ponytail: 不引 sse-starlette，原生 StreamingResponse + format_sse 已是 SSE 文本。
取消：POST /api/ask/cancel?trace_id= 置位进程内 CancelToken；loop 在检查点响应。"""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.core.types import CancelToken
from src.web.sse import ViewerMode, filter_event, format_sse


class AskRequest(BaseModel):
    user_id: str
    session_id: str
    text: str
    # 字段类型直接用枚举，pydantic 收到非法字符串自动 422
    mode: ViewerMode = ViewerMode.USER


class AskSyncRequest(BaseModel):
    """同步问数（/api/ask，非 SSE）：供飞书智能体/Aily 插件等外部调用。
    session_id 空则自动建（首次），返回里带 session_id 供多轮带上次返回的。"""
    user_id: str
    text: str
    session_id: str | None = None
    mode: ViewerMode = ViewerMode.USER


# 进程内 trace_id → CancelToken 注册表，供取消端点置位。
# ponytail: 单进程内存即可（uvicorn 单 worker 内）；多 worker 部署需换 Redis 标志，
# 但本期单进程内网工具够用。流结束自动清理。
_running: dict[str, CancelToken] = {}


def build_ask_router(orchestrator) -> APIRouter:
    router = APIRouter()

    @router.post("/api/ask/sse")
    async def ask_sse(req: AskRequest):
        mode = req.mode  # 已是 ViewerMode 枚举
        trace_id = uuid4().hex
        cancel_token = CancelToken()
        _running[trace_id] = cancel_token
        # 首问填标题：会话无 title 时用首条问题截前 20 字，侧边栏即有可读标题。
        # 失败不影响问数主链路。
        try:
            await orchestrator._sessions.fill_title_if_empty(
                req.session_id, req.text[:20])
        except Exception:
            pass
        try:
            async def stream():
                try:
                    async for evt in orchestrator.handle_message(
                        req.user_id, req.session_id, req.text, mode, trace_id,
                        cancel_token=cancel_token,
                    ):
                        filtered = filter_event(evt, mode)
                        if filtered is not None:
                            yield format_sse(filtered)
                finally:
                    _running.pop(trace_id, None)
            return StreamingResponse(stream(), media_type="text/event-stream")
        except Exception:
            _running.pop(trace_id, None)
            raise

    @router.post("/api/ask/cancel")
    async def cancel_ask(trace_id: str) -> dict:
        """前端取消：置位对应 trace 的 CancelToken，loop 下一检查点响应。"""
        tok = _running.get(trace_id)
        if tok is None:
            raise HTTPException(404, "无此 trace 的运行中会话（可能已结束）")
        tok.cancel()
        return {"ok": True, "trace_id": trace_id}

    @router.post("/api/ask")
    async def ask_sync(req: AskSyncRequest) -> dict:
        """同步问数（非 SSE）：跑完返回最终答案；需要澄清返回 question+options。
        供飞书智能体/Aily 插件等外部调用。session_id 空则自动建并返回（多轮带上次返回的）。"""
        sid = req.session_id
        if not sid:
            sid = await orchestrator._sessions.create_session(req.user_id, "feishu")
        trace_id = uuid4().hex
        answer, clarify = "", None
        async for evt in orchestrator.handle_message(
            req.user_id, sid, req.text, req.mode, trace_id, cancel_token=CancelToken()
        ):
            if evt.type == "done":
                answer = evt.data.get("answer", "")
            elif evt.type == "clarification_needed":
                clarify = {"question": evt.data.get("question"), "options": evt.data.get("options")}
            elif evt.type == "error":
                return {"session_id": sid, "error": evt.data.get("message", "出错")}
        if clarify:
            return {"session_id": sid, "need_clarify": True,
                    "question": clarify["question"], "options": clarify.get("options")}
        return {"session_id": sid, "answer": answer}

    return router
