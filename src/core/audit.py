"""审计落库器：把 loop 的每个 SSE 事件落 audit_events 表（细粒度复盘），
trace 结束时落 audit_traces 一行（成败/耗时/最终答案汇总）。

落库异步、失败不抛——审计挂了不该毁掉问数主链路。
answer_delta 流式片段合并成一条 answer/reasoning 存（不存碎片，省行）。"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from src.logging import get_logger
from src.storage.models import AuditEvent, AuditTrace
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)


@dataclass
class _TurnBuffer:
    """单轮缓冲：把 answer_delta 碎片合并成一条 answer。"""
    answer: str = ""


@dataclass
class _TraceState:
    """单个 trace 的累积状态。AuditSink 不再用单例共享 buffer——每个 trace 一份，
    并发 run 互不串台。根治"审计里没有记录"：旧实现 begin() 会清空在跑 trace 的事件，
    第二个 run 一 begin 就把第一个冲没了。"""
    trace_id: str
    session_id: str
    user_id: str
    raw_input: str
    start_ts: float
    seq: int = 0
    events: list[dict] = field(default_factory=list)
    turn_buf: dict[int, _TurnBuffer] = field(default_factory=dict)
    sqls: list[str] = field(default_factory=list)
    result_ids: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)


class AuditSink:
    """落库器。每个 trace 独立 _TraceState（按 trace_id 隔离），并发 run 互不干扰。
    落库异步、失败不抛——审计挂了不该毁掉问数主链路。answer_delta 碎片合并成一条 answer。"""

    def __init__(self):
        self._traces: dict[str, _TraceState] = {}

    def begin(self, trace_id: str, session_id: str, user_id: str,
              raw_input: str, normalized: str | None = None) -> None:
        """trace 开始：为该 trace_id 建独立状态 + 记原始输入 + 启动计时。
        不再清空全局状态（按 trace_id 隔离），并发 run 各自独立。"""
        st = _TraceState(trace_id=trace_id, session_id=session_id, user_id=user_id,
                         raw_input=raw_input, start_ts=time.monotonic())
        self._traces[trace_id] = st
        self._append(st, "user_input", None, {"raw": raw_input, "normalized": normalized})

    def event(self, trace_id: str, evt_type: str, data: dict, turn: int | None = None) -> None:
        """记录一个 SSE 事件。answer_delta 合并进轮缓冲，不单独落库。"""
        st = self._traces.get(trace_id)
        if st is None:
            return   # 该 trace 未 begin（测试关审计/异常），静默跳过
        if evt_type == "answer_delta":
            buf = st.turn_buf.setdefault(turn or 0, _TurnBuffer())
            buf.answer += data.get("text", "")
            return
        if evt_type == "tool_call":
            tc = {"name": data.get("name"), "args": data.get("args", {}),
                  "id": data.get("id"), "turn": turn}
            st.tool_calls.append(tc)
            if data.get("name") == "execute_sql":
                sql = (data.get("args") or {}).get("sql")
                if sql:
                    st.sqls.append(sql)
            self._append(st, "tool_call", turn, tc)
            return
        if evt_type == "tool_result":
            rid = data.get("result_id")
            if rid:
                st.result_ids.append(rid)
            self._append(st, "tool_result", turn, {"name": data.get("name"),
                                                   "summary": data.get("summary"),
                                                   "result_id": rid,
                                                   "converged": data.get("converged")})
            self._flush_turn(st, turn or 0)
            return
        if evt_type == "clarification_needed":
            self._append(st, "clarification", turn, {"question": data.get("question")})
            return
        if evt_type == "turn_start":
            self._append(st, "turn_start", data.get("turn"), {})
            return
        self._append(st, evt_type, turn, data)

    def _flush_turn(self, st: _TraceState, turn: int) -> None:
        """轮结束：把合并的 answer 文本落一行（不存流式碎片）。"""
        buf = st.turn_buf.pop(turn, None)
        if buf and buf.answer:
            self._append(st, "answer", turn, {"text": buf.answer})

    async def finalize(self, success: bool, final_answer: str,
                       trace_id: str) -> None:
        """trace 结束：落剩余轮缓冲 + audit_traces 汇总行 + audit_events 全部事件。
        按 trace_id 取出独立状态后即从内存移除（防泄漏）。失败不抛。"""
        st = self._traces.pop(trace_id, None)
        if st is None:
            return
        for turn, buf in st.turn_buf.items():
            if buf.answer:
                self._append(st, "answer", turn, {"text": buf.answer})
        st.turn_buf.clear()

        elapsed_ms = int((time.monotonic() - st.start_ts) * 1000) if st.start_ts else None
        self._append(st, "done" if success else "error", None,
                     {"answer": final_answer, "success": success})

        try:
            async with AsyncSessionFactory() as s:
                s.add(AuditTrace(
                    trace_id=st.trace_id, session_id=st.session_id,
                    user_id=st.user_id, raw_input=st.raw_input,
                    tool_calls_json=json.dumps(st.tool_calls, ensure_ascii=False),
                    sql_text="\n;\n".join(st.sqls) if st.sqls else None,
                    result_id=st.result_ids[-1] if st.result_ids else None,
                    elapsed_ms=elapsed_ms, success=success, final_answer=final_answer))
                for e in st.events:
                    s.add(AuditEvent(trace_id=st.trace_id, seq=e["seq"],
                                     event_type=e["event_type"], turn=e.get("turn"),
                                     content_json=json.dumps(e.get("content"),
                                                             ensure_ascii=False,
                                                             default=str) if e.get("content") is not None else None))
                await s.commit()
        except Exception as e:
            log.warning("审计落库失败（忽略，不影响主链路）: %s", e)

    def _append(self, st: _TraceState, event_type: str, turn: int | None,
                content: dict | None) -> None:
        st.seq += 1
        st.events.append({"seq": st.seq, "event_type": event_type,
                          "turn": turn, "content": content})
