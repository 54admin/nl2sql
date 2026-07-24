"""execute_sql 工具测试：mock engine，不连真库。
覆盖：正常返回摘要+result_id / 执行失败回灌让 LLM 自愈 / validate_sql 占位 pass-through / 无 SQL 报错。"""
import json

import pytest

from src.tools.sql_engine import execute_sql, validate_sql


class FakeResult:
    """模拟 sqlalchemy Result：keys() 返回列名，fetchall/fetchmany 返回带 _mapping 的行。"""
    def __init__(self, cols, rows):
        self._cols, self._rows = cols, rows

    def keys(self):
        return self._cols

    def fetchall(self):
        return [type("R", (), {"_mapping": r})() for r in self._rows]

    def fetchmany(self, size=None):
        return self.fetchall()


class FakeConn:
    def __init__(self, cols, rows):
        self._r = FakeResult(cols, rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def execute(self, stmt):
        return self._r


class FakeEngine:
    def __init__(self, cols, rows):
        self._c = FakeConn(cols, rows)

    def connect(self):
        return self._c


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    """manager 在 import 时引用 crypto，但本测试 mock 掉了 get_engine，
    实际不会触发加解密；fixture 留着与 test_query_metadata 同款兜底。"""
    from cryptography.fernet import Fernet
    monkeypatch.setenv("NL2SQL_DS_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _no_sensitive_columns(monkeypatch):
    """默认 _sensitive_columns 返回空集（无 sensitive 配置 = 向后兼容），
    避免每个测试都 mock 一遍；需要 sensitive 行为的测试单独覆盖。"""
    async def _empty(ds_id):
        return set()
    monkeypatch.setattr("src.tools.sql_engine._sensitive_columns", _empty)


class Ctx:
    session_id = "sess-test"


@pytest.mark.asyncio
async def test_execute_returns_summary_with_result_id(monkeypatch):
    async def fake_list(self):
        return [{"id": 1}]

    async def fake_get_engine(self, ds_id):
        return FakeEngine(["kwh"], [{"kwh": 100}, {"kwh": 200}])

    async def fake_save(sid, cols, rows, datasource_id=None):
        return "rid-123"

    monkeypatch.setattr("src.datasource.manager.DataSourceManager.list_datasources", fake_list)
    monkeypatch.setattr("src.datasource.manager.DataSourceManager.get_engine", fake_get_engine)
    monkeypatch.setattr("src.tools.sql_engine.save_result", fake_save)

    res = await execute_sql({"sql": "SELECT kwh FROM fact_power"}, Ctx(), None)
    s = json.loads(res.summary)
    assert s["result_id"] == "rid-123"
    assert s["rows"] == 2
    assert s["columns"] == ["kwh"]
    assert s["preview"] == [{"kwh": 100}, {"kwh": 200}]


@pytest.mark.asyncio
async def test_execute_failure_returns_error_for_self_heal(monkeypatch):
    """执行抛异常时不能炸主链路，错误信息回灌让 LLM 改 SQL 重试。"""
    async def fake_get_engine(self, ds_id):
        raise RuntimeError("连接拒绝")

    async def fake_list(self):
        return [{"id": 1}]

    monkeypatch.setattr("src.datasource.manager.DataSourceManager.list_datasources", fake_list)
    monkeypatch.setattr("src.datasource.manager.DataSourceManager.get_engine", fake_get_engine)

    res = await execute_sql({"sql": "SELECT 1"}, Ctx(), None)
    assert "SQL 执行失败" in res.summary
    assert "连接拒绝" in res.summary


@pytest.mark.asyncio
async def test_validate_sql_blocks_ddl_dml_and_allows_select():
    """P1 SQL 护栏：sqlglot 解析，DDL/DML 拦截，只读 SELECT（含 CTE）放行。"""
    assert validate_sql("DROP TABLE x") is not None
    assert validate_sql("DELETE FROM t") is not None
    assert validate_sql("UPDATE t SET a=1") is not None
    assert validate_sql("CREATE TABLE t(a int)") is not None
    assert validate_sql("INSERT INTO t VALUES(1)") is not None
    assert validate_sql("SELECT 1") is None
    assert validate_sql("WITH cte AS (SELECT 1) SELECT * FROM cte") is None


@pytest.mark.asyncio
async def test_no_sql_returns_error():
    res = await execute_sql({}, Ctx(), None)
    assert "未提供 SQL" in res.summary


@pytest.mark.asyncio
async def test_execute_filters_sensitive_columns(monkeypatch):
    """P1c：role_tag=sensitive 字段从摘要（columns/preview）剔除，不回灌 LLM；
    全量结果（save_result）保留完整列。"""
    async def fake_list(self):
        return [{"id": 1}]

    async def fake_get_engine(self, ds_id):
        return FakeEngine(["kwh", "id_card"], [{"kwh": 1, "id_card": "110xxx"}])

    saved = {}

    async def fake_save(sid, cols, rows, datasource_id=None):
        saved["cols"] = cols
        saved["rows"] = rows
        return "rid"

    async def fake_sensitive(ds_id):
        return {"id_card"}

    monkeypatch.setattr("src.datasource.manager.DataSourceManager.list_datasources", fake_list)
    monkeypatch.setattr("src.datasource.manager.DataSourceManager.get_engine", fake_get_engine)
    monkeypatch.setattr("src.tools.sql_engine.save_result", fake_save)
    monkeypatch.setattr("src.tools.sql_engine._sensitive_columns", fake_sensitive)

    res = await execute_sql({"sql": "SELECT kwh,id_card FROM t"}, Ctx(), None)
    s = json.loads(res.summary)
    assert "id_card" not in s["columns"]    # sensitive 被滤出摘要
    assert "kwh" in s["columns"]
    assert "id_card" not in str(s["preview"])
    assert saved["cols"] == ["kwh", "id_card"]   # 全量结果完整保留（前端要完整数据）


@pytest.mark.asyncio
async def test_execute_no_sensitive_config_passes_through(monkeypatch):
    """role_tag 全空（未配）时不过滤，向后兼容。"""
    async def fake_list(self):
        return [{"id": 1}]

    async def fake_get_engine(self, ds_id):
        return FakeEngine(["kwh"], [{"kwh": 1}])

    async def fake_save(sid, cols, rows, datasource_id=None):
        return "rid"

    async def fake_sensitive(ds_id):
        return set()

    monkeypatch.setattr("src.datasource.manager.DataSourceManager.list_datasources", fake_list)
    monkeypatch.setattr("src.datasource.manager.DataSourceManager.get_engine", fake_get_engine)
    monkeypatch.setattr("src.tools.sql_engine.save_result", fake_save)
    monkeypatch.setattr("src.tools.sql_engine._sensitive_columns", fake_sensitive)

    res = await execute_sql({"sql": "SELECT kwh FROM t"}, Ctx(), None)
    s = json.loads(res.summary)
    assert s["columns"] == ["kwh"]
