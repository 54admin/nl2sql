"""编排入口：纠错前置 → 查状态分流 → 读 system prompt → 透传 loop 事件（spec 6.1/6.3/6.4）。
- 非 resume：normalizer 前置（有修正发 correction 事件）→ loop
- resume（awaiting_clarification）：跳过纠错，user_msg 原样进 loop（断点恢复，spec 6.4）
- system prompt：可选 PromptStore（Task 9 集成），读 default 场景透传 loop.run(system_prompt=...)
- 双模式过滤在路由层做，orchestrator 总 yield 全量事件
- loop 异常转 ERROR 事件，不中断流
orchestrator 只读会话状态（判 is_resume），状态转移由 loop 内部 SessionState 驱动。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict

from src.core.types import CancelToken, SSEEvent
from src.logging import get_logger
from src.memory.session import SessionManager
from src.web.sse import SSEEventType, ViewerMode

log = get_logger(__name__)


class Orchestrator:
    """编排入口。组合 normalizer + agent_loop + session_manager + prompt_store(可选)。"""

    def __init__(self, normalizer, loop, sessions: SessionManager,
                 prompt_store=None):
        self._normalizer = normalizer
        self._loop = loop
        self._sessions = sessions
        self._prompts = prompt_store

    async def handle_message(self, user_id: str, session_id: str, text: str,
                             mode: ViewerMode, trace_id: str
                             ) -> AsyncIterator[SSEEvent]:
        # 1. 查会话状态：awaiting_clarification => 断点恢复，跳过纠错（spec 6.4）
        sess = await self._sessions.get_session(session_id)
        is_resume = bool(sess and sess.get("status") == "awaiting_clarification")

        # 2. 名称纠错前置（仅新轮；恢复轮不重走纠错/意图识别，spec 6.4）
        if is_resume:
            user_msg = text
        else:
            user_msg, corrections = await self._normalizer.normalize(text)
            if corrections:
                yield SSEEvent(
                    SSEEventType.CORRECTION.value,
                    {"original": text, "normalized": user_msg,
                     "corrections": [asdict(c) for c in corrections]},
                    trace_id)

        # 3. 读 system prompt（Task 9 prompts 集成点；prompt_store 为空则 None）
        system_prompt = None
        if self._prompts is not None:
            system_prompt = await self._prompts.get("default")

        # 4. 迭代 loop，透传事件；异常转 ERROR 不中断流
        cancel_token = CancelToken()
        try:
            async for evt in self._loop.run(
                session_id=session_id, user_id=user_id,
                user_msg=user_msg, trace_id=trace_id,
                cancel_token=cancel_token, is_resume=is_resume,
                system_prompt=system_prompt,
            ):
                yield evt
        except Exception as e:
            log.exception("loop 执行异常 trace=%s", trace_id)
            yield SSEEvent(SSEEventType.ERROR.value,
                           {"message": str(e)}, trace_id)
