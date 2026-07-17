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


def test_user_hides_technical_details():
    hidden = ["metadata_lookup", "sql_generated", "knowledge_hit",
              "attribution_step",
              "turn_start", "assistant", "tool_call", "tool_result",
              "warning", "cancelled"]
    for t in hidden:
        ev = SSEEvent(type=t, data={}, trace_id="t")
        assert should_emit(ev, ViewerMode.USER) is False


def test_user_emits_friendly_events():
    friendly = ["correction", "clarification_needed", "plan",
                "answer_delta", "done", "error"]
    for t in friendly:
        ev = SSEEvent(type=t, data={}, trace_id="t")
        assert should_emit(ev, ViewerMode.USER) is True


def test_user_emits_visible_types():
    visible = ["correction", "clarification_needed", "plan", "todo_update",
               "query_progress", "intermediate", "answer_delta", "done", "error"]
    for t in visible:
        ev = SSEEvent(type=t, data={}, trace_id="t")
        assert should_emit(ev, ViewerMode.USER) is True


def test_filter_event_none_for_hidden():
    ev = SSEEvent(type="sql_generated", data={"sql": "select 1"}, trace_id="t")
    assert filter_event(ev, ViewerMode.USER) is None


def test_filter_event_passthrough_visible():
    ev = SSEEvent(type="answer_delta", data={"text": "hi"}, trace_id="t")
    out = filter_event(ev, ViewerMode.USER)
    assert out is ev


def test_filter_event_admin_passthrough_hidden():
    ev = SSEEvent(type="metadata_lookup", data={}, trace_id="t")
    out = filter_event(ev, ViewerMode.ADMIN)
    assert out is ev


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
