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

    async def handle_message(self, user_id: str, session_id: str, text: str,
                             mode: ViewerMode, trace_id: str,
                             cancel_token: CancelToken | None = None
                             ) -> AsyncIterator[SSEEvent]:
        # 查会话状态：awaiting_clarification => 断点恢复（spec 6.4）
        sess = await self._sessions.get_session(session_id)
        is_resume = bool(sess and sess.get("status") == "awaiting_clarification")
        user_msg = text

        # 读 system prompt（prompt_store 为空则 None）
        system_prompt = None
        if self._prompts is not None:
            system_prompt = await self._prompts.get_active()
        # 注入当前日期：LLM 据此换算"本月/上月"等相对时间，否则会瞎猜年份（审计实证猜成去年）
        if system_prompt:
            _now = datetime.now()
            _wd = "一二三四五六日"[_now.weekday()]
            _ym = f"{_now.year}-{_now.month:02d}"
            system_prompt = (f"【当前日期】今天 {_now.year}-{_now.month:02d}-{_now.day:02d}"
                             f"（周{_wd}），当前年月 {_ym}。用户说\"本月/上月/最近\"等相对时间时据此换算。\n\n"
                             + system_prompt)
        # get_sql_template 强引导：模板改走工具按需取（不再全量塞 prompt 占上下文），靠这句让 LLM 主动调
        if system_prompt:
            system_prompt = system_prompt + (
                "\n\n【工具提示】写复杂查询（同比/环比/行转列/同行多指标对比排序）前，"
                "先调 get_sql_template 工具查看现成样板，按 usage 改表名/参数后用 execute_sql 执行；"
                "没有合适样板再自己写。")
        # 迭代 loop，透传事件；异常转 ERROR 不中断流
        # cancel_token 由路由层注入（前端取消则置位，loop 在检查点响应）。
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
