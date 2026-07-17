import json
import pytest

from src.memory.session import SessionManager
from src.storage.redis_client import RedisClient
from src.config import RedisConfig
from src.storage.pg_client import init_db, AsyncSessionFactory


@pytest.fixture
async def mgr():
    await init_db("sqlite+aiosqlite:///:memory:")
    redis = RedisClient(RedisConfig(host="unreachable.invalid", port=1))
    await redis.connect()  # 降级到内存
    return SessionManager(redis)


@pytest.mark.asyncio
async def test_create_and_get_session(mgr):
    sid = await mgr.create_session(user_id="u1", channel="web")
    sess = await mgr.get_session(sid)
    assert sess["user_id"] == "u1"
    assert sess["status"] == "idle"


@pytest.mark.asyncio
async def test_append_and_list_messages(mgr):
    sid = await mgr.create_session(user_id="u1", channel="web")
    await mgr.append_message(sid, role="user", content="你好", trace_id="t1")
    await mgr.append_message(sid, role="assistant", content="在的", trace_id="t1")
    msgs = await mgr.get_messages(sid)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["content"] == "在的"


@pytest.mark.asyncio
async def test_status_persisted(mgr):
    sid = await mgr.create_session(user_id="u1", channel="web")
    await mgr.set_status(sid, "running")
    sess = await mgr.get_session(sid)
    assert sess["status"] == "running"


@pytest.mark.asyncio
async def test_delete_session(mgr):
    sid = await mgr.create_session(user_id="u1", channel="web")
    await mgr.delete_session(sid)
    assert await mgr.get_session(sid) is None
