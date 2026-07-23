import pytest

from src.config import RedisConfig
from src.core.session import SessionState, SessionStatus
from src.memory.session import SessionManager
from src.storage.models import LoopCheckpoint
from src.storage.pg_client import AsyncSessionFactory, init_db
from src.storage.redis_client import RedisClient
from datetime import datetime, timedelta


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


# ---- sweep_stale_suspended ----
@pytest.mark.asyncio
async def test_sweep_skips_fresh_suspended(state):
    """刚挂起的（未超时）不应被清。"""
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    await state.suspend(sid, [], pending_tool="c1")
    n = await state.sweep_stale_suspended(max_age_minutes=30)
    assert n == 0
    assert (await state.current_status(sid)) == SessionStatus.AWAITING_CLARIFICATION
    assert await state.is_suspended(sid)


@pytest.mark.asyncio
async def test_sweep_clears_old_suspended(state):
    """挂起超过阈值的会话被清：状态回 idle，checkpoint 删。"""
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    await state.suspend(sid, [], pending_tool="c1")
    # 手动把 checkpoint 的 created_at 改成 1 小时前模拟挂起超时
    old = datetime.now() - timedelta(hours=1)
    async with AsyncSessionFactory() as s:
        cp = (await s.execute(LoopCheckpoint.__table__.select())).first()
        await s.execute(LoopCheckpoint.__table__.update().where(
            LoopCheckpoint.id == cp.id).values(created_at=old))
        await s.commit()
    n = await state.sweep_stale_suspended(max_age_minutes=30)
    assert n == 1
    assert (await state.current_status(sid)) == SessionStatus.IDLE
    assert not await state.is_suspended(sid)


@pytest.mark.asyncio
async def test_sweep_clears_orphan_checkpoint(state):
    """会话已离开挂起态但 checkpoint 残留（异常残留）：清孤儿 checkpoint。
    模拟：suspend 后正常 resume（会删当次 cp），但另留一条时间调旧的残留 cp。"""
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    await state.suspend(sid, [], pending_tool="c1")
    await state.resume(sid, "回复")  # 正常恢复，删当次 cp，状态回 running
    assert (await state.current_status(sid)) == SessionStatus.RUNNING
    # 再造一条时间调旧的残留 cp（模拟异常没删干净）
    old = datetime.now() - timedelta(hours=1)
    async with AsyncSessionFactory() as s:
        s.add(LoopCheckpoint(id="orphan1", session_id=sid,
                             messages_json="[]", pending_tool="c1",
                             created_at=old))
        await s.commit()
    n = await state.sweep_stale_suspended(max_age_minutes=30)
    assert n == 1
    async with AsyncSessionFactory() as s:
        row = await s.get(LoopCheckpoint, "orphan1")
        assert row is None


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
