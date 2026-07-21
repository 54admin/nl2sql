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
        async def fake_fetch(engine, table_name, schema=None):
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


@pytest.mark.asyncio
async def test_read_metadata_filters_by_schema(client):
    """GET /metadata?schema_name=dw 只返该库的表（fixture 预置的 fact_power schema_name=None 不含）。"""
    async with AsyncSessionFactory() as s:
        s.add_all([
            MetadataTable(datasource_id=client._ds_id, schema_name="dw",
                          table_name="fact_a", source="synced"),
            MetadataTable(datasource_id=client._ds_id, schema_name="ods",
                          table_name="fact_b", source="synced"),
        ])
        await s.commit()
    r = await client.get("/api/admin/metadata",
                         params={"datasource_id": client._ds_id, "schema_name": "dw"})
    tables = r.json()["tables"]
    assert {t["table_name"] for t in tables} == {"fact_a"}


@pytest.mark.asyncio
async def test_read_metadata_includes_schema_field(client):
    """返回项带 schema_name 字段（前端按库分组用）。"""
    async with AsyncSessionFactory() as s:
        s.add(MetadataTable(datasource_id=client._ds_id, schema_name="dw",
                            table_name="t1", source="synced"))
        await s.commit()
    r = await client.get("/api/admin/metadata", params={"datasource_id": client._ds_id})
    by_name = {t["table_name"]: t for t in r.json()["tables"]}
    assert by_name["t1"]["schema_name"] == "dw"


@pytest.mark.asyncio
async def test_dashboard_groups_by_datasource_and_schema(client):
    """GET /dashboard：所有数据源 × 按库分组的表清单。"""
    async with AsyncSessionFactory() as s:
        s.add_all([
            MetadataTable(datasource_id=client._ds_id, schema_name="dw",
                          table_name="fact_a", source="synced", enabled=True),
            MetadataTable(datasource_id=client._ds_id, schema_name="dw",
                          table_name="dim_b", source="synced", enabled=False),
            MetadataTable(datasource_id=client._ds_id, schema_name="ods",
                          table_name="raw_c", source="synced", enabled=False),
        ])
        await s.commit()
    r = await client.get("/api/admin/dashboard")
    data = r.json()
    assert len(data["datasources"]) == 1
    ds = data["datasources"][0]
    assert ds["name"] == "d"
    schemas = {s["schema_name"]: s for s in ds["schemas"]}
    assert {"dw", "ods"} <= set(schemas.keys())
    assert {t["table_name"] for t in schemas["dw"]["tables"]} == {"fact_a", "dim_b"}
    assert {t["table_name"] for t in schemas["ods"]["tables"]} == {"raw_c"}
    # enabled 标志透传（一眼看哪些表勾选参与问数）
    dw_tables = {t["table_name"]: t for t in schemas["dw"]["tables"]}
    assert dw_tables["fact_a"]["enabled"] is True
    assert dw_tables["dim_b"]["enabled"] is False


@pytest.mark.asyncio
async def test_list_objects_left_joins_pg(client, monkeypatch):
    """list_objects：实时拉表清单 + 左连 PG 显示 enabled/手写注释。
    - business 注释默认；source=manual 的 PG 注释覆盖；enabled 按 PG 行。"""
    async def fake_fetch(engine, schema):
        return [{"name": "fact_power", "kind": "table", "comment": "fresh 业务注释"},
                {"name": "v_new", "kind": "view", "comment": "新视图"}]
    monkeypatch.setattr("src.datasource.metadata_sync.fetch_objects", fake_fetch)

    class _FakeMgr:
        async def get_engine(self, _ds_id):
            return object()   # fetch 已 mock，engine 不会被真正用
    monkeypatch.setattr("src.datasource.manager.DataSourceManager", lambda: _FakeMgr())

    # 预置 PG 行：fact_power 已勾选 + 手写注释
    async with AsyncSessionFactory() as s:
        s.add(MetadataTable(datasource_id=client._ds_id, schema_name="dw",
                            table_name="fact_power", enabled=True, source="manual",
                            table_comment="用户手写注释"))
        await s.commit()

    r = await client.get(f"/api/admin/datasources/{client._ds_id}/schemas/dw/objects")
    assert r.status_code == 200
    by_name = {o["name"]: o for o in r.json()["objects"]}
    # fact_power：PG 有 manual 注释→用手写；enabled=true；有 id
    assert by_name["fact_power"]["comment"] == "用户手写注释"
    assert by_name["fact_power"]["enabled"] is True
    assert by_name["fact_power"]["id"] is not None
    # v_new：PG 没行→用业务库 fresh 注释；enabled=false；id=null
    assert by_name["v_new"]["comment"] == "新视图"
    assert by_name["v_new"]["enabled"] is False
    assert by_name["v_new"]["id"] is None


