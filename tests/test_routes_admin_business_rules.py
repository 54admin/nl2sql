import pytest
import httpx
from fastapi import FastAPI

from src.storage.pg_client import init_db
from src.web.routes.admin_business_rules import build_business_rules_router


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(build_business_rules_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_crud(client):
    r = await client.post("/api/admin/business-rules", json={
        "category": "metric", "key": "发电量",
        "value_json": '{"unit":"kWh","decimal":2}', "enabled": True})
    assert r.status_code == 200
    rid = r.json()["id"]
    assert r.json()["version"] == 1
    # 查
    r = await client.get("/api/admin/business-rules", params={"category": "metric"})
    assert len(r.json()["rules"]) == 1
    assert r.json()["rules"][0]["key"] == "发电量"
    # 改
    r = await client.put(f"/api/admin/business-rules/{rid}", json={"value_json": '{"unit":"kWh"}'})
    assert r.json()["version"] == 2
    # 删 + 二次删 404
    r = await client.delete(f"/api/admin/business-rules/{rid}")
    assert r.json()["ok"] is True
    r = await client.delete(f"/api/admin/business-rules/{rid}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_filter_by_category(client):
    await client.post("/api/admin/business-rules",
                      json={"category": "metric", "key": "a", "value_json": "1"})
    await client.post("/api/admin/business-rules",
                      json={"category": "constraint", "key": "b", "value_json": "2"})
    r = await client.get("/api/admin/business-rules", params={"category": "metric"})
    assert len(r.json()["rules"]) == 1
