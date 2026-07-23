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
        self.all_messages = []          # 每次调用的 messages 快照
        self.summarize_calls = []       # 压缩时调 summarize 的入参

    async def chat_stream(self, messages, tools=None):
        self.calls += 1
        self.last_messages = messages
        self.all_messages.append(list(messages))
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

    async def summarize(self, text: str) -> str:
        """压缩用：把长文本摘要成短文本。测试里记录调用、返回固定摘要。"""
        self.summarize_calls.append(text)
        return "已摘要"


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


def _build_loop(llm, reg, state, mgr, **kw):
    """构造 AgentLoop，默认注入 session_manager 开启会话历史。
    测试想关历史就传 session_manager=None。"""
    return AgentLoop(llm, reg, state, session_manager=mgr, **kw)


@pytest.mark.asyncio
async def test_happy_path_no_tools(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([FakeResp(content="答案是42")])
    loop = AgentLoop(llm, FakeRegistry(), state, session_manager=mgr)
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
    loop = AgentLoop(llm, reg, state, session_manager=mgr)
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
    loop = AgentLoop(llm, reg, state, session_manager=mgr)
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
    loop = AgentLoop(llm, reg, state, session_manager=mgr)
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
    loop = AgentLoop(llm, reg, state, max_turns=3, session_manager=mgr)
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
    loop = AgentLoop(llm, reg, state, session_manager=mgr)
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
    loop = AgentLoop(llm, reg, state, max_ask_user=0, session_manager=mgr)
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
    loop = AgentLoop(llm, reg, state, session_manager=mgr)
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

    loop = AgentLoop(llm, CancelInExecute(), state, session_manager=mgr)
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
    loop = AgentLoop(llm, reg, state, session_manager=mgr)
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
    loop1 = AgentLoop(llm1, reg, state, session_manager=mgr)
    await _collect(loop1.run(sid, "u1", "查", "t1", CancelToken(), is_resume=False))
    assert await state.is_suspended(sid)
    llm2 = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "finish", "args": {"answer": "结果"}, "id": "f1"}]),
    ])
    loop2 = AgentLoop(llm2, reg, state, session_manager=mgr)
    events = await _collect(loop2.run(sid, "u1", "6月", "t1", CancelToken(),
                                     is_resume=True))
    types = [e.type for e in events]
    assert types[-1] == "done"
    assert events[-1].data["answer"] == "结果"
    assert (await state.current_status(sid)) == SessionStatus.DONE


# ===== 会话历史 =====

@pytest.mark.asyncio
async def test_history_loaded_into_messages(env):
    """第二轮对话：上一轮 user+assistant 文本回合应注入 loop 的 messages。"""
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    # 预置历史：上一轮问过"发电量"、答过"5度"
    await mgr.append_message(sid, "user", "发电量", "t0")
    await mgr.append_message(sid, "assistant", "5度", "t0")
    llm = FakeLLM([FakeResp(content="好的")])
    loop = AgentLoop(llm, FakeRegistry(), state, session_manager=mgr)
    await _collect(loop.run(sid, "u1", "那用电呢", "t1", CancelToken()))
    msgs = llm.last_messages
    # 历史 user 回合 + 本轮 user 都在；assistant 历史回合也在
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    assert any("发电量" in m["content"] for m in user_msgs)   # 历史
    assert any("用电呢" in m["content"] for m in user_msgs)   # 本轮
    assert any(m.get("role") == "assistant" and "5度" in m["content"]
               for m in msgs)


@pytest.mark.asyncio
async def test_done_writes_user_and_assistant_to_history(env):
    """loop 正常结束时，本轮 user+assistant 最终答案应回写会话历史。"""
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([FakeResp(content="答案")])
    loop = AgentLoop(llm, FakeRegistry(), state, session_manager=mgr)
    await _collect(loop.run(sid, "u1", "问1", "t1", CancelToken()))
    hist = await mgr.get_messages(sid)
    roles = [(m["role"], m["content"]) for m in hist]
    assert ("user", "问1") in roles
    assert ("assistant", "答案") in roles


# ===== 会话压缩（逼近窗口阈值触发，按 group 切分，保留最近 2 group）=====

def _big_tool_result(n_chars=5000):
    """造一个超大 tool 结果。"""
    return ToolResult(summary="x" * n_chars)


@pytest.mark.asyncio
async def test_compress_triggers_when_approaching_window_threshold(env):
    """总量逼近窗口阈值触发压缩：中段 group 被摘要替换，最近 2 group 保留。"""
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    # 造多轮历史：每轮 user+assistant，字符堆过阈值。
    # max_context=2000 token → 阈值 2000 token → 8000 字符触发
    for i in range(6):
        await mgr.append_message(sid, "user", f"问{i} " + "a" * 1500, "t0")
        await mgr.append_message(sid, "assistant", f"答{i} " + "b" * 1500, "t0")
    big = _big_tool_result(5000)
    llm = FakeLLM([
        # turn1: 调 stub 拿大结果（加入 msgs）
        FakeResp(content="", tool_calls=[{"name": "stub", "args": {}, "id": "c1"}]),
        # turn2: 收到压缩后的对话摘要，基于它作答
        FakeResp(content="基于摘要作答"),
    ])
    reg = FakeRegistry({"stub": big})
    loop = AgentLoop(llm, reg, state, max_context=2000,
                     session_manager=mgr)
    events = await _collect(loop.run(sid, "u1", "本轮问", "t1", CancelToken()))
    assert events[-1].type == "done"
    # 压缩触发过 summarize（中段 group 整体压成摘要）
    assert len(llm.summarize_calls) >= 1
    # 第二次 LLM 调用（压缩后）的 messages 里应含摘要 system 消息
    compressed_msgs = llm.all_messages[1]
    assert any("对话摘要-压缩" in m.get("content", "")
               for m in compressed_msgs if m.get("role") == "system")
    # 压缩后总量应明显小于触发前（早期多组大块被压成一条摘要）
    total_after = sum(len(m.get("content", "")) for m in compressed_msgs)
    assert total_after < 18000   # 历史就 18000+，压缩后必远小于


@pytest.mark.asyncio
async def test_no_compress_when_under_window_threshold(env):
    """总量未逼近窗口阈值不触发压缩。"""
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[{"name": "stub", "args": {}, "id": "c1"}]),
        FakeResp(content="作答"),
    ])
    reg = FakeRegistry({"stub": ToolResult(summary="短结果")})
    # max_context=100000 token → 阈值 100000 token → 40万字符才触发，根本触发不了
    loop = AgentLoop(llm, reg, state, max_context=100000,
                     session_manager=mgr)
    await _collect(loop.run(sid, "u1", "查", "t1", CancelToken()))
    assert len(llm.summarize_calls) == 0
