import pytest
import httpx
from fastapi import FastAPI

from src.storage.pg_client import AsyncSessionFactory, init_db
from src.storage.models import Datasource
from src.web.routes.admin_sql_templates import build_sql_templates_router


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    async with AsyncSessionFactory() as s:
        ds = Datasource(name="d", type="starrocks", host="h", port=1,
                        db_name="db", username="u", password_enc="c")
        s.add(ds); await s.commit()
        ds_id = ds.id
    app = FastAPI()
    app.include_router(build_sql_templates_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c._ds_id = ds_id
        yield c


@pytest.mark.asyncio
async def test_crud(client):
    ds_id = client._ds_id
    # 增
    r = await client.post("/api/admin/sql-templates", json={
        "datasource_id": ds_id, "name": "月发电量",
        "sql_template": "SELECT month, sum(kwh) FROM fact_power WHERE month=:m GROUP BY month",
        "params_json": '[{"name":"m","required":true}]',
        "trigger_keywords": "发电量,月度"})
    assert r.status_code == 200
    tid = r.json()["id"]
    assert r.json()["version"] == 1
    # 查
    r = await client.get("/api/admin/sql-templates", params={"datasource_id": ds_id})
    assert len(r.json()["templates"]) == 1
    assert r.json()["templates"][0]["name"] == "月发电量"
    # 改
    r = await client.put(f"/api/admin/sql-templates/{tid}", json={"name": "月度发电量"})
    assert r.json()["version"] == 2
    # 删 + 二次删 404
    r = await client.delete(f"/api/admin/sql-templates/{tid}")
    assert r.json()["ok"] is True
    r = await client.delete(f"/api/admin/sql-templates/{tid}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_filter_by_datasource(client):
    """只返回本 datasource 的模板。"""
    ds_id = client._ds_id
    await client.post("/api/admin/sql-templates", json={
        "datasource_id": ds_id, "name": "t1", "sql_template": "SELECT 1"})
    # 造另一个 datasource 的模板
    async with AsyncSessionFactory() as s:
        s.add(Datasource(name="d2", type="starrocks", host="h", port=1,
                         db_name="db", username="u", password_enc="c"))
        await s.commit()
        ds2 = (await s.execute(Datasource.__table__.select().where(
            Datasource.name == "d2"))).first()
    await client.post("/api/admin/sql-templates", json={
        "datasource_id": ds2.id, "name": "t2", "sql_template": "SELECT 2"})
    r = await client.get("/api/admin/sql-templates", params={"datasource_id": ds_id})
    assert len(r.json()["templates"]) == 1
    assert r.json()["templates"][0]["name"] == "t1"
