import pytest
import httpx
from fastapi import FastAPI

from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory, init_db
from src.web.routes.admin_llm import build_admin_llm_router


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(build_admin_llm_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_get_returns_yaml_source_when_no_dynamic(client):
    resp = await client.get("/api/admin/llm-config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "yaml"
    assert body["dynamic"] is None


@pytest.mark.asyncio
async def test_put_creates_dynamic_config(client):
    resp = await client.put("/api/admin/llm-config", json={
        "model": "qwen3", "base_url": "http://gw", "api_key": "k",
        "temperature": 0.5, "timeout": 90, "enabled": True})
    assert resp.status_code == 200
    resp = await client.get("/api/admin/llm-config")
    body = resp.json()
    assert body["source"] == "dynamic"
    assert body["dynamic"]["model"] == "qwen3"
    assert body["dynamic"]["temperature"] == 0.5


@pytest.mark.asyncio
async def test_put_then_put_bumps_version(client):
    await client.put("/api/admin/llm-config", json={
        "model": "v1", "base_url": "u", "enabled": True})
    await client.put("/api/admin/llm-config", json={
        "model": "v2", "base_url": "u", "enabled": True})
    resp = await client.get("/api/admin/llm-config")
    assert resp.json()["dynamic"]["version"] == 2


@pytest.mark.asyncio
async def test_put_triggers_llm_service_reset(client):
    reset_calls = {"n": 0}

    class FakeLLMService:
        def reset_dynamic(self):
            reset_calls["n"] += 1

    app = FastAPI()
    app.include_router(build_admin_llm_router(FakeLLMService()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.put("/api/admin/llm-config", json={
            "model": "x", "base_url": "u", "enabled": True})
    assert reset_calls["n"] == 1
