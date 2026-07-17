import asyncio
import json
import pytest

from src.config import RedisConfig
from src.core.agent_loop import AgentLoop
from src.core.session import SessionState, SessionStatus
from src.core.types import CancelToken, SSEEvent, ToolResult
from src.memory.session import SessionManager
from src.storage.pg_client import init_db
from src.storage.redis_client import RedisClient


# ---- Fake 组件 ----
class FakeResp:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.last_messages = None

    async def chat_stream(self, messages, tools=None):
        self.calls += 1
        self.last_messages = messages
        resp = self._responses.pop(0)
        from src.llm.service import _Chunk
        from types import SimpleNamespace
        if resp.content:
            yield _Chunk(content=resp.content)
        for tc in resp.tool_calls:
            yield _Chunk(tool_call_delta=[SimpleNamespace(
                index=0, id=tc["id"],
                function=SimpleNamespace(name=tc["name"],
                                         arguments=json.dumps(tc.get("args", {}))))])


class FakeRegistry:
    def __init__(self, results=None):
        self._results = results or {}
        self.executed = []

    def openai_tools(self):
        return []

    async def execute(self, name, args, ctx, cancel_token):
        self.executed.append((name, args))
        return self._results.get(name, ToolResult(summary="ok"))


@pytest.fixture
async def env():
    await init_db("sqlite+aiosqlite:///:memory:")
    redis = RedisClient(RedisConfig(host="unreachable.invalid", port=1))
    await redis.connect()
    mgr = SessionManager(redis)
    return mgr, SessionState(mgr)


async def _collect(gen):
    return [e async for e in gen]


@pytest.mark.asyncio
async def test_happy_path_no_tools(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([FakeResp(content="答案是42")])
    loop = AgentLoop(llm, FakeRegistry(), state)
    events = await _collect(loop.run(sid, "u1", "你好", "t1", CancelToken()))
    types = [e.type for e in events]
    assert "answer_delta" in types
    assert types[-1] == "done"
    assert events[-1].data["answer"] == "答案是42"
    assert llm.calls == 1
    assert (await state.current_status(sid)) == SessionStatus.DONE


@pytest.mark.asyncio
async def test_tool_execution_with_result_id(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {"q": "x"}, "id": "c1"}]),
        FakeResp(content="汇总5行"),
    ])
    reg = FakeRegistry({"stub": ToolResult(summary="命中5行", result_id="r1")})
    loop = AgentLoop(llm, reg, state)
    events = await _collect(loop.run(sid, "u1", "查", "t1", CancelToken()))
    assert len(reg.executed) == 1
    tr = [e for e in events if e.type == "tool_result"][0]
    assert tr.data["summary"] == "命中5行"
    assert tr.data["result_id"] == "r1"
    assert events[-1].type == "done"
    assert events[-1].data["answer"] == "汇总5行"


@pytest.mark.asyncio
async def test_tool_summary_only_no_full_result_in_messages(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {}, "id": "c1"}]),
        FakeResp(content="done"),
    ])
    reg = FakeRegistry({"stub": ToolResult(summary="摘要", result_id="r1")})
    loop = AgentLoop(llm, reg, state)
    await _collect(loop.run(sid, "u1", "查", "t1", CancelToken()))
    tool_msgs = [m for m in llm.last_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "摘要"
    assert "r1" not in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_finish_tool_terminates_loop(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "finish", "args": {"answer": "完成"}, "id": "c1"}]),
    ])
    reg = FakeRegistry({"finish": ToolResult(summary="完成", finished=True)})
    loop = AgentLoop(llm, reg, state)
    events = await _collect(loop.run(sid, "u1", "你好", "t1", CancelToken()))
    assert events[-1].type == "done"
    assert events[-1].data["answer"] == "完成"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_guard_max_turns(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    # 每个 i 产出一个 FakeResp，其 tool_calls 是含单元素的列表
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {"i": i}, "id": f"c{i}"}])
        for i in range(20)])
    reg = FakeRegistry({"stub": ToolResult(summary="ok")})
    loop = AgentLoop(llm, reg, state, max_turns=3)
    events = await _collect(loop.run(sid, "u1", "查", "t1", CancelToken()))
    types = [e.type for e in events]
    assert "warning" in types
    assert types[-1] == "done"
    tool_call_count = sum(1 for e in events if e.type == "tool_call")
    assert tool_call_count == 3


