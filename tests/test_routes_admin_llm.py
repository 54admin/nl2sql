"""admin LLM 配置路由测试（CRUD：列表/新建/改/删/启停，按 purpose）。"""
import pytest
import httpx
from fastapi import FastAPI

from src.storage.pg_client import init_db
from src.web.routes.admin_llm import build_admin_llm_router


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(build_admin_llm_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _put(c, cid, purpose="analysis", **kw):
    body = {"purpose": purpose, "model": "m", "base_url": "u", "enabled": True}
    body.update(kw)
    return await c.put(f"/api/admin/llm-config/{cid}", json=body)


@pytest.mark.asyncio
async def test_list_empty(client):
    resp = await client.get("/api/admin/llm-config")
    assert resp.json() == {"configs": []}


@pytest.mark.asyncio
async def test_put_creates(client):
    resp = await _put(client, "qwen-chat", purpose="analysis", model="Qwen")
    assert resp.status_code == 200
    assert resp.json()["id"] == "qwen-chat"
    cfgs = {c["id"]: c for c in (await client.get("/api/admin/llm-config")).json()["configs"]}
    assert cfgs["qwen-chat"]["model"] == "Qwen"
    assert cfgs["qwen-chat"]["purpose"] == "analysis"


@pytest.mark.asyncio
async def test_put_then_put_bumps_version(client):
    await _put(client, "m1")
    await _put(client, "m1", model="m2")
    cfg = {c["id"]: c for c in (await client.get("/api/admin/llm-config")).json()["configs"]}["m1"]
    assert cfg["version"] == 2
    assert cfg["model"] == "m2"


@pytest.mark.asyncio
async def test_delete(client):
    await _put(client, "m1")
    assert (await client.delete("/api/admin/llm-config/m1")).status_code == 200
    assert (await client.get("/api/admin/llm-config")).json() == {"configs": []}


@pytest.mark.asyncio
async def test_delete_missing_404(client):
    assert (await client.delete("/api/admin/llm-config/nope")).status_code == 404


@pytest.mark.asyncio
async def test_invalid_purpose_rejected(client):
    resp = await _put(client, "m1", purpose="foo")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_same_purpose_multiple(client):
    """同用途可多个（备用切换）。"""
    await _put(client, "chat-a", purpose="analysis", model="A")
    await _put(client, "chat-b", purpose="analysis", model="B")
    cfgs = [c for c in (await client.get("/api/admin/llm-config")).json()["configs"]
            if c["purpose"] == "analysis"]
    assert len(cfgs) == 2


@pytest.mark.asyncio
async def test_put_triggers_llm_service_reset():
    await init_db("sqlite+aiosqlite:///:memory:")
    reset_calls = {"n": 0}

    class FakeLLMService:
        def reset_dynamic(self):
            reset_calls["n"] += 1

    app = FastAPI()
    app.include_router(build_admin_llm_router(FakeLLMService()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await _put(c, "m1")
    assert reset_calls["n"] == 1
