import pytest

from src.datasource.manager import DataSourceManager
from src.datasource.metadata_sync import fetch_objects, fetch_table_columns, sync_metadata
from src.storage.models import Datasource, MetadataColumn, MetadataTable
from src.storage.pg_client import AsyncSessionFactory, init_db


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("NL2SQL_DS_KEY", Fernet.generate_key().decode())


class FakeEngine:
    """假业务库 engine：connect() → FakeConn，run_sync 调度到 collect_fn/fetch_fn 返回预设数据。"""
    def __init__(self, fetched=None, cols=None):
        self._fetched = fetched
        self._cols = cols
    def connect(self):
        return FakeConn(self._fetched, self._cols)


class FakeConn:
    def __init__(self, fetched, cols):
        self._fetched = fetched
        self._cols = cols
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        pass
    async def run_sync(self, fn, *args, **kwargs):
        # sync 走 _collect_sync（返 fetched）；fetch_columns 走 _get_cols（返 cols）
        # 额外 args/kwargs 接 schema_name 等（不真正调 fn，直接返预设数据）
        return self._fetched if self._fetched is not None else self._cols


@pytest.fixture
async def db():
    await init_db("sqlite+aiosqlite:///:memory:")
    mgr = DataSourceManager()
    ds_id = await mgr.create_datasource(
        dict(name="d", type="starrocks", host="h", port=1, db_name="db",
             username="u", password="p", sync_scope="fact_,dim_"))
    return ds_id


@pytest.mark.asyncio
async def test_sync_inserts_new_tables_only(db):
    """同步只拉表名清单——fetched 不含 columns，写库不写 metadata_columns。"""
    fetched = [
        {"table": "fact_power", "kind": "table", "comment": "发电量事实表"}]
    res = await sync_metadata(db, FakeEngine(fetched=fetched), "fact_,dim_")
    assert res["added"] == 1          # 只计 1 张表，不再 +字段
    async with AsyncSessionFactory() as s:
        tables = (await s.execute(MetadataTable.__table__.select())).all()
        cols = (await s.execute(MetadataColumn.__table__.select())).all()
        assert {t.table_name for t in tables} == {"fact_power"}
        assert cols == []              # 字段不再同步时存
        assert tables[0].enabled is False   # 白名单默认不参与
        assert tables[0].kind == "table"


@pytest.mark.asyncio
async def test_sync_records_view_kind(db):
    """fetched kind=view 写库后持久化（PG 迁移前的 SQLite 路径）。"""
    fetched = [{"table": "v_monthly_power", "kind": "view", "comment": "月度视图"}]
    await sync_metadata(db, FakeEngine(fetched=fetched), "v_,fact_")
    async with AsyncSessionFactory() as s:
        row = (await s.execute(MetadataTable.__table__.select())).first()
        assert row.kind == "view"


@pytest.mark.asyncio
async def test_sync_keeps_manual_table_override(db):
    """source=manual 的整表不被同步覆盖（注释/kind 不动）。"""
    # 先同步一次（synced）
    await sync_metadata(db, FakeEngine(fetched=[
        {"table": "fact_power", "kind": "table", "comment": "旧注释"}]), "fact_")
    # 改成 manual + 手写注释
    async with AsyncSessionFactory() as s:
        (await s.execute(MetadataTable.__table__.update().where(
            MetadataTable.table_name == "fact_power").values(
                table_comment="手写表注释", source="manual")))
        await s.commit()
    # 再同步，传新注释/新 kind；manual 表不被覆盖，计入 skipped
    res = await sync_metadata(db, FakeEngine(fetched=[
        {"table": "fact_power", "kind": "view", "comment": "新注释"}]), "fact_")
    assert res["updated"] == 0 and res["skipped"] == 1
    async with AsyncSessionFactory() as s:
        row = (await s.execute(MetadataTable.__table__.select())).first()
        assert row.table_comment == "手写表注释"
        assert row.kind == "table"          # 没被改成 view
        assert row.source == "manual"


@pytest.mark.asyncio
async def test_sync_updates_synced_table(db):
    """source=synced 的表，注释/kind 变更会被更新。"""
    await sync_metadata(db, FakeEngine(fetched=[
        {"table": "fact_power", "kind": "table", "comment": "旧"}]), "fact_")
    res = await sync_metadata(db, FakeEngine(fetched=[
        {"table": "fact_power", "kind": "view", "comment": "新注释"}]), "fact_")
    assert res["updated"] == 1
    async with AsyncSessionFactory() as s:
        row = (await s.execute(MetadataTable.__table__.select())).first()
        assert row.table_comment == "新注释"
        assert row.kind == "view"