@pytest.mark.asyncio
async def test_guard_duplicate_call(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {"q": "x"}, "id": "c1"}]),
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {"q": "x"}, "id": "c2"}]),
        FakeResp(content="最终"),
    ])
    reg = FakeRegistry({"stub": ToolResult(summary="ok")})
    loop = AgentLoop(llm, reg, state)
    events = await _collect(loop.run(sid, "u1", "查", "t1", CancelToken()))
    assert len(reg.executed) == 1
    converged = [e for e in events if e.type == "tool_result"
                 and e.data.get("converged")]
    assert len(converged) == 1


@pytest.mark.asyncio
async def test_guard_ask_user_limit(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "ask_user", "args": {"question": "q1"}, "id": "a1"}]),
        FakeResp(content="最终答案"),
    ])
    reg = FakeRegistry({"ask_user": ToolResult(summary="q1", suspended=True)})
    loop = AgentLoop(llm, reg, state, max_ask_user=0)
    events = await _collect(loop.run(sid, "u1", "查", "t1", CancelToken()))
    assert not await state.is_suspended(sid)
    types = [e.type for e in events]
    assert "clarification_needed" not in types
    assert types[-1] == "done"


@pytest.mark.asyncio
async def test_cancel_between_turns(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    tk = CancelToken()
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {}, "id": "c1"}]),
        FakeResp(content="不应到达"),
    ])
    reg = FakeRegistry({"stub": ToolResult(summary="ok")})
    loop = AgentLoop(llm, reg, state)
    events = []
    async for e in loop.run(sid, "u1", "查", "t1", tk):
        events.append(e)
        if e.type == "tool_result":
            tk.cancel()
    assert events[-1].type == "cancelled"


@pytest.mark.asyncio
async def test_cancel_in_tool(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    tk = CancelToken()
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {}, "id": "c1"}]),
        FakeResp(content="不应到达"),
    ])

    class CancelInExecute:
        def openai_tools(self):
            return []

        async def execute(self, name, args, ctx, cancel_token):
            cancel_token.cancel()
            return ToolResult(summary="ok")

    loop = AgentLoop(llm, CancelInExecute(), state)
    events = await _collect(loop.run(sid, "u1", "查", "t1", tk))
    assert events[-1].type == "cancelled"


@pytest.mark.asyncio
async def test_ask_user_suspends_and_checkpoints(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "ask_user", "args": {"question": "哪个时间范围?"}, "id": "a1"}]),
    ])
    reg = FakeRegistry({"ask_user": ToolResult(summary="哪个时间范围?", suspended=True)})
    loop = AgentLoop(llm, reg, state)
    events = await _collect(loop.run(sid, "u1", "查发电量", "t1", CancelToken()))
    types = [e.type for e in events]
    assert "clarification_needed" in types
    cn = [e for e in events if e.type == "clarification_needed"][0]
    assert cn.data["question"] == "哪个时间范围?"
    assert types[-1] == "clarification_needed"
    assert "tool_result" not in types
    assert await state.is_suspended(sid)
    rc = await state.resume(sid, "6月")
    assert rc is not None
    assert rc.pending_tool == "a1"
    assert rc.messages[-1] == {"role": "tool", "tool_call_id": "a1",
                               "content": "6月"}
    tool_msgs = [m for m in rc.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1


@pytest.mark.asyncio
async def test_ask_user_resume_continues_loop(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm1 = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "ask_user", "args": {"question": "q?"}, "id": "a1"}]),
    ])
    reg = FakeRegistry({
        "ask_user": ToolResult(summary="q?", suspended=True),
        "finish": ToolResult(summary="最终", finished=True),
    })
    loop1 = AgentLoop(llm1, reg, state)
    await _collect(loop1.run(sid, "u1", "查", "t1", CancelToken(), is_resume=False))
    assert await state.is_suspended(sid)
    llm2 = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "finish", "args": {"answer": "结果"}, "id": "f1"}]),
    ])
    loop2 = AgentLoop(llm2, reg, state)
    events = await _collect(loop2.run(sid, "u1", "6月", "t1", CancelToken(),
                                     is_resume=True))
    types = [e.type for e in events]
    assert types[-1] == "done"
    assert events[-1].data["answer"] == "结果"
    assert (await state.current_status(sid)) == SessionStatus.DONE
