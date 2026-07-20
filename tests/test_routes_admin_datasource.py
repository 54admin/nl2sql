import pytest
import httpx
from fastapi import FastAPI

from src.datasource.manager import DataSourceManager
from src.storage.pg_client import init_db
from src.web.routes.admin_datasource import build_datasource_router


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("NL2SQL_DS_KEY", Fernet.generate_key().decode())


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(build_datasource_router(DataSourceManager()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _payload(**over):
    base = dict(name="ds", type="starrocks", host="h", port=9030,
                db_name="db", username="u", password="p", sync_scope="fact_")
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_create_list(client):
    r = await client.post("/api/admin/datasources", json=_payload())
    assert r.status_code == 200
    ds_id = r.json()["id"]
    r = await client.get("/api/admin/datasources")
    assert len(r.json()["datasources"]) == 1
    assert r.json()["datasources"][0]["id"] == ds_id


@pytest.mark.asyncio
async def test_list_never_returns_password(client):
    await client.post("/api/admin/datasources", json=_payload(password="secret"))
    r = await client.get("/api/admin/datasources")
    body = str(r.json())
    assert "secret" not in body
    assert "password" not in r.json()["datasources"][0]


@pytest.mark.asyncio
async def test_update_and_delete(client):
    ds_id = (await client.post("/api/admin/datasources", json=_payload())).json()["id"]
    r = await client.put(f"/api/admin/datasources/{ds_id}", json={"host": "h2"})
    assert r.status_code == 200
    r = await client.delete(f"/api/admin/datasources/{ds_id}")
    assert r.json()["ok"] is True
    r = await client.delete(f"/api/admin/datasources/{ds_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_test_endpoint_calls_manager(client, monkeypatch):
    """test 端点调 manager.test_connection；mock 它验证路由接通。"""
    called = []
    async def fake_test(self, ds_id):
        called.append(ds_id)
    from src.datasource.manager import DataSourceManager
    monkeypatch.setattr(DataSourceManager, "test_connection", fake_test)
    ds_id = (await client.post("/api/admin/datasources", json=_payload())).json()["id"]
    r = await client.post(f"/api/admin/datasources/{ds_id}/test")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert called == [ds_id]


@pytest.mark.asyncio
async def test_sync_endpoint(client, monkeypatch):
    """sync 端点调 sync_metadata；mock engine.run_sync 验证接通。"""
    class FakeEngine:
        async def run_sync(self, fn): return []
        async def dispose(self): pass
    from src.datasource.manager import DataSourceManager
    monkeypatch.setattr(DataSourceManager, "get_engine",
                        lambda self, ds_id: _async_return(FakeEngine()))
    ds_id = (await client.post("/api/admin/datasources", json=_payload())).json()["id"]
    r = await client.post(f"/api/admin/datasources/{ds_id}/sync")
    assert r.status_code == 200
    assert r.json() == {"added": 0, "updated": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_update_404_when_missing(client):
    r = await client.put("/api/admin/datasources/9999", json={"host": "x"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_test_endpoint_404_when_missing(client):
    """ds_id 不存在时 test 端点应返回 404（不是 400 连接失败）。"""
    r = await client.post("/api/admin/datasources/9999/test")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_sync_endpoint_404_when_missing(client):
    r = await client.post("/api/admin/datasources/9999/sync")
    assert r.status_code == 404


async def _async_return(v):
    return v
