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
    """单轮缓冲：把 answer_delta 碎片合并成一条 answer；reasoning 也合并。"""
    answer: str = ""
    reasoning: str = ""


class AuditSink:
    """落库器。每事件 append（内存累积 + 异步批量写），trace 结束 finalize 落汇总行。
    ponytail: 简单实现——每事件一次 INSERT，不批量；问数低频，行数可控。
    异常吞掉（审计非关键路径），只 warn。"""

    def __init__(self):
        self._seq: int = 0
        self._events: list[dict] = []   # 待写事件（按 seq）
        self._turn_buf: dict[int, _TurnBuffer] = {}
        self._start_ts: float | None = None
        self._raw_input: str = ""
        self._normalized_input: str | None = None
        self._sqls: list[str] = []
        self._result_ids: list[str] = []
        self._tool_calls: list[dict] = []

    def begin(self, trace_id: str, session_id: str, user_id: str,
              raw_input: str, normalized_input: str | None = None) -> None:
        """trace 开始：重置所有累积状态 + 记原始输入 + 启动计时。

        AuditSink 是单例（一个实例服务所有提问），begin 必须清空上一次 trace 的
        _events/_seq/_sqls 等，否则新 trace 会带上历史提问的事件 —— 表现为「问数详情
        里混入别的问题」。这是详情串台的根因。"""
        self._trace_id = trace_id
        self._session_id = session_id
        self._user_id = user_id
        self._raw_input = raw_input
        self._normalized_input = normalized_input
        self._start_ts = time.monotonic()
        self._seq = 0
        self._events = []
        self._turn_buf = {}
        self._sqls = []
        self._result_ids = []
        self._tool_calls = []
        self._append("user_input", None, {"raw": raw_input,
                                          "normalized": normalized_input})

    def event(self, trace_id: str, evt_type: str, data: dict, turn: int | None = None) -> None:
        """记录一个 SSE 事件。answer_delta 合并进轮缓冲，不单独落库。"""
        if evt_type == "answer_delta":
            # 合并到当前轮缓冲，turn 结束时统一落
            buf = self._turn_buf.setdefault(turn or 0, _TurnBuffer())
            buf.answer += data.get("text", "")
            return
        if evt_type == "tool_call":
            tc = {"name": data.get("name"), "args": data.get("args", {}),
                  "id": data.get("id"), "turn": turn}
            self._tool_calls.append(tc)
            # execute_sql 的 SQL 单独抽出来供统计
            if data.get("name") == "execute_sql":
                sql = (data.get("args") or {}).get("sql")
                if sql:
                    self._sqls.append(sql)
            self._append("tool_call", turn, tc)
            return
        if evt_type == "tool_result":
            rid = data.get("result_id")
            if rid:
                self._result_ids.append(rid)
            self._append("tool_result", turn, {"name": data.get("name"),
                                               "summary": data.get("summary"),
                                               "result_id": rid,
                                               "converged": data.get("converged")})
            # turn 结束：把合并的 answer 落一行
            self._flush_turn(turn or 0)
            return
        if evt_type == "clarification_needed":
            self._append("clarification", turn, {"question": data.get("question")})
            return
        if evt_type == "turn_start":
            self._append("turn_start", data.get("turn"), {})
            return
        # 其他事件（warning/answer 等）原样记
        self._append(evt_type, turn, data)

    def _flush_turn(self, turn: int) -> None:
        """轮结束：把合并的 answer 文本落一行（不存流式碎片）。"""
        buf = self._turn_buf.pop(turn, None)
        if buf and buf.answer:
            self._append("answer", turn, {"text": buf.answer})

    async def finalize(self, success: bool, final_answer: str) -> None:
        """trace 结束：落剩余轮缓冲 + audit_traces 汇总行 + audit_events 全部事件。
        失败不抛。"""
        # 落剩余未 flush 的轮
        for turn, buf in self._turn_buf.items():
            if buf.answer:
                self._append("answer", turn, {"text": buf.answer})
        self._turn_buf.clear()

        elapsed_ms = int((time.monotonic() - self._start_ts) * 1000) if self._start_ts else None
        # 终态事件
        self._append("done" if success else "error", None,
                     {"answer": final_answer, "success": success})

        try:
            async with AsyncSessionFactory() as s:
                # 汇总行
                s.add(AuditTrace(
                    trace_id=self._trace_id, session_id=self._session_id,
                    user_id=self._user_id, raw_input=self._raw_input,
                    normalized_input=self._normalized_input,
                    tool_calls_json=json.dumps(self._tool_calls, ensure_ascii=False),
                    sql_text="\n;\n".join(self._sqls) if self._sqls else None,
                    result_id=self._result_ids[-1] if self._result_ids else None,
                    sse_log_json=None, elapsed_ms=elapsed_ms,
                    cost_tokens=None, success=success, final_answer=final_answer))
                # 事件流
                for e in self._events:
                    s.add(AuditEvent(trace_id=self._trace_id, seq=e["seq"],
                                     event_type=e["event_type"], turn=e.get("turn"),
                                     content_json=json.dumps(e.get("content"),
                                                             ensure_ascii=False,
                                                             default=str) if e.get("content") is not None else None))
                await s.commit()
        except Exception as e:
            log.warning("审计落库失败（忽略，不影响主链路）: %s", e)

    def _append(self, event_type: str, turn: int | None, content: dict | None) -> None:
        self._seq += 1
        self._events.append({"seq": self._seq, "event_type": event_type,
                             "turn": turn, "content": content})
