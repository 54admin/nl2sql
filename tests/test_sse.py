import json

from src.core.types import SSEEvent
from src.web.sse import (
    SSEEventType, ViewerMode,
    filter_event, format_sse, should_emit,
)


def test_admin_emits_all_types():
    for t in SSEEventType:
        ev = SSEEvent(type=t.value, data={}, trace_id="t")
        assert should_emit(ev, ViewerMode.ADMIN) is True


def test_all_types_visible_no_mode_filter():
    """不分模式：所有事件类型都透传（含原被 user 隐藏的 tool_call/sql_generated 等）。"""
    for t in SSEEventType:
        ev = SSEEvent(type=t.value, data={}, trace_id="t")
        assert should_emit(ev, ViewerMode.USER) is True


def test_filter_event_passthrough_all():
    """过滤恒透传：无论 user/admin，所有事件原样返回。"""
    for t in SSEEventType:
        ev = SSEEvent(type=t.value, data={}, trace_id="t")
        assert filter_event(ev, ViewerMode.USER) is ev
        assert filter_event(ev, ViewerMode.ADMIN) is ev


def test_format_sse_structure():
    ev = SSEEvent(type="answer_delta", data={"text": "你好"}, trace_id="abc123")
    out = format_sse(ev)
    assert out.startswith("event: answer_delta\n")
    assert out.endswith("\n\n")
    data_line = out.split("\n")[1].removeprefix("data: ")
    payload = json.loads(data_line)
    assert payload["data"] == {"text": "你好"}
    assert payload["trace_id"] == "abc123"


def test_format_sse_unicode_not_escaped():
    ev = SSEEvent(type="answer_delta", data={"text": "你好世界"}, trace_id="t")
    out = format_sse(ev)
    assert "你好世界" in out
    assert "\\u" not in out


def test_format_sse_done():
    ev = SSEEvent(type="done", data={"answer": "结果"}, trace_id="t1")
    out = format_sse(ev)
    assert "event: done\n" in out
