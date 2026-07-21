import json

import pytest
import httpx
from fastapi import FastAPI

from src.storage.pg_client import AsyncSessionFactory, init_db
from src.storage.models import Datasource, MetadataColumn, MetadataTable
from src.web.routes.admin_metadata import build_metadata_router


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(build_metadata_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # 预置一个数据源 + 一张元数据表 + 字段
        async with AsyncSessionFactory() as s:
            ds = Datasource(name="d", type="starrocks", host="h", port=1,
                            db_name="db", username="u", password_enc="c")
            s.add(ds); await s.flush()
            mt = MetadataTable(datasource_id=ds.id, table_name="fact_power",
                               table_comment="发电量", source="synced")
            s.add(mt); await s.flush()
            s.add(MetadataColumn(table_id=mt.id, column_name="kwh",
                                 column_comment="度数", data_type="BIGINT", source="synced"))
            await s.commit()
            ds_id = ds.id
        c._ds_id = ds_id
        yield c


@pytest.mark.asyncio
async def test_read_metadata(client):
    r = await client.get("/api/admin/metadata", params={"datasource_id": client._ds_id})
    assert r.status_code == 200
    data = r.json()
    assert len(data["tables"]) == 1
    assert data["tables"][0]["table_name"] == "fact_power"
    assert data["tables"][0]["columns"][0]["column_name"] == "kwh"


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