@pytest.mark.asyncio
async def test_list_objects_pg_synced_comment_loses_to_fresh(client, monkeypatch):
    """PG source=synced 的注释**不**覆盖业务库 fresh 注释（避免旧同步值遮蔽最新值）。"""
    async def fake_fetch(engine, schema):
        return [{"name": "t", "kind": "table", "comment": "fresh"}]
    monkeypatch.setattr("src.datasource.metadata_sync.fetch_objects", fake_fetch)
    class _FakeMgr:
        async def get_engine(self, _ds_id): return object()
    monkeypatch.setattr("src.datasource.manager.DataSourceManager", lambda: _FakeMgr())
    async with AsyncSessionFactory() as s:
        s.add(MetadataTable(datasource_id=client._ds_id, schema_name="dw",
                            table_name="t", enabled=True, source="synced",
                            table_comment="stale 旧同步注释"))
        await s.commit()
    r = await client.get(f"/api/admin/datasources/{client._ds_id}/schemas/dw/objects")
    assert r.json()["objects"][0]["comment"] == "fresh"   # 业务库 fresh 胜出


@pytest.mark.asyncio
async def test_toggle_upsert_by_key_creates_row(client):
    """PUT /tables/0 + body 带 ds+schema+table → upsert 新建行并 enabled=true。"""
    r = await client.put("/api/admin/metadata/tables/0", json={
        "datasource_id": client._ds_id, "schema_name": "dw",
        "table_name": "brand_new", "enabled": True, "kind": "table",
        "comment": "fresh"})
    assert r.status_code == 200
    new_id = r.json()["id"]
    assert new_id > 0
    # 拉一次确认 enabled=true 落库
    r = await client.get("/api/admin/metadata",
                         params={"datasource_id": client._ds_id, "schema_name": "dw"})
    by_name = {t["table_name"]: t for t in r.json()["tables"]}
    assert by_name["brand_new"]["enabled"] is True


@pytest.mark.asyncio
async def test_toggle_upsert_by_key_finds_existing(client):
    """PG 已有该行（如老同步留下的 enabled=false）→ upsert 找到该行更新 enabled=true。"""
    async with AsyncSessionFactory() as s:
        s.add(MetadataTable(datasource_id=client._ds_id, schema_name="dw",
                            table_name="existing", enabled=False, source="synced"))
        await s.commit()
    r = await client.put("/api/admin/metadata/tables/0", json={
        "datasource_id": client._ds_id, "schema_name": "dw",
        "table_name": "existing", "enabled": True})
    assert r.status_code == 200
    # 不新建——仍是原来那一行
    async with AsyncSessionFactory() as s:
        rows = (await s.execute(MetadataTable.__table__.select().where(
            MetadataTable.datasource_id == client._ds_id,
            MetadataTable.schema_name == "dw"))).all()
    assert len(rows) == 1
    assert rows[0].enabled is True


@pytest.mark.asyncio
async def test_toggle_disable_when_no_row_is_noop(client):
    """PG 无该行 + enabled=false → 空操作（不新建 disabled 行）。"""
    r = await client.put("/api/admin/metadata/tables/0", json={
        "datasource_id": client._ds_id, "schema_name": "dw",
        "table_name": "ghost", "enabled": False})
    assert r.status_code == 200
    assert r.json()["id"] is None
    async with AsyncSessionFactory() as s:
        rows = (await s.execute(MetadataTable.__table__.select().where(
            MetadataTable.table_name == "ghost"))).all()
    assert rows == []


@pytest.mark.asyncio
async def test_get_columns_by_key(client, monkeypatch):
    """按 ds+schema+table 拉字段（无需 PG 行）。"""
    async def fake_fetch(engine, table_name, schema=None):
        assert table_name == "any_table" and schema == "dw"
        return [{"name": "c1", "type": "INT", "comment": "列"}]
    monkeypatch.setattr("src.datasource.metadata_sync.fetch_table_columns", fake_fetch)
    class _FakeMgr:
        async def get_engine(self, _ds_id): return object()
    monkeypatch.setattr("src.datasource.manager.DataSourceManager", lambda: _FakeMgr())
    r = await client.get(f"/api/admin/datasources/{client._ds_id}/schemas/dw/tables/any_table/columns")
    assert r.status_code == 200
    cols = r.json()["columns"]
    assert cols[0]["name"] == "c1" and cols[0]["type"] == "INT"
