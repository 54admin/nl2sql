import pytest

from src.core.types import (
    CancelToken, LoopContext, ToolDefinition, ToolResult,
)
from src.tools.registry import ToolRegistry, coerce_tool_args, require_module


@pytest.fixture
def ctx():
    return LoopContext(session_id="s", user_id="u", trace_id="t")


@pytest.fixture
def cancel_token():
    return CancelToken()


# ---- coerce_tool_args ----
def test_coerce_integer():
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    assert coerce_tool_args(schema, {"limit": "100"}) == {"limit": 100}


def test_coerce_number():
    schema = {"type": "object", "properties": {"pi": {"type": "number"}}}
    assert coerce_tool_args(schema, {"pi": "3.14"})["pi"] == 3.14


def test_coerce_boolean_truthy():
    schema = {"type": "object", "properties": {"flag": {"type": "boolean"}}}
    assert coerce_tool_args(schema, {"flag": "true"})["flag"] is True
    assert coerce_tool_args(schema, {"flag": "yes"})["flag"] is True
    assert coerce_tool_args(schema, {"flag": "1"})["flag"] is True


def test_coerce_boolean_falsy():
    schema = {"type": "object", "properties": {"flag": {"type": "boolean"}}}
    assert coerce_tool_args(schema, {"flag": "0"})["flag"] is False
    assert coerce_tool_args(schema, {"flag": "no"})["flag"] is False


def test_coerce_array():
    schema = {"type": "object", "properties": {"ids": {"type": "array"}}}
    assert coerce_tool_args(schema, {"ids": "[1,2]"})["ids"] == [1, 2]


def test_coerce_object():
    schema = {"type": "object", "properties": {"obj": {"type": "object"}}}
    assert coerce_tool_args(schema, {"obj": '{"k":1}'})["obj"] == {"k": 1}


def test_coerce_union_type_takes_first_non_null():
    schema = {"type": "object", "properties": {"x": {"type": ["integer", "null"]}}}
    assert coerce_tool_args(schema, {"x": "5"})["x"] == 5


def test_coerce_invalid_keeps_original():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    out = coerce_tool_args(schema, {"n": "abc"})
    assert out["n"] == "abc"  # 强转失败保留原值


def test_coerce_non_string_untouched():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    out = coerce_tool_args(schema, {"n": 5})
    assert out["n"] == 5


def test_coerce_missing_schema_field():
    schema = {"type": "object", "properties": {}}
    out = coerce_tool_args(schema, {"extra": "x"})
    assert out["extra"] == "x"


# ---- require_module ----
def test_require_module_present():
    assert require_module("json")() is True


def test_require_module_absent():
    assert require_module("__no_such_module_xyz__")() is False


# ---- ToolRegistry 注册/查询 ----
async def _ok_handler(args, ctx, tk):
    return ToolResult(summary=f"got {args}")


def test_register_available_defs_and_openai_tools():
    reg = ToolRegistry()
    on = ToolDefinition(name="on", description="d1", parameters={"type": "object"},
                        handler=_ok_handler)
    off = ToolDefinition(name="off", description="d2", parameters={"type": "object"},
                         handler=_ok_handler, availability=lambda: False)
    reg.register(on)
    reg.register(off)
    names = {td.name for td in reg.available_defs()}
    assert names == {"on"}
    tools = reg.openai_tools()
    assert len(tools) == 1
    assert tools[0] == {"type": "function",
                        "function": {"name": "on", "description": "d1",
                                     "parameters": {"type": "object"}}}


def test_get_hit_and_miss():
    reg = ToolRegistry()
    td = ToolDefinition(name="x", description="d", parameters={}, handler=_ok_handler)
    reg.register(td)
    assert reg.get("x") is td
    assert reg.get("nope") is None


# ---- ToolRegistry.execute ----
@pytest.mark.asyncio
async def test_execute_coerces_args(ctx, cancel_token):
    seen = {}

    async def h(args, c, t):
        seen.update(args)
        return ToolResult(summary="ok")

    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="t", description="d",
        parameters={"type": "object", "properties": {"n": {"type": "integer"}}},
        handler=h))
    await reg.execute("t", {"n": "5"}, ctx, cancel_token)
    assert seen == {"n": 5}


@pytest.mark.asyncio
async def test_execute_unknown_tool(ctx, cancel_token):
    reg = ToolRegistry()
    r = await reg.execute("ghost", {}, ctx, cancel_token)
    assert "不存在" in r.summary
    assert r.finished is False and r.suspended is False


@pytest.mark.asyncio
async def test_execute_unavailable(ctx, cancel_token):
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="x", description="d", parameters={},
                                handler=_ok_handler, availability=lambda: False))
    r = await reg.execute("x", {}, ctx, cancel_token)
    assert "不可用" in r.summary


@pytest.mark.asyncio
async def test_execute_handler_exception(ctx, cancel_token):
    async def boom(args, c, t):
        raise RuntimeError("炸了")

    reg = ToolRegistry()
    reg.register(ToolDefinition(name="x", description="d", parameters={}, handler=boom))
    r = await reg.execute("x", {}, ctx, cancel_token)
    assert "执行出错" in r.summary
    assert "炸了" in r.summary


# ==== builtins 测试（Task 4 追加）====
from src.tools.builtins import ECHO, FINISH, ASK_USER, default_registry


@pytest.mark.asyncio
async def test_echo_handler(ctx, cancel_token):
    r = await ECHO.handler({"text": "hi"}, ctx, cancel_token)
    assert r.summary == "echo: hi"
    assert r.finished is False and r.suspended is False


@pytest.mark.asyncio
async def test_finish_handler(ctx, cancel_token):
    r = await FINISH.handler({"answer": "done"}, ctx, cancel_token)
    assert r.summary == "done"
    assert r.finished is True


@pytest.mark.asyncio
async def test_ask_user_handler(ctx, cancel_token):
    r = await ASK_USER.handler({"question": "哪个月?"}, ctx, cancel_token)
    assert r.summary == "哪个月?"
    assert r.suspended is True


def test_default_registry_three_tools():
    reg = default_registry()
    names = {td.name for td in reg.available_defs()}
    assert names == {"echo", "finish", "ask_user", "query_metadata", "execute_sql"}
    tools = reg.openai_tools()
    assert len(tools) == 5
    for t in tools:
        assert t["type"] == "function"
        assert "name" in t["function"]
        assert "parameters" in t["function"]


def test_registry_hides_unavailable_tool():
    from src.tools.registry import ToolRegistry
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="hidden", description="d", parameters={"type": "object"},
        handler=ECHO.handler, availability=require_module("__no_such_module__")))
    assert reg.openai_tools() == []  # 缺依赖自动隐藏
