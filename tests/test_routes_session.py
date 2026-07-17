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
        assert set(s.keys()) == {"id", "channel", "status", "created_at"}
        assert s["channel"] == "web"
        assert s["status"] == "idle"


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
        assert resp.json() == {"ok": True}
        resp = await client.get("/api/session", params={"user_id": "u1"})
        assert len(resp.json()["sessions"]) == 0


@pytest.mark.asyncio
async def test_delete_session_idempotent(setup_db):
    mgr = setup_db
    app = FastAPI()
    app.include_router(build_session_router(mgr))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/api/session/ghost-sid")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
