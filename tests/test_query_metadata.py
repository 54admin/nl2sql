import json

import pytest

from src.datasource.manager import DataSourceManager
from src.storage.models import MetadataTable
from src.storage.pg_client import AsyncSessionFactory, init_db
from src.tools.metadata import _list_enabled_tables, query_metadata



class FakeEngine:     # 不连真库，fetch 被 mock，engine 仅作占位
    pass


@pytest.fixture
async def db(monkeypatch):
    """预置 1 数据源 + 2 表（1 enabled 1 disabled）。字段懒加载，mock fetch 返回固定字段。"""
    await init_db("sqlite+aiosqlite:///:memory:")
    ds_id = await DataSourceManager().create_datasource(
        dict(name="d", type="starrocks", host="h", port=1, db_name="db",
             username="u", password="p"))
    async with AsyncSessionFactory() as s:
        s.add_all([
            MetadataTable(datasource_id=ds_id, table_name="fact_power",
                          table_comment="发电量", enabled=True, source="synced"),
            MetadataTable(datasource_id=ds_id, table_name="fact_old",
                          table_comment="旧表", enabled=False, source="synced"),
        ])
        await s.commit()

    # mock fetch_table_columns：fact_power 返 1 字段，其他返空（验证调过）
    async def fake_fetch(engine, table_name, schema=None):
        if table_name == "fact_power":
            return [{"name": "kwh", "type": "BIGINT", "comment": "度数"}]
        return []
    monkeypatch.setattr("src.tools.metadata.fetch_table_columns", fake_fetch)
    return ds_id


@pytest.mark.asyncio
async def test_list_only_enabled_tables(db):
    """enabled=True 的表才返回；字段从 mock 的 fetch_table_columns 实时拉。"""
    tables = await _list_enabled_tables(db, FakeEngine())
    assert {t["table_name"] for t in tables} == {"fact_power"}   # fact_old 被 enabled=False 过滤
    assert tables[0]["table_comment"] == "发电量"
    assert tables[0]["columns"][0]["name"] == "kwh"
    assert tables[0]["columns"][0]["comment"] == "度数"
    assert tables[0]["columns"][0]["type"] == "BIGINT"


@pytest.mark.asyncio
async def test_handler_returns_enabled_tables(db):
    class Ctx: pass
    res = await query_metadata({"datasource_id": db}, Ctx(), None)
    parsed = json.loads(res.summary)
    assert parsed["tables"][0]["table_name"] == "fact_power"   # P1c：结构变 tables/relations
    assert parsed["relations"] == []   # 未配关联时是空数组，不是缺键


@pytest.mark.asyncio
async def test_handler_no_enabled_tables(db):
    async with AsyncSessionFactory() as s:
        (await s.execute(MetadataTable.__table__.update().where(
            MetadataTable.table_name == "fact_power").values(enabled=False)))
        await s.commit()
    class Ctx: pass
    res = await query_metadata({"datasource_id": db}, Ctx(), None)
    assert "没有勾选" in res.summary


@pytest.mark.asyncio
async def test_handler_defaults_to_first_datasource(db):
    """未传 datasource_id 时取第一个数据源（单源默认）。"""
    class Ctx: pass
    res = await query_metadata({}, Ctx(), None)
    parsed = json.loads(res.summary)
    assert parsed["tables"][0]["table_name"] == "fact_power"


@pytest.mark.asyncio
async def test_metadata_includes_relations(db):
    """P1c：已配的逻辑关联随 metadata 返回，供 LLM 生成多表 JOIN。"""
    from src.storage.models import TableRelation
    async with AsyncSessionFactory() as s:
        s.add(TableRelation(datasource_id=db, main_table="fact_power", rel_table="dim_station",
                            join_keys_json='[{"main":"fact_power.sid","rel":"dim_station.id"}]',
                            join_type="left", business_note="场站"))
        await s.commit()
    res = await query_metadata({"datasource_id": db}, type("C", (), {"session_id": "s"})(), None)
    parsed = json.loads(res.summary)
    assert parsed["tables"][0]["table_name"] == "fact_power"   # tables 还在
    assert parsed["relations"][0]["rel_table"] == "dim_station"
    assert parsed["relations"][0]["join_keys"] == [{"main": "fact_power.sid", "rel": "dim_station.id"}]
    assert parsed["relations"][0]["join_type"] == "left"
    assert parsed["relations"][0]["business_note"] == "场站"


@pytest.mark.asyncio
async def test_handler_returns_qualified_name_with_schema(monkeypatch):
    """P1b：schema_name 存在时 table_name 给 `schema.table` 全限定名——
    LLM 直接拿它生成 SELECT FROM schema.table，execute_sql 连实例执行跨库查。"""
    await init_db("sqlite+aiosqlite:///:memory:")
    ds_id = await DataSourceManager().create_datasource(
        dict(name="d", type="starrocks", host="h", port=1, db_name=None,  # 连实例不带库
             username="u", password="p"))
    async with AsyncSessionFactory() as s:
        s.add(MetadataTable(datasource_id=ds_id, schema_name="dw",
                            table_name="fact_power", enabled=True, source="synced"))
        await s.commit()
    # mock fetch：验证 schema 透传
    got_schema = {}
    async def fake_fetch(engine, table_name, schema=None):
        got_schema["schema"] = schema
        return [{"name": "kwh", "type": "BIGINT", "comment": "度数"}]
    monkeypatch.setattr("src.tools.metadata.fetch_table_columns", fake_fetch)

    class Ctx: pass
    res = await query_metadata({"datasource_id": ds_id}, Ctx(), None)
    parsed = json.loads(res.summary)
    assert parsed["tables"][0]["table_name"] == "dw.fact_power"   # 全限定名
    assert got_schema["schema"] == "dw"                           # schema 透传给 fetch


@pytest.mark.asyncio
async def test_handler_bare_name_when_no_schema(db):
    """schema_name=None（老数据）时仍返裸表名——向后兼容。"""
    class Ctx: pass
    res = await query_metadata({"datasource_id": db}, Ctx(), None)
    parsed = json.loads(res.summary)
    assert parsed["tables"][0]["table_name"] == "fact_power"   # 无 schema 前缀


@pytest.mark.asyncio
async def test_handler_no_datasource(monkeypatch):
    """无数据源时给出引导提示。"""
    await init_db("sqlite+aiosqlite:///:memory:")
    class Ctx: pass
    res = await query_metadata({}, Ctx(), None)
    assert "无可用数据源" in res.summary


@pytest.mark.asyncio
async def test_query_metadata_excludes_templates(db):
    """SQL 模板进 system_prompt（orchestrator 注入），query_metadata 不再带 templates 字段。"""
    from src.storage.models import SqlTemplate
    async with AsyncSessionFactory() as s:
        s.add(SqlTemplate(datasource_id=db, name="发电量排名",
                          trigger_keywords="发电量,排名",
                          sql_template="SELECT * FROM fact_power ORDER BY kwh DESC LIMIT :n",
                          enabled=True))
        await s.commit()
    res = await query_metadata({"datasource_id": db}, type("C", (), {"session_id": "s"})(), None)
    parsed = json.loads(res.summary)
    assert "templates" not in parsed   # SQL 模板走 system prompt，不附 query_metadata
