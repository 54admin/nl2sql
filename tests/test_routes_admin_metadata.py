import json

import pytest
import httpx
from fastapi import FastAPI

from src.storage.pg_client import AsyncSessionFactory, init_db
from src.storage.models import Datasource, MetadataTable
from src.web.routes.admin_metadata import build_metadata_router


@pytest.fixture
async def client(monkeypatch):
    await init_db("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(build_metadata_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # 预置一个数据源 + 一张元数据表（不再预置字段——懒加载）
        async with AsyncSessionFactory() as s:
            ds = Datasource(name="d", type="starrocks", host="h", port=1,
                            db_name="db", username="u", password_enc="c")
            s.add(ds); await s.flush()
            mt = MetadataTable(datasource_id=ds.id, table_name="fact_power",
                               table_comment="发电量", source="synced")
            s.add(mt); await s.commit()
            ds_id = ds.id
            table_id = mt.id
        # mock fetch_table_columns：columns 端点不连真库
        async def fake_fetch(engine, table_name):
            return [{"name": "kwh", "type": "BIGINT", "comment": "度数"}]
        monkeypatch.setattr("src.datasource.metadata_sync.fetch_table_columns", fake_fetch)
        c._ds_id = ds_id
        c._table_id = table_id
        yield c


@pytest.mark.asyncio
async def test_read_metadata(client):
    """read_metadata 只返表清单，不再带 columns（懒加载）。"""
    r = await client.get("/api/admin/metadata", params={"datasource_id": client._ds_id})
    assert r.status_code == 200
    data = r.json()
    assert len(data["tables"]) == 1
    assert data["tables"][0]["table_name"] == "fact_power"
    assert "columns" not in data["tables"][0]   # 字段不在这里


@pytest.mark.asyncio
async def test_read_metadata_includes_enabled(client):
    r = await client.get("/api/admin/metadata", params={"datasource_id": client._ds_id})
    t = r.json()["tables"][0]
    assert "enabled" in t
    assert t["enabled"] is False


@pytest.mark.asyncio
async def test_read_metadata_includes_kind(client):
    """read_metadata 返回 kind 字段（区分表/视图）。"""
    r = await client.get("/api/admin/metadata", params={"datasource_id": client._ds_id})
    t = r.json()["tables"][0]
    assert "kind" in t
    assert t["kind"] == "table"     # ORM default


@pytest.mark.asyncio
async def test_get_table_columns(client):
    """点表展开时拉字段——columns 端点返实时拉的字段列表。"""
    r = await client.get(f"/api/admin/metadata/tables/{client._table_id}/columns")
    assert r.status_code == 200
    cols = r.json()["columns"]
    assert len(cols) == 1
    assert cols[0]["name"] == "kwh"
    assert cols[0]["type"] == "BIGINT"
    assert cols[0]["comment"] == "度数"


@pytest.mark.asyncio
async def test_get_table_columns_404(client):
    """表不存在返 404。"""
    r = await client.get("/api/admin/metadata/tables/999999/columns")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_toggle_table_enabled(client):
    r = await client.get("/api/admin/metadata", params={"datasource_id": client._ds_id})
    table_id = r.json()["tables"][0]["id"]
    r = await client.put(f"/api/admin/metadata/tables/{table_id}", json={"enabled": True})
    assert r.json()["ok"] is True
    r = await client.get("/api/admin/metadata", params={"datasource_id": client._ds_id})
    assert r.json()["tables"][0]["enabled"] is True


@pytest.mark.asyncio
async def test_table_relations_crud(client):
    ds_id = client._ds_id
    payload = {"datasource_id": int(ds_id), "main_table": "fact_power",
               "rel_table": "dim_station",
               "join_keys_json": json.dumps([{"main": "fact_power.sid", "rel": "dim_station.id"}]),
               "join_type": "left", "business_note": "场站关联"}
    r = await client.post("/api/admin/table-relations", json=payload)
    assert r.status_code == 200
    rid = r.json()["id"]
    assert rid is not None
    r = await client.get("/api/admin/table-relations", params={"datasource_id": ds_id})
    assert len(r.json()["relations"]) == 1
    assert r.json()["relations"][0]["business_note"] == "场站关联"
    # 增 → 删 → 二次删（404），CRD 闭环
    r = await client.delete(f"/api/admin/table-relations/{rid}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    r = await client.delete(f"/api/admin/table-relations/{rid}")
    assert r.status_code == 404
