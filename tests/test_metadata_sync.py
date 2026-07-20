import pytest

from src.datasource.manager import DataSourceManager
from src.datasource.metadata_sync import sync_metadata
from src.storage.models import Datasource, MetadataColumn, MetadataTable
from src.storage.pg_client import AsyncSessionFactory, init_db


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("NL2SQL_DS_KEY", Fernet.generate_key().decode())


class FakeEngine:
    """假业务库 engine：run_sync 返回预设的表/字段，不连真库。"""
    def __init__(self, fetched):
        self._fetched = fetched
    async def run_sync(self, fn):
        return self._fetched


@pytest.fixture
async def db():
    await init_db("sqlite+aiosqlite:///:memory:")
    mgr = DataSourceManager()
    ds_id = await mgr.create_datasource(
        dict(name="d", type="starrocks", host="h", port=1, db_name="db",
             username="u", password="p", sync_scope="fact_,dim_"))
    return ds_id


@pytest.mark.asyncio
async def test_sync_inserts_new_tables_and_columns(db):
    fetched = [
        {"table": "fact_power", "comment": "发电量事实表",
         "columns": [{"name": "kwh", "type": "BIGINT", "comment": "度数"},
                     {"name": "station_id", "type": "VARCHAR(32)", "comment": ""}]}]
    res = await sync_metadata(db, FakeEngine(fetched), "fact_,dim_")
    assert res["added"] == 3          # 1 表 + 2 字段，确定值
    async with AsyncSessionFactory() as s:
        tables = (await s.execute(MetadataTable.__table__.select())).all()
        cols = (await s.execute(MetadataColumn.__table__.select())).all()
        assert {t.table_name for t in tables} == {"fact_power"}
        assert {c.column_name for c in cols} == {"kwh", "station_id"}


@pytest.mark.asyncio
async def test_sync_keeps_manual_override(db):
    """source=manual 的字段不被同步覆盖。"""
    # 先正常同步一次
    await sync_metadata(db, FakeEngine([
        {"table": "fact_power", "comment": "旧注释",
         "columns": [{"name": "kwh", "type": "INT", "comment": "旧"}]}]), "fact_")
    # 手动把 kwh 改成 manual + 手写注释
    async with AsyncSessionFactory() as s:
        col = (await s.execute(MetadataColumn.__table__.select())).first()
        (await s.execute(MetadataColumn.__table__.update().where(
            MetadataColumn.id == col.id).values(
                column_comment="手写度数", source="manual")))
        await s.commit()
    # 再同步，kwh 类型/注释变了；手写字段不被覆盖，计入 skipped
    res = await sync_metadata(db, FakeEngine([
        {"table": "fact_power", "comment": "新注释",
         "columns": [{"name": "kwh", "type": "BIGINT", "comment": "新"}]}]), "fact_")
    assert res["updated"] == 1 and res["skipped"] == 1   # 表更新 1，kwh manual 跳过 1
    async with AsyncSessionFactory() as s:
        col2 = (await s.execute(MetadataColumn.__table__.select())).first()
        assert col2.column_comment == "手写度数"   # 没被覆盖
        assert col2.source == "manual"


@pytest.mark.asyncio
async def test_sync_scope_filters(db):
    """sync_scope 外的表不同步。"""
    fetched = [
        {"table": "fact_power", "comment": "", "columns": []},
        {"table": "ods_raw", "comment": "", "columns": []},]   # 不在 fact_,dim_ 范围
    await sync_metadata(db, FakeEngine(fetched), "fact_,dim_")
    async with AsyncSessionFactory() as s:
        names = {t.table_name for t in (await s.execute(
            MetadataTable.__table__.select())).all()}
        assert names == {"fact_power"}   # ods_raw 被过滤


def test_collect_sync_handles_fallbacks(monkeypatch):
    """_collect_sync 的 3 处兜底：get_table_comment 异常→""、comment None→""、type→str。"""
    from sqlalchemy import BigInteger, String

    from src.datasource.metadata_sync import _collect_sync

    class FakeInspector:
        def get_table_names(self): return ["t1"]
        def get_table_comment(self, name): raise RuntimeError("不支持")  # 触发异常兜底
        def get_columns(self, name):
            return [{"name": "c1", "type": BigInteger(), "comment": None},   # None→""
                    {"name": "c2", "type": String(32), "comment": "场站"}]

    monkeypatch.setattr("src.datasource.metadata_sync.inspect",
                        lambda conn: FakeInspector())
    result = _collect_sync(None)   # conn 不重要，inspect(conn) 返回 FakeInspector
    assert result == [{
        "table": "t1", "comment": "",                 # 异常被吞成空
        "columns": [
            {"name": "c1", "type": "BIGINT", "comment": ""},      # None→空 + type 转字符串
            {"name": "c2", "type": "VARCHAR(32)", "comment": "场站"},
        ]}]
