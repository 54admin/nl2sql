import json

import pytest

from src.datasource.manager import DataSourceManager
from src.storage.models import MetadataColumn, MetadataTable
from src.storage.pg_client import AsyncSessionFactory, init_db
from src.tools.metadata import _list_enabled_tables, query_metadata


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("NL2SQL_DS_KEY", Fernet.generate_key().decode())


@pytest.fixture
async def db():
    """预置 1 数据源 + 2 表（1 enabled 1 disabled）+ 字段。"""
    await init_db("sqlite+aiosqlite:///:memory:")
    ds_id = await DataSourceManager().create_datasource(
        dict(name="d", type="starrocks", host="h", port=1, db_name="db",
             username="u", password="p"))
    async with AsyncSessionFactory() as s:
        mt1 = MetadataTable(datasource_id=ds_id, table_name="fact_power",
                            table_comment="发电量", enabled=True, source="synced")
        mt2 = MetadataTable(datasource_id=ds_id, table_name="fact_old",
                            table_comment="旧表", enabled=False, source="synced")
        s.add_all([mt1, mt2])
        await s.flush()
        s.add(MetadataColumn(table_id=mt1.id, column_name="kwh",
                             column_comment="度数", data_type="BIGINT", source="synced"))
        await s.commit()
    return ds_id


@pytest.mark.asyncio
async def test_list_only_enabled_tables(db):
    tables = await _list_enabled_tables(db)
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
    assert parsed[0]["table_name"] == "fact_power"


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
    assert parsed[0]["table_name"] == "fact_power"


@pytest.mark.asyncio
async def test_handler_no_datasource(monkeypatch):
    """无数据源时给出引导提示。"""
    await init_db("sqlite+aiosqlite:///:memory:")
    class Ctx: pass
    res = await query_metadata({}, Ctx(), None)
    assert "无可用数据源" in res.summary
