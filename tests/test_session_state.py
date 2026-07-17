import pytest

from src.config import RedisConfig
from src.core.session import SessionState, SessionStatus
from src.memory.session import SessionManager
from src.storage.pg_client import init_db
from src.storage.redis_client import RedisClient


@pytest.fixture
async def state():
    await init_db("sqlite+aiosqlite:///:memory:")
    redis = RedisClient(RedisConfig(host="unreachable.invalid", port=1))
    await redis.connect()
    return SessionState(SessionManager(redis))


# ---- 状态机转换 ----
@pytest.mark.asyncio
async def test_transition_idle_to_running(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    assert (await state.current_status(sid)) == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_transition_illegal_raises(state):
    sid = await state._sm.create_session("u1", "web")
    with pytest.raises(ValueError):  # idle 不能直接跳 done
        await state.transition(sid, SessionStatus.DONE)


@pytest.mark.asyncio
async def test_transition_nonexistent_raises(state):
    with pytest.raises(ValueError):
        await state.transition("nope", SessionStatus.RUNNING)


@pytest.mark.asyncio
async def test_current_status_nonexistent_returns_none(state):
    assert await state.current_status("nope") is None


@pytest.mark.asyncio
async def test_transition_running_to_idle_for_cancel(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    await state.transition(sid, SessionStatus.IDLE)  # 取消场景合法
    assert (await state.current_status(sid)) == SessionStatus.IDLE


# ---- suspend ----
@pytest.mark.asyncio
async def test_suspend_creates_checkpoint_and_marks_status(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    cp_id = await state.suspend(sid, [{"role": "u", "content": "hi"}],
                                pending_tool="call_42")
    assert len(cp_id) == 32  # uuid4().hex
    assert (await state.current_status(sid)) == SessionStatus.AWAITING_CLARIFICATION


@pytest.mark.asyncio
async def test_suspend_requires_running(state):
    sid = await state._sm.create_session("u1", "web")
    with pytest.raises(ValueError):  # idle 不能转 awaiting
        await state.suspend(sid, [], pending_tool="x")


# ---- resume ----
@pytest.mark.asyncio
async def test_resume_injects_tool_result(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    await state.suspend(sid, [{"role": "u", "content": "hi"}], pending_tool="call_42")
    rc = await state.resume(sid, "北京")
    assert rc is not None
    assert rc.pending_tool == "call_42"
    assert rc.messages[-1] == {"role": "tool", "tool_call_id": "call_42",
                               "content": "北京"}
    assert (await state.current_status(sid)) == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_resume_idempotent_after_delete(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    await state.suspend(sid, [], pending_tool="c1")
    await state.resume(sid, "x")
    assert await state.resume(sid, "again") is None  # checkpoint 已删


@pytest.mark.asyncio
async def test_resume_non_suspended_returns_none(state):
    sid = await state._sm.create_session("u1", "web")
    assert await state.resume(sid, "x") is None


@pytest.mark.asyncio
async def test_resume_inconsistent_self_heals(state):
    """状态 awaiting 但无 checkpoint（数据不一致）→ 自愈回退 idle，不崩。"""
    sid = await state._sm.create_session("u1", "web")
    await state._sm.set_status(sid, SessionStatus.AWAITING_CLARIFICATION.value)
    rc = await state.resume(sid, "x")
    assert rc is None
    assert (await state.current_status(sid)) == SessionStatus.IDLE


# ---- expire_suspended ----
@pytest.mark.asyncio
async def test_expire_suspended(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    await state.suspend(sid, [], pending_tool="c1")
    assert await state.expire_suspended(sid) is True
    assert (await state.current_status(sid)) == SessionStatus.IDLE


@pytest.mark.asyncio
async def test_expire_non_suspended_returns_false(state):
    sid = await state._sm.create_session("u1", "web")
    assert await state.expire_suspended(sid) is False


# ---- 端到端 ----
@pytest.mark.asyncio
async def test_suspend_resume_e2e(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    msgs = [{"role": "u", "content": "hi"},
            {"role": "assistant", "content": "x"}]
    await state.suspend(sid, msgs, pending_tool="call_42")
    assert await state.is_suspended(sid) is True
    rc = await state.resume(sid, "北京")
    assert rc is not None
    assert len(rc.messages) == 3  # 原 2 条 + 注入的 tool result
    assert rc.messages[-1]["content"] == "北京"
    assert rc.messages[-1]["tool_call_id"] == "call_42"
    await state.transition(sid, SessionStatus.DONE)
    assert (await state.current_status(sid)) == SessionStatus.DONE
