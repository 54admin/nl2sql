import asyncio
import pytest

from src.core.types import (
    CancelToken, LoopContext, ToolResult, SSEEvent, ToolDefinition,
)


# ---- CancelToken ----
def test_cancel_token_default_not_cancelled():
    tk = CancelToken()
    assert tk.cancelled is False


def test_cancel_token_cancel_sets_flag():
    tk = CancelToken()
    tk.cancel()
    assert tk.cancelled is True


def test_cancel_token_check_silent_when_not_cancelled():
    tk = CancelToken()
    tk.check()  # 不抛异常即通过


def test_cancel_token_check_raises_when_cancelled():
    tk = CancelToken()
    tk.cancel()
    with pytest.raises(asyncio.CancelledError):
        tk.check()


# ---- LoopContext ----
def test_loop_context_default_channel():
    ctx = LoopContext(session_id="s", user_id="u", trace_id="t")
    assert ctx.channel == "web"


def test_loop_context_custom_channel():
    ctx = LoopContext(session_id="s", user_id="u", trace_id="t", channel="feishu")
    assert ctx.channel == "feishu"


# ---- ToolResult ----
def test_tool_result_defaults():
    r = ToolResult(summary="hi")
    assert r.result_id is None
    assert r.finished is False
    assert r.suspended is False


def test_tool_result_finished():
    r = ToolResult(summary="done", finished=True)
    assert r.finished is True


def test_tool_result_suspended():
    r = ToolResult(summary="q?", suspended=True)
    assert r.suspended is True


# ---- SSEEvent ----
def test_sse_event_defaults():
    ev = SSEEvent(type="done")
    assert ev.data == {}
    assert ev.trace_id == ""


def test_sse_event_full():
    ev = SSEEvent(type="answer_delta", data={"text": "x"}, trace_id="t1")
    assert ev.type == "answer_delta"
    assert ev.data == {"text": "x"}


# ---- ToolDefinition ----
def test_tool_definition_minimal():
    async def h(args, ctx, tk):
        return ToolResult(summary="ok")

    td = ToolDefinition(name="x", description="d", parameters={}, handler=h)
    assert td.name == "x"
    assert td.availability() is True  # 默认可用


def test_tool_definition_custom_availability():
    async def h(args, ctx, tk):
        return ToolResult(summary="ok")

    td = ToolDefinition(name="x", description="d", parameters={},
                        handler=h, availability=lambda: False)
    assert td.availability() is False
