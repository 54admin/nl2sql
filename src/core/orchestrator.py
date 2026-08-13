"""编排入口：查状态分流 → 读 system prompt → 透传 loop 事件（spec 6.1/6.4）。
- resume（awaiting_clarification）：user_msg 原样进 loop（断点恢复，spec 6.4）
- system prompt：PromptStore 读 default 场景透传 loop.run(system_prompt=...)
- loop 异常转 ERROR 事件，不中断流
orchestrator 只读会话状态（判 is_resume），状态转移由 loop 内部 SessionState 驱动。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

from src.core.types import CancelToken, SSEEvent
from src.logging import get_logger
from src.memory.session import SessionManager
from src.web.sse import SSEEventType, ViewerMode

log = get_logger(__name__)


class Orchestrator:
    """编排入口。组合 agent_loop + session_manager + prompt_store(可选)。"""

    def __init__(self, loop, sessions: SessionManager,
                 prompt_store=None, audit=None):
        self._loop = loop
        self._sessions = sessions
        self._prompts = prompt_store
        self._audit = audit
        # 会话忙时闸门：正在跑的 session_id 集合。同 session 已有 run 在跑→直接拒，
        # 防两个 run 重叠（重叠会冲掉审计、白费算力）。真正的并发防护在这，不靠消息级去重。
        # 不靠 DB status（RUNNING 只 resume 路径写，普通问数不置位，不可靠）——内存显式管。
        self._running: set[str] = set()

    async def handle_message(self, user_id: str, session_id: str, text: str,
                             mode: ViewerMode, trace_id: str,
                             cancel_token: CancelToken | None = None
                             ) -> AsyncIterator[SSEEvent]:
        # 会话忙时闸门：同一 session 已有 run 在跑 → 立即拒绝，绝不并发跑第二个。
        # 防两个 run 重叠（重叠会让审计 begin() 互冲丢记录、并白费算力）。
        # 拒绝也要落审计：否则被拒问题在统计页完全不可见——begin+finalize 一条失败记录。
        if session_id in self._running:
            log.warning("会话忙，拒绝并发 run sid=%s trace=%s", session_id, trace_id)
            if self._audit is not None:
                try:
                    self._audit.begin(trace_id, session_id, user_id, text)
                    self._audit.event(trace_id, "error",
                                      {"message": "会话忙，拒绝并发（上一条还在处理中）"})
                    await self._audit.finalize(False, "⚠ 上一条还在处理中，请等它完成或先取消，再发送新问题。", trace_id)
                except Exception as ex:
                    log.warning("审计记录被拒请求失败（忽略）: %s", ex)
            yield SSEEvent(SSEEventType.ERROR.value,
                           {"message": "上一条还在处理中，请等它完成或先取消，再发送新问题。"},
                           trace_id)
            return
        self._running.add(session_id)
        try:
            # 查会话状态：awaiting_clarification => 断点恢复（spec 6.4）
            sess = await self._sessions.get_session(session_id)
            is_resume = bool(sess and sess.get("status") == "awaiting_clarification")
            user_msg = text

            # 读 system prompt：内核协议 + 所有 always-on skill（PromptStore.assemble_system_prompt 组装）。
            system_prompt = None
            if self._prompts is not None:
                system_prompt = await self._prompts.assemble_system_prompt()
            if system_prompt:
                _now = datetime.now()
                _wd = "一二三四五六日"[_now.weekday()]
                _ym = f"{_now.year}-{_now.month:02d}"
                system_prompt = system_prompt + (
                    f"\n\n【当前日期】今天 {_now.year}-{_now.month:02d}-{_now.day}"
                    f"（周{_wd}），当前年月 {_ym}。用户说\"本月/上月/最近\"等相对时间时据此换算。")
            if cancel_token is None:
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
        finally:
            self._running.discard(session_id)
