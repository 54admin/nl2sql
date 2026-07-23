import pytest
import httpx
from fastapi import FastAPI

from src.config import RedisConfig
from src.memory.session import SessionManager
from src.storage.pg_client import init_db
from src.storage.redis_client import RedisClient
from src.web.routes.session import build_session_router


@pytest.fixture
async def setup_db():
    await init_db("sqlite+aiosqlite:///:memory:")
    redis = RedisClient(RedisConfig(host="unreachable.invalid", port=1))
    await redis.connect()
    return SessionManager(redis)


@pytest.mark.asyncio
async def test_list_sessions_by_user(setup_db):
    mgr = setup_db
    sid1 = await mgr.create_session("u1", "web")
    sid2 = await mgr.create_session("u1", "app")
    await mgr.create_session("u2", "web")

    app = FastAPI()
    app.include_router(build_session_router(mgr))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/session", params={"user_id": "u1"})
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 2
        assert {s["id"] for s in sessions} == {sid1, sid2}


@pytest.mark.asyncio
async def test_list_sessions_fields(setup_db):
    mgr = setup_db
    sid = await mgr.create_session("u1", "web")
    app = FastAPI()
    app.include_router(build_session_router(mgr))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/session", params={"user_id": "u1"})
        s = resp.json()["sessions"][0]
        assert set(s.keys()) == {"id", "channel", "status", "title", "created_at"}
        assert s["channel"] == "web"
        assert s["status"] == "idle"
        assert s["title"] is None   # 新建无标题，首问才填


@pytest.mark.asyncio
async def test_delete_session(setup_db):
    mgr = setup_db
    sid = await mgr.create_session("u1", "web")
    app = FastAPI()
    app.include_router(build_session_router(mgr))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/api/session/{sid}")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}   # 真删
        resp = await client.get("/api/session", params={"user_id": "u1"})
        assert len(resp.json()["sessions"]) == 0


@pytest.mark.asyncio
async def test_delete_session_idempotent(setup_db):
    mgr = setup_db
    app = FastAPI()
    app.include_router(build_session_router(mgr))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 删不存在的会话：返回 ok=False（逻辑删除幂等，不存在不报错）
        resp = await client.delete("/api/session/ghost-sid")
        assert resp.status_code == 200
        assert resp.json() == {"ok": False}


@pytest.mark.asyncio
async def test_rename_session(setup_db):
    mgr = setup_db
    sid = await mgr.create_session("u1", "web")
    app = FastAPI()
    app.include_router(build_session_router(mgr))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(f"/api/session/{sid}", json={"title": "我的问数"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # 列表里标题应已改
        r = await client.get("/api/session", params={"user_id": "u1"})
        assert r.json()["sessions"][0]["title"] == "我的问数"


@pytest.mark.asyncio
async def test_rename_nonexistent_404(setup_db):
    mgr = setup_db
    app = FastAPI()
    app.include_router(build_session_router(mgr))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch("/api/session/ghost", json={"title": "x"})
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_fill_title_only_when_empty(setup_db):
    """首问填标题：仅空标题时写，不覆盖已有标题。"""
    mgr = setup_db
    sid = await mgr.create_session("u1", "web")
    assert await mgr.fill_title_if_empty(sid, "第一条问题") is True   # 空时写
    assert await mgr.fill_title_if_empty(sid, "不应覆盖") is False    # 已有不覆盖
    sessions = await mgr.list_sessions("u1")
    assert sessions[0]["title"] == "第一条问题"
