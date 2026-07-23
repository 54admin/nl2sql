import pytest

from src.datasource.manager import DataSourceManager
from src.storage.models import Datasource
from src.storage.pg_client import AsyncSessionFactory, init_db


@pytest.fixture
async def db():
    await init_db("sqlite+aiosqlite:///:memory:")
    return DataSourceManager()


def _payload(**over):
    base = dict(name="ds1", type="starrocks", host="10.0.0.1", port=9030,
                db_name="dw", username="root", password="secret",
                sync_scope="fact_,dim_", enabled=True)
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_create_then_list(db):
    ds_id = await db.create_datasource(_payload())
    assert ds_id is not None
    rows = await db.list_datasources()
    assert len(rows) == 1
    assert rows[0]["name"] == "ds1"
    assert "password_enc" not in rows[0]    # 永不返回密文


@pytest.mark.asyncio
async def test_password_stored_plaintext(db):
    await db.create_datasource(_payload(password="my-secret"))
    async with AsyncSessionFactory() as s:
        row = (await s.execute(Datasource.__table__.select())).first()
        assert row.password_enc == "my-secret"   # 明文存（内网工具去加密）


@pytest.mark.asyncio
async def test_update_bumps_version_and_disposes_engine(db, monkeypatch):
    disposed = []
    async def fake_dispose(*args, **kwargs):
        disposed.append(True)
    # 注入一个假装已缓存的 engine，验证 update 会 dispose
    ds_id = await db.create_datasource(_payload())
    db._engines[ds_id] = type("E", (), {"dispose": fake_dispose})()
    ok = await db.update_datasource(ds_id, {"host": "10.0.0.2"})
    assert ok is True
    assert ds_id not in db._engines          # 缓存清掉
    assert disposed == [True]                # 旧 engine 被 dispose
    async with AsyncSessionFactory() as s:
        row = await s.get(Datasource, ds_id)
        assert row.host == "10.0.0.2"
        assert row.version == 2


@pytest.mark.asyncio
async def test_delete(db):
    ds_id = await db.create_datasource(_payload())
    assert await db.delete_datasource(ds_id) is True
    assert await db.delete_datasource(ds_id) is False  # 已删
    assert await db.list_datasources() == []


@pytest.mark.asyncio
async def test_delete_cascades_metadata(db):
    """删数据源要级联清 metadata_tables/columns/relations/sql_templates，不留孤儿。"""
    from src.storage.models import (MetadataColumn, MetadataTable,
                                    TableRelation, SqlTemplate)
    from src.storage.pg_client import AsyncSessionFactory
    ds_id = await db.create_datasource(_payload())
    async with AsyncSessionFactory() as s:
        mt = MetadataTable(datasource_id=ds_id, schema_name="db1",
                           table_name="fact_a", source="synced")
        s.add(mt); await s.flush()
        s.add(MetadataColumn(table_id=mt.id, column_name="k",
                             source="synced"))
        s.add(TableRelation(datasource_id=ds_id, main_table="a",
                            rel_table="b", join_keys_json="[]"))
        s.add(SqlTemplate(datasource_id=ds_id, name="t",
                          sql_template="SELECT 1"))
        await s.commit()
    assert await db.delete_datasource(ds_id) is True
    async with AsyncSessionFactory() as s:
        assert (await s.execute(MetadataTable.__table__.select().where(
            MetadataTable.datasource_id == ds_id))).all() == []
        assert (await s.execute(MetadataColumn.__table__.select().where(
            MetadataColumn.table_id == mt.id))).all() == []
        assert (await s.execute(TableRelation.__table__.select().where(
            TableRelation.datasource_id == ds_id))).all() == []
        assert (await s.execute(SqlTemplate.__table__.select().where(
            SqlTemplate.datasource_id == ds_id))).all() == []


@pytest.mark.asyncio
async def test_get_engine_lazily_built_and_cached(db, monkeypatch):
    ds_id = await db.create_datasource(_payload())
    built = []
    class FakeEngine:
        async def dispose(self): pass
    def fake_create(url, **kw):
        built.append(url)
        return FakeEngine()
    monkeypatch.setattr("src.datasource.manager.create_async_engine", fake_create)
    e1 = await db.get_engine(ds_id)
    e2 = await db.get_engine(ds_id)
    assert e1 is e2                          # 缓存
    assert len(built) == 1                   # 只建一次
    assert "mysql+aiomysql://root:secret@10.0.0.1:9030/dw" == built[0]


@pytest.mark.asyncio
async def test_get_engine_url_escapes_special_chars(db, monkeypatch):
    # 密码含 @ : / 时必须被 quote_plus 转义，否则 URL 分割错乱
    ds_id = await db.create_datasource(_payload(password="p@ss:w/o"))
    built = []
    class FakeEngine:
        async def dispose(self): pass
    def fake_create(url, **kw):
        built.append(url)
        return FakeEngine()
    monkeypatch.setattr("src.datasource.manager.create_async_engine", fake_create)
    await db.get_engine(ds_id)
    # p@ss:w/o → quote_plus = p%40ss%3Aw%2Fo（@ : / 全部转义）
    assert built[0] == "mysql+aiomysql://root:p%40ss%3Aw%2Fo@10.0.0.1:9030/dw"


@pytest.mark.asyncio
async def test_update_ignores_password_enc_injection(db):
    # trust boundary：请求体塞 {"password_enc":"..."} 必须被忽略，密码只走 password 字段
    ds_id = await db.create_datasource(_payload(password="orig"))
    async with AsyncSessionFactory() as s:
        original = (await s.get(Datasource, ds_id)).password_enc
    ok = await db.update_datasource(ds_id, {"password_enc": "明文攻击", "host": "10.0.0.9"})
    assert ok is True
    async with AsyncSessionFactory() as s:
        row = await s.get(Datasource, ds_id)
        assert row.password_enc == original      # 未被改写
        assert row.password_enc != "明文攻击"     # 明文没入库
        assert row.host == "10.0.0.9"             # 合法字段照常更新


@pytest.mark.asyncio
async def test_delete_disposes_engine(db):
    disposed = []
    async def fake_dispose(*args, **kwargs):
        disposed.append(True)
    ds_id = await db.create_datasource(_payload())
    db._engines[ds_id] = type("E", (), {"dispose": fake_dispose})()
    assert await db.delete_datasource(ds_id) is True
    assert ds_id not in db._engines             # 缓存清掉
    assert disposed == [True]                   # 旧 engine 被 dispose