@pytest.mark.asyncio
async def test_sync_scope_filters(db):
    """sync_scope 外的表不同步。"""
    fetched = [
        {"table": "fact_power", "kind": "table", "comment": ""},
        {"table": "ods_raw", "kind": "table", "comment": ""},]   # 不在 fact_,dim_ 范围
    await sync_metadata(db, FakeEngine(fetched=fetched), "fact_,dim_")
    async with AsyncSessionFactory() as s:
        names = {t.table_name for t in (await s.execute(
            MetadataTable.__table__.select())).all()}
        assert names == {"fact_power"}   # ods_raw 被过滤


def test_collect_sync_handles_comment_fallback(monkeypatch):
    """_collect_sync：get_table_comment 异常→""；正常返回字符串。"""
    from src.datasource.metadata_sync import _collect_sync

    class FakeInspector:
        def get_table_names(self): return ["t1"]
        def get_view_names(self): return []
        def get_table_comment(self, name): raise RuntimeError("不支持")  # 异常→""

    monkeypatch.setattr("src.datasource.metadata_sync.inspect",
                        lambda conn: FakeInspector())
    result = _collect_sync(None)
    assert result == [{"table": "t1", "kind": "table", "comment": ""}]


def test_collect_sync_filters_system_tables(monkeypatch):
    """_collect_sync 自动过滤系统库/表（information_schema/mysql/performance_schema/sys）。
    业务表保留，系统库.表 / 系统库名 / 大小写混写都过滤；不带 schema 的普通业务表名不误伤。"""
    from src.datasource.metadata_sync import _collect_sync

    class FakeInspector:
        def get_table_names(self):
            return ["fact_power",                          # 业务表，保留
                    "dim_station",                         # 业务表，保留
                    "information_schema.tables",           # 系统库.表，过滤
                    "mysql.user",                          # 系统库.表，过滤
                    "performance_schema.setup_instruments",# 过滤
                    "sys.config",                          # 系统库.表，过滤
                    "MYSQL",                               # 系统库名（大写），过滤
                    "Information_Schema.foo"]              # 大小写混写，过滤
        def get_view_names(self): return []
        def get_table_comment(self, name): return ""

    monkeypatch.setattr("src.datasource.metadata_sync.inspect",
                        lambda conn: FakeInspector())
    result = _collect_sync(None)
    assert {r["table"] for r in result} == {"fact_power", "dim_station"}


def test_collect_sync_includes_views(monkeypatch):
    """_collect_sync 同时拉表和视图（视图也进问数元数据），kind 区分。"""
    from src.datasource.metadata_sync import _collect_sync

    class FakeInspector:
        def get_table_names(self): return ["fact_power"]
        def get_view_names(self): return ["v_monthly_power"]   # 视图
        def get_table_comment(self, name): return "注释"

    monkeypatch.setattr("src.datasource.metadata_sync.inspect",
                        lambda conn: FakeInspector())
    result = _collect_sync(None)
    assert {r["table"] for r in result} == {"fact_power", "v_monthly_power"}   # 表+视图都进
    by_name = {r["table"]: r for r in result}
    assert by_name["fact_power"]["kind"] == "table"
    assert by_name["v_monthly_power"]["kind"] == "view"


@pytest.mark.asyncio
async def test_fetch_table_columns_returns_columns():
    """fetch_table_columns 走 engine.connect().run_sync → _get_cols，返回字段列表。"""
    cols = [{"name": "kwh", "type": "BIGINT", "comment": "度数"},
            {"name": "sid", "type": "VARCHAR(32)", "comment": ""}]
    got = await fetch_table_columns(FakeEngine(cols=cols), "fact_power")
    assert got == cols


