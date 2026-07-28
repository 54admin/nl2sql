"""admin LLM 配置路由测试（model 级：一行=一个模型，purposes 多选，启用互斥移除用途）。"""
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


async def _put(c, cid, purposes=None, **kw):
    body = {"purposes": purposes if purposes is not None else ["analysis"], "model": "m", "base_url": "u", "enabled": True}
    body.update(kw)
    return await c.put(f"/api/admin/llm-config/{cid}", json=body)


@pytest.mark.asyncio
async def test_list_empty(client):
    assert (await client.get("/api/admin/llm-config")).json() == {"configs": []}


@pytest.mark.asyncio
async def test_put_creates(client):
    r = await _put(client, "qwen", purposes=["analysis", "attribution"], model="Qwen")
    assert r.status_code == 200
    cfgs = {c["id"]: c for c in (await client.get("/api/admin/llm-config")).json()["configs"]}
    assert cfgs["qwen"]["purposes"] == ["analysis", "attribution"]
    assert cfgs["qwen"]["model"] == "Qwen"


@pytest.mark.asyncio
async def test_invalid_purpose_rejected(client):
    assert (await _put(client, "m1", purposes=["foo"])).status_code == 400


@pytest.mark.asyncio
async def test_empty_purposes_rejected(client):
    assert (await _put(client, "m1", purposes=[])).status_code == 400


@pytest.mark.asyncio
async def test_delete(client):
    await _put(client, "m1")
    assert (await client.delete("/api/admin/llm-config/m1")).status_code == 200
    assert (await client.get("/api/admin/llm-config")).json() == {"configs": []}


@pytest.mark.asyncio
async def test_delete_missing_404(client):
    assert (await client.delete("/api/admin/llm-config/nope")).status_code == 404


@pytest.mark.asyncio
async def test_enabling_removes_overlap_purpose_from_others(client):
    """一用途一启用：启用 B(analysis) → A 的 analysis 从 purposes 移除（模型不删，只移除冲突用途）。"""
    await _put(client, "a", purposes=["analysis", "attribution"], enabled=True)
    await _put(client, "b", purposes=["analysis"], enabled=True)   # B 抢 analysis
    cfgs = {c["id"]: c for c in (await client.get("/api/admin/llm-config")).json()["configs"]}
    assert cfgs["a"]["purposes"] == ["attribution"]   # analysis 被移除，attribution 留着（不冲突）
    assert cfgs["b"]["purposes"] == ["analysis"]
    assert cfgs["a"]["enabled"] is True               # A 模型还在、仍 enabled


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
