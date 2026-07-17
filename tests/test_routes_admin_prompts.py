import pytest
import httpx
from fastapi import FastAPI

from src.core.prompt_store import PromptStore
from src.storage.pg_client import init_db
from src.web.routes.admin_prompts import build_admin_prompts_router


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    store = PromptStore()
    app = FastAPI()
    app.include_router(build_admin_prompts_router(store))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_list_empty(client):
    resp = await client.get("/api/admin/prompts")
    assert resp.status_code == 200
    assert resp.json() == {"prompts": []}


@pytest.mark.asyncio
async def test_post_then_get(client):
    resp = await client.post("/api/admin/prompts", json={
        "scene": "default", "content": "你是助手", "enabled": True})
    assert resp.status_code == 200
    assert resp.json()["version"] == 1
    resp = await client.get("/api/admin/prompts/default")
    assert resp.json()["content"] == "你是助手"


@pytest.mark.asyncio
async def test_put_updates_existing(client):
    await client.post("/api/admin/prompts", json={
        "scene": "default", "content": "v1", "enabled": True})
    resp = await client.put("/api/admin/prompts/default", json={
        "scene": "default", "content": "v2", "enabled": True})
    assert resp.json()["version"] == 2
    resp = await client.get("/api/admin/prompts/default")
    assert resp.json()["content"] == "v2"


@pytest.mark.asyncio
async def test_delete(client):
    await client.post("/api/admin/prompts", json={
        "scene": "default", "content": "x", "enabled": True})
    resp = await client.delete("/api/admin/prompts/default")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    resp = await client.delete("/api/admin/prompts/default")
    assert resp.json()["deleted"] is False


@pytest.mark.asyncio
async def test_list_after_inserts(client):
    await client.post("/api/admin/prompts", json={
        "scene": "default", "content": "d", "enabled": True})
    await client.post("/api/admin/prompts", json={
        "scene": "attribution", "content": "a", "enabled": True})
    resp = await client.get("/api/admin/prompts")
    scenes = {p["scene"] for p in resp.json()["prompts"]}
    assert scenes == {"default", "attribution"}