@pytest.mark.asyncio
async def test_fetch_table_columns_fallback_for_view(monkeypatch):
    """Inspector.get_columns 对 StarRocks 视图返空 → fallback SHOW FULL COLUMNS 拿字段名/类型/注释。
    复现真实 bug：视图字段拉不到（空），靠 SHOW FULL COLUMNS 兜底。"""
    class FakeInspector:
        def get_columns(self, table_name, schema=None): return []   # 视图拉空 → 触发 fallback

    class FakeRow:
        def __init__(self, m): self._mapping = m

    class FakeResult:
        def fetchall(self):
            return [FakeRow({"Field": "kwh", "Type": "BIGINT", "Comment": "度数"}),
                    FakeRow({"Field": "sid", "Type": "VARCHAR(32)", "Comment": ""})]

    class FakeSyncConn:
        def execute(self, stmt): return FakeResult()

    class FakeConn:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def run_sync(self, fn, *a, **kw): return fn(FakeSyncConn())

    class FakeEngine:
        def connect(self): return FakeConn()

    monkeypatch.setattr("src.datasource.metadata_sync.inspect",
                        lambda conn: FakeInspector())
    got = await fetch_table_columns(FakeEngine(), "v_power", "ods")
    assert got == [
        {"name": "kwh", "type": "BIGINT", "comment": "度数"},
        {"name": "sid", "type": "VARCHAR(32)", "comment": ""},
    ]


@pytest.mark.asyncio
async def test_sync_with_schema_name_writes_schema_field(db):
    """sync_metadata 传 schema_name 时：拉指定库表 + metadata_tables.schema_name 持久化。"""
    fetched = [{"table": "fact_power", "kind": "table", "comment": ""}]
    res = await sync_metadata(db, FakeEngine(fetched=fetched), None, schema_name="dw")
    assert res["added"] == 1
    async with AsyncSessionFactory() as s:
        row = (await s.execute(MetadataTable.__table__.select())).first()
        assert row.schema_name == "dw"      # 写入了 schema_name
        assert row.table_name == "fact_power"


@pytest.mark.asyncio
async def test_sync_same_table_in_different_schemas(db):
    """同源同表名跨库共存：schema_name 区分（uq_ds_schema_table 不冲突）。"""
    fetched = [{"table": "t", "kind": "table", "comment": ""}]
    await sync_metadata(db, FakeEngine(fetched=fetched), None, schema_name="dw1")
    await sync_metadata(db, FakeEngine(fetched=fetched), None, schema_name="dw2")
    async with AsyncSessionFactory() as s:
        rows = (await s.execute(MetadataTable.__table__.select())).all()
        assert {r.schema_name for r in rows} == {"dw1", "dw2"}   # 两条共存
        assert len(rows) == 2


def test_collect_sync_passes_schema_to_inspector(monkeypatch):
    """_collect_sync(sync_conn, schema) 把 schema 透传给 inspector.get_table_names(schema=...)。"""
    from src.datasource.metadata_sync import _collect_sync

    captured = {}
    class FakeInspector:
        def get_table_names(self, schema=None):
            captured["schema"] = schema
            return ["t1"]
        def get_view_names(self, schema=None):
            return []
        def get_table_comment(self, name, schema=None):
            return ""
    monkeypatch.setattr("src.datasource.metadata_sync.inspect",
                        lambda conn: FakeInspector())
    _collect_sync(None, schema="dw2")
    assert captured["schema"] == "dw2"


@pytest.mark.asyncio
async def test_fetch_objects_returns_name_and_kind_only():
    """fetch_objects 实时拉表清单（不写 PG），只返 name/kind——不拉注释（快）。
    FakeConn.run_sync 直接返预设数据（绕过 _fast），所以 fetched 用最终格式模拟。"""
    fetched = [{"name": "fact_power", "kind": "table"},
               {"name": "v_monthly", "kind": "view"}]
    got = await fetch_objects(FakeEngine(fetched=fetched), "dw")
    assert got == [{"name": "fact_power", "kind": "table"},
                   {"name": "v_monthly", "kind": "view"}]


@pytest.mark.asyncio
async def test_fetch_objects_does_not_write_pg(db):
    """fetch_objects 不写 PG——业务库拉完后 PG metadata_tables 应仍为空。"""
    async with AsyncSessionFactory() as s:
        before = (await s.execute(MetadataTable.__table__.select())).all()
    assert before == []
    await fetch_objects(FakeEngine(fetched=[{"name": "t", "kind": "table"}]), "dw")
    async with AsyncSessionFactory() as s:
        after = (await s.execute(MetadataTable.__table__.select())).all()
    assert after == []   # 没写 PG
