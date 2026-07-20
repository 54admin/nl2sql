"""GET /api/result/{id} 端点测试。sqlite PG + Redis 全 mock。"""
import pytest, httpx
from fastapi import FastAPI

from src.storage import query_results
from src.storage.pg_client import init_db
from src.web.routes.result import build_result_router


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(build_result_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _no_redis():
    """让 _get_redis 返回 None，强制走 PG。"""
    return None


@pytest.mark.asyncio
async def test_get_existing_result(client, monkeypatch):
    monkeypatch.setattr(query_results, "_get_redis", _no_redis)
    rid = await query_results.save_result("s", ["a"], [{"a": 1}])
    r = await client.get(f"/api/result/{rid}")
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["a"]
    assert body["rows"] == [{"a": 1}]
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_get_missing_404(client, monkeypatch):
    monkeypatch.setattr(query_results, "_get_redis", _no_redis)
    r = await client.get("/api/result/nonexistent")
    assert r.status_code == 404
