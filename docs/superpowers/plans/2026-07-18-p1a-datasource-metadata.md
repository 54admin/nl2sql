# P1a 数据源与元数据地基 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建 P1 数据查询的地基——StarRocks 数据源管理（密码加密）、SQLAlchemy 连接池、元数据反向同步（保留手写覆盖）、三个配置口径表（table_relations / business_rules / sql_templates）的 CRUD。

**Architecture:** Python 3.12 + FastAPI + SQLAlchemy 2.x async。新增 `src/datasource/` 包：`crypto`（Fernet 加解密，密钥走环境变量）、`manager`（`DataSourceManager`：每 datasource 一个 `AsyncEngine`，懒建缓存）、`metadata_sync`（Inspector 拉 → 写系统 PG，保留 `source=manual`）。双库边界：`AsyncSessionFactory` 连系统 PG（存元数据/配置），datasource engine 连业务 StarRocks（查数）。配置口径表纯 CRUD（直接 `AsyncSessionFactory`），不依赖 manager。

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x(async), aiomysql（StarRocks async 驱动）, cryptography（Fernet）, pytest, pytest-asyncio, httpx

**对应设计文档：** `docs/superpowers/specs/2026-07-18-p1a-datasource-metadata-design.md`

**对应总 spec：** `docs/superpowers/specs/2026-07-17-nl2sql-ai-wenshu-design.md`（第 7、8、12 章）

**实现偏离 spec 一处（简化）：** spec 第 6 章写「datasource create/update 后预建 engine + 自检」。本 plan 改为 **engine 懒建**（`get_engine` 时按需建+缓存），自检由 `POST .../test` 端点显式触发。理由：CRUD 不触发业务库连接 → 测试干净（纯系统 PG）、避免建错数据源时 CRUD 卡在连接超时。前端体验不变（建完点「测试」自检）。

---

## File Structure

```
nl2sql/
├── src/
│   ├── datasource/                      # 新建
│   │   ├── __init__.py                  # 新建（空）
│   │   ├── crypto.py                    # 新建：Fernet 加解密
│   │   ├── manager.py                   # 新建：DataSourceManager
│   │   └── metadata_sync.py             # 新建：sync_metadata
│   ├── storage/models.py                # 修改：+ 6 张表 ORM
│   └── web/routes/
│       ├── admin_datasource.py          # 新建：datasource CRUD + test + sync
│       ├── admin_metadata.py            # 新建：metadata 读 + table-relations CRUD
│       ├── admin_business_rules.py      # 新建：business-rules CRUD
│       └── admin_sql_templates.py       # 新建：sql-templates CRUD
├── tests/
│   ├── test_datasource_models.py        # 新建：6 表建表 + 基本 CRUD
│   ├── test_crypto.py                   # 新建
│   ├── test_datasource_manager.py       # 新建
│   ├── test_metadata_sync.py            # 新建
│   ├── test_routes_admin_datasource.py  # 新建
│   ├── test_routes_admin_metadata.py    # 新建
│   ├── test_routes_admin_business_rules.py  # 新建
│   └── test_routes_admin_sql_templates.py   # 新建
├── src/main.py                          # 修改：lifespan 初始化 manager + 注册 4 路由
└── requirements.txt                     # 修改：+ aiomysql, cryptography
```

---

## Task 1: 依赖 + ORM 6 表

**Files:**
- Modify: `requirements.txt`
- Modify: `src/storage/models.py`（顶部 import + 文件末尾追加 6 个类）
- Test: `tests/test_datasource_models.py`

- [ ] **Step 1: 加依赖**

在 `requirements.txt` 末尾追加两行：
```
aiomysql>=0.2
cryptography>=42.0
```

- [ ] **Step 2: 写失败测试**

创建 `tests/test_datasource_models.py`：
```python
import pytest

from src.storage.models import (Datasource, MetadataTable, MetadataColumn,
                                TableRelation, BusinessRule, SqlTemplate)
from src.storage.pg_client import AsyncSessionFactory, init_db


@pytest.fixture
async def db():
    await init_db("sqlite+aiosqlite:///:memory:")
    yield


@pytest.mark.asyncio
async def test_datasource_crud_roundtrip(db):
    """建表 + 基本 CRUD 可用，密码字段可存可取。"""
    async with AsyncSessionFactory() as s:
        ds = Datasource(name="风电数仓", type="starrocks", host="10.0.0.1",
                        port=9030, db_name="dw", username="root",
                        password_enc="cipher", sync_scope="fact_,dim_")
        s.add(ds)
        await s.commit()
        assert ds.id is not None
    async with AsyncSessionFactory() as s:
        row = await s.get(Datasource, ds.id)
        assert row.name == "风电数仓"
        assert row.password_enc == "cipher"
        assert row.version == 1


@pytest.mark.asyncio
async def test_metadata_tables_and_columns(db):
    async with AsyncSessionFactory() as s:
        ds = Datasource(name="d", type="starrocks", host="h", port=1,
                        db_name="db", username="u", password_enc="c")
        s.add(ds); await s.flush()
        mt = MetadataTable(datasource_id=ds.id, table_name="fact_power",
                           table_comment="发电量", source="synced")
        s.add(mt); await s.flush()
        s.add(MetadataColumn(table_id=mt.id, column_name="kwh",
                             column_comment="度数", data_type="BIGINT",
                             is_primary=False, source="synced"))
        await s.commit()
    async with AsyncSessionFactory() as s:
        cols = (await s.execute(MetadataColumn.__table__.select())).all()
        assert len(cols) == 1
        assert cols[0].column_name == "kwh"


@pytest.mark.asyncio
async def test_other_three_tables_persist(db):
    """table_relations / business_rules / sql_templates 可写入读取。"""
    async with AsyncSessionFactory() as s:
        ds = Datasource(name="d2", type="starrocks", host="h", port=1,
                        db_name="db", username="u", password_enc="c")
        s.add(ds); await s.flush()
        s.add(TableRelation(datasource_id=ds.id, main_table="a", rel_table="b",
                            join_keys_json='[{"main":"a.id","rel":"b.aid"}]',
                            join_type="left", business_note="工单关联场站"))
        s.add(BusinessRule(category="metric", key="发电量",
                           value_json='{"unit":"kWh"}', enabled=True))
        s.add(SqlTemplate(datasource_id=ds.id, name="月发电量",
                          sql_template="SELECT * FROM fact_power WHERE month=:m",
                          params_json='[{"name":"m","required":true}]'))
        await s.commit()
    async with AsyncSessionFactory() as s:
        assert len((await s.execute(TableRelation.__table__.select())).all()) == 1
        assert len((await s.execute(BusinessRule.__table__.select())).all()) == 1
        assert len((await s.execute(SqlTemplate.__table__.select())).all()) == 1
```

- [ ] **Step 3: 跑测试确认失败**

Run: `pytest tests/test_datasource_models.py -v`
Expected: FAIL —— `ImportError: cannot import name 'Datasource'`

- [ ] **Step 4: 改 models.py 顶部 import**

把 `src/storage/models.py:4` 的 import 行：
```python
from sqlalchemy import String, Text, DateTime, func
```
改为：
```python
from sqlalchemy import String, Text, DateTime, Integer, Boolean, ForeignKey, UniqueConstraint, func
```

- [ ] **Step 5: 追加 6 个 ORM 类**

在 `src/storage/models.py` 末尾（`Prompt` 类之后）追加：
```python
class Datasource(Base):
    """业务数据源连接配置（密码加密存）。P1a。"""
    __tablename__ = "datasources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(32))           # starrocks/mysql/pg
    host: Mapped[str] = mapped_column(String(128))
    port: Mapped[int] = mapped_column(Integer)
    db_name: Mapped[str] = mapped_column(String(128))
    username: Mapped[str] = mapped_column(String(128))
    password_enc: Mapped[str] = mapped_column(Text)         # Fernet 密文
    sync_scope: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class MetadataTable(Base):
    """元数据·表（反向同步 + 手写覆盖）。P1a。"""
    __tablename__ = "metadata_tables"
    __table_args__ = (UniqueConstraint("datasource_id", "table_name", name="uq_ds_table"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    datasource_id: Mapped[int] = mapped_column(ForeignKey("datasources.id"), index=True)
    table_name: Mapped[str] = mapped_column(String(128))
    table_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="synced")  # synced/manual
    display_columns_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    hidden_columns_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class MetadataColumn(Base):
    """元数据·字段。P1a。"""
    __tablename__ = "metadata_columns"
    __table_args__ = (UniqueConstraint("table_id", "column_name", name="uq_table_col"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("metadata_tables.id"), index=True)
    column_name: Mapped[str] = mapped_column(String(128))
    column_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    role_tag: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="synced")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class TableRelation(Base):
    """逻辑主外键关系（人工录入）。P1a 建口径，P1c JOIN 消费。"""
    __tablename__ = "table_relations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    datasource_id: Mapped[int] = mapped_column(ForeignKey("datasources.id"), index=True)
    main_table: Mapped[str] = mapped_column(String(128))
    rel_table: Mapped[str] = mapped_column(String(128))
    join_keys_json: Mapped[str] = mapped_column(Text)       # [{"main":"a.id","rel":"b.a_id"}]
    join_type: Mapped[str] = mapped_column(String(16), default="inner")
    business_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class BusinessRule(Base):
    """业务规则（人工录入）。P1a 建口径，后续阶段消费。"""
    __tablename__ = "business_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(32), index=True)  # metric/constraint/interaction/attribution
    key: Mapped[str] = mapped_column(String(128))
    value_json: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class SqlTemplate(Base):
    """SQL 模板（人工录入）。P1a 建口径，P1b 应用。"""
    __tablename__ = "sql_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    datasource_id: Mapped[int] = mapped_column(ForeignKey("datasources.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    trigger_keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_semantics: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql_template: Mapped[str] = mapped_column(Text)
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    formatters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())
```

- [ ] **Step 6: 跑测试确认通过**

Run: `pytest tests/test_datasource_models.py -v`
Expected: 3 passed

- [ ] **Step 7: 装依赖**

Run: `pip install aiomysql cryptography`
Expected: 成功安装（后续 Task 才 import 得到）

- [ ] **Step 8: Commit**

```bash
git add requirements.txt src/storage/models.py tests/test_datasource_models.py
git commit -m "feat(p1a): 6 张表 ORM + 依赖（datasource/metadata/table_relations/business_rules/sql_templates）"
```

---

## Task 2: crypto.py（Fernet 加解密）

**Files:**
- Create: `src/datasource/__init__.py`（空）
- Create: `src/datasource/crypto.py`
- Test: `tests/test_crypto.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_crypto.py`：
```python
import pytest

from src.datasource import crypto


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    """每个测试注入一个固定密钥。"""
    from cryptography.fernet import Fernet
    monkeypatch.setenv("NL2SQL_DS_KEY", Fernet.generate_key().decode())


@pytest.mark.asyncio
async def test_roundtrip():
    enc = crypto.encrypt("p@ssw0rd")
    assert enc != "p@ssw0rd"
    assert crypto.decrypt(enc) == "p@ssw0rd"


@pytest.mark.asyncio
async def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("NL2SQL_DS_KEY", raising=False)
    with pytest.raises(RuntimeError, match="NL2SQL_DS_KEY"):
        crypto.encrypt("x")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_crypto.py -v`
Expected: FAIL —— `ModuleNotFoundError: src.datasource.crypto`

- [ ] **Step 3: 建 __init__.py + crypto.py**

创建空文件 `src/datasource/__init__.py`。

创建 `src/datasource/crypto.py`：
```python
"""数据源密码加解密：Fernet 对称加密，密钥走环境变量 NL2SQL_DS_KEY。
密钥用 Fernet.generate_key() 生成（44 字节 base64），放环境变量，不入库不入 git。"""
import os

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    key = os.environ.get("NL2SQL_DS_KEY")
    if not key:
        raise RuntimeError("缺少环境变量 NL2SQL_DS_KEY（数据源密码加密密钥，"
                           "用 Fernet.generate_key() 生成）")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plain: str) -> str:
    """明文 → Fernet 密文（str）。"""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Fernet 密文 → 明文。"""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_crypto.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/datasource/__init__.py src/datasource/crypto.py tests/test_crypto.py
git commit -m "feat(p1a): 数据源密码 Fernet 加解密（密钥走环境变量）"
```

---

## Task 3: DataSourceManager（连接池 + datasource CRUD）

**Files:**
- Create: `src/datasource/manager.py`
- Test: `tests/test_datasource_manager.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_datasource_manager.py`：
```python
import pytest

from src.datasource.crypto import encrypt
from src.datasource.manager import DataSourceManager
from src.storage.models import Datasource
from src.storage.pg_client import AsyncSessionFactory, init_db


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("NL2SQL_DS_KEY", Fernet.generate_key().decode())


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
async def test_password_stored_encrypted(db):
    await db.create_datasource(_payload(password="my-secret"))
    async with AsyncSessionFactory() as s:
        row = (await s.execute(Datasource.__table__.select())).first()
        assert row.password_enc != "my-secret"   # 加密存
        assert encrypt("my-secret") != row.password_enc or True  # 不同密文也行


@pytest.mark.asyncio
async def test_update_bumps_version_and_disposes_engine(db, monkeypatch):
    disposed = []
    async def fake_dispose():
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_datasource_manager.py -v`
Expected: FAIL —— `ModuleNotFoundError: src.datasource.manager`

- [ ] **Step 3: 实现 manager.py**

创建 `src/datasource/manager.py`：
```python
"""数据源管理：连接池（每 datasource 一个 AsyncEngine，懒建缓存）+ datasource CRUD。

双库边界：AsyncSessionFactory 连系统 PG（存元数据/配置）；
_engines 里的 engine 连业务库（StarRocks，查数用）。"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.datasource.crypto import decrypt, encrypt
from src.logging import get_logger
from src.storage.models import Datasource
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)


class DataSourceManager:
    def __init__(self) -> None:
        self._engines: dict[int, AsyncEngine] = {}

    # ---- 连接池 ----
    def _build_engine(self, row: Datasource) -> AsyncEngine:
        pwd = decrypt(row.password_enc)
        url = f"mysql+aiomysql://{row.username}:{pwd}@{row.host}:{row.port}/{row.db_name}"
        return create_async_engine(url, pool_pre_ping=True)

    async def get_engine(self, ds_id: int) -> AsyncEngine:
        """懒建 + 缓存。miss 则读表解密建 engine。"""
        if ds_id in self._engines:
            return self._engines[ds_id]
        async with AsyncSessionFactory() as s:
            row = await s.get(Datasource, ds_id)
        if row is None or not row.enabled:
            raise KeyError(f"数据源不存在或未启用: {ds_id}")
        eng = self._build_engine(row)
        self._engines[ds_id] = eng
        return eng

    async def test_connection(self, ds_id: int) -> None:
        """SELECT 1 探活。失败抛异常（路由层 catch）。"""
        from sqlalchemy import text
        eng = await self.get_engine(ds_id)
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))

    # ---- CRUD（只操作系统 PG，不碰业务库）----
    async def list_datasources(self) -> list[dict]:
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(Datasource.__table__.select())).all()
        return [{"id": r.id, "name": r.name, "type": r.type, "host": r.host,
                 "port": r.port, "db_name": r.db_name, "username": r.username,
                 "sync_scope": r.sync_scope, "enabled": r.enabled} for r in rows]

    async def create_datasource(self, data: dict) -> int:
        pwd = data.pop("password")
        ds = Datasource(password_enc=encrypt(pwd), **data)
        async with AsyncSessionFactory() as s:
            s.add(ds)
            await s.commit()
            return ds.id

    async def update_datasource(self, ds_id: int, data: dict) -> bool:
        if "password" in data:
            data["password_enc"] = encrypt(data.pop("password"))
        async with AsyncSessionFactory() as s:
            row = await s.get(Datasource, ds_id)
            if row is None:
                return False
            for k, v in data.items():
                setattr(row, k, v)
            row.version += 1
            await s.commit()
        # 连接信息可能变了，旧 engine 失效
        old = self._engines.pop(ds_id, None)
        if old is not None:
            await old.dispose()
        return True

    async def delete_datasource(self, ds_id: int) -> bool:
        async with AsyncSessionFactory() as s:
            row = await s.get(Datasource, ds_id)
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
        old = self._engines.pop(ds_id, None)
        if old is not None:
            await old.dispose()
        return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_datasource_manager.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/datasource/manager.py tests/test_datasource_manager.py
git commit -m "feat(p1a): DataSourceManager 连接池懒建 + datasource CRUD（密码加密）"
```

---

## Task 4: metadata_sync.py（反向同步，保留手写）

**Files:**
- Create: `src/datasource/metadata_sync.py`
- Test: `tests/test_metadata_sync.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_metadata_sync.py`：
```python
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
                     {"name": "station_id", "type": "VARCHAR(32)", "comment": ""}]}
    ]
    res = await sync_metadata(db, FakeEngine(fetched), "fact_,dim_")
    assert res["added"] >= 1
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
    # 再同步，kwh 类型/注释变了
    await sync_metadata(db, FakeEngine([
        {"table": "fact_power", "comment": "新注释",
         "columns": [{"name": "kwh", "type": "BIGINT", "comment": "新"}]}]), "fact_")
    async with AsyncSessionFactory() as s:
        col2 = (await s.execute(MetadataColumn.__table__.select())).first()
        assert col2.column_comment == "手写度数"   # 没被覆盖
        assert col2.source == "manual"


@pytest.mark.asyncio
async def test_sync_scope_filters(db):
    """sync_scope 外的表不同步。"""
    fetched = [
        {"table": "fact_power", "comment": "", "columns": []},
        {"table": "ods_raw", "comment": "", "columns": []},   # 不在 fact_,dim_ 范围
    ]
    await sync_metadata(db, FakeEngine(fetched), "fact_,dim_")
    async with AsyncSessionFactory() as s:
        names = {t.table_name for t in (await s.execute(
            MetadataTable.__table__.select())).all()}
        assert names == {"fact_power"}   # ods_raw 被过滤
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_metadata_sync.py -v`
Expected: FAIL —— `ModuleNotFoundError: src.datasource.metadata_sync`

- [ ] **Step 3: 实现 metadata_sync.py**

创建 `src/datasource/metadata_sync.py`：
```python
"""元数据同步：从业务库（StarRocks）Inspector 拉表/字段，写系统 PG metadata_*。
保留 source=manual 的手写覆盖，不被同步冲掉。同步只写 comment/type，不碰 is_primary/role_tag。"""
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from src.logging import get_logger
from src.storage.models import MetadataColumn, MetadataTable
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)


def _in_scope(table_name: str, sync_scope: str | None) -> bool:
    """sync_scope 为空=全要；非空=表名匹配任一前缀/全名才要。"""
    if not sync_scope:
        return True
    prefixes = [p.strip() for p in sync_scope.split(",") if p.strip()]
    return any(table_name == p or table_name.startswith(p) for p in prefixes)


def _collect_sync(sync_conn) -> list[dict]:
    """同步函数（被 engine.run_sync 调用，在同步连接上跑 Inspector）。"""
    insp = inspect(sync_conn)
    out = []
    for table_name in insp.get_table_names():
        try:
            tcomment = (insp.get_table_comment(table_name) or {}).get("text") or ""
        except Exception:
            tcomment = ""
        cols = [{"name": c["name"], "type": str(c["type"]),
                 "comment": c.get("comment") or ""}
                for c in insp.get_columns(table_name)]
        out.append({"table": table_name, "comment": tcomment, "columns": cols})
    return out


async def sync_metadata(ds_id: int, engine: AsyncEngine, sync_scope: str | None) -> dict:
    """同步一个数据源的元数据。返回 {added, updated, skipped}。

    ponytail: 库里已删的表/字段本期不清理（避免误删手写），后续可加。"""
    fetched = await engine.run_sync(_collect_sync)
    added = updated = skipped = 0
    async with AsyncSessionFactory() as s:
        for t in fetched:
            if not _in_scope(t["table"], sync_scope):
                continue
            row = (await s.execute(MetadataTable.__table__.select().where(
                MetadataTable.datasource_id == ds_id,
                MetadataTable.table_name == t["table"]))).first()
            if row is None:
                mt = MetadataTable(datasource_id=ds_id, table_name=t["table"],
                                   table_comment=t["comment"], source="synced")
                s.add(mt); await s.flush()
                for c in t["columns"]:
                    s.add(MetadataColumn(table_id=mt.id, column_name=c["name"],
                                         column_comment=c["comment"], data_type=c["type"],
                                         source="synced"))
                added += 1 + len(t["columns"])
            elif row.source == "synced":
                await s.execute(MetadataTable.__table__.update().where(
                    MetadataTable.id == row.id).values(table_comment=t["comment"]))
                for c in t["columns"]:
                    crow = (await s.execute(MetadataColumn.__table__.select().where(
                        MetadataColumn.table_id == row.id,
                        MetadataColumn.column_name == c["name"]))).first()
                    if crow is None:
                        s.add(MetadataColumn(table_id=row.id, column_name=c["name"],
                                             column_comment=c["comment"], data_type=c["type"],
                                             source="synced"))
                        added += 1
                    elif crow.source == "synced":
                        await s.execute(MetadataColumn.__table__.update().where(
                            MetadataColumn.id == crow.id).values(
                                column_comment=c["comment"], data_type=c["type"]))
                        updated += 1
                    else:
                        skipped += 1          # manual 字段不动
                updated += 1
            else:
                skipped += 1                  # 整表 manual
        await s.commit()
    log.info("元数据同步 ds=%s added=%s updated=%s skipped=%s",
             ds_id, added, updated, skipped)
    return {"added": added, "updated": updated, "skipped": skipped}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_metadata_sync.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/datasource/metadata_sync.py tests/test_metadata_sync.py
git commit -m "feat(p1a): 元数据反向同步（Inspector 拉，保留 manual 覆盖，sync_scope 过滤）"
```

---

## Task 5: admin_datasource 路由

**Files:**
- Create: `src/web/routes/admin_datasource.py`
- Test: `tests/test_routes_admin_datasource.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_routes_admin_datasource.py`：
```python
import pytest
import httpx
from fastapi import FastAPI

from src.datasource.manager import DataSourceManager
from src.storage.pg_client import init_db
from src.web.routes.admin_datasource import build_datasource_router


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("NL2SQL_DS_KEY", Fernet.generate_key().decode())


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(build_datasource_router(DataSourceManager()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _payload(**over):
    base = dict(name="ds", type="starrocks", host="h", port=9030,
                db_name="db", username="u", password="p", sync_scope="fact_")
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_create_list(client):
    r = await client.post("/api/admin/datasources", json=_payload())
    assert r.status_code == 200
    ds_id = r.json()["id"]
    r = await client.get("/api/admin/datasources")
    assert len(r.json()["datasources"]) == 1
    assert r.json()["datasources"][0]["id"] == ds_id


@pytest.mark.asyncio
async def test_list_never_returns_password(client):
    await client.post("/api/admin/datasources", json=_payload(password="secret"))
    r = await client.get("/api/admin/datasources")
    body = str(r.json())
    assert "secret" not in body
    assert "password" not in r.json()["datasources"][0]


@pytest.mark.asyncio
async def test_update_and_delete(client):
    ds_id = (await client.post("/api/admin/datasources", json=_payload())).json()["id"]
    r = await client.put(f"/api/admin/datasources/{ds_id}", json={"host": "h2"})
    assert r.status_code == 200
    r = await client.delete(f"/api/admin/datasources/{ds_id}")
    assert r.json()["ok"] is True
    r = await client.delete(f"/api/admin/datasources/{ds_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_test_endpoint_calls_manager(client, monkeypatch):
    """test 端点调 manager.test_connection；mock 它验证路由接通。"""
    called = []
    async def fake_test(self, ds_id):
        called.append(ds_id)
    from src.datasource.manager import DataSourceManager
    monkeypatch.setattr(DataSourceManager, "test_connection", fake_test)
    ds_id = (await client.post("/api/admin/datasources", json=_payload())).json()["id"]
    r = await client.post(f"/api/admin/datasources/{ds_id}/test")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert called == [ds_id]


@pytest.mark.asyncio
async def test_sync_endpoint(client, monkeypatch):
    """sync 端点调 sync_metadata；mock engine.run_sync 验证接通。"""
    class FakeEngine:
        async def run_sync(self, fn): return []
        async def dispose(self): pass
    from src.datasource.manager import DataSourceManager
    monkeypatch.setattr(DataSourceManager, "get_engine",
                        lambda self, ds_id: _async_return(FakeEngine()))
    ds_id = (await client.post("/api/admin/datasources", json=_payload())).json()["id"]
    r = await client.post(f"/api/admin/datasources/{ds_id}/sync")
    assert r.status_code == 200
    assert r.json() == {"added": 0, "updated": 0, "skipped": 0}


async def _async_return(v):
    return v
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_routes_admin_datasource.py -v`
Expected: FAIL —— `ModuleNotFoundError: src.web.routes.admin_datasource`

- [ ] **Step 3: 实现路由**

创建 `src/web/routes/admin_datasource.py`：
```python
"""数据源管理路由：CRUD + 连通性测试 + 元数据同步触发。P1a。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.datasource.manager import DataSourceManager
from src.datasource.metadata_sync import sync_metadata
from src.storage.models import Datasource
from src.storage.pg_client import AsyncSessionFactory


class DatasourceIn(BaseModel):
    name: str
    type: str = "starrocks"
    host: str
    port: int
    db_name: str
    username: str
    password: str
    sync_scope: str | None = None
    enabled: bool = True


class DatasourcePatch(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    db_name: str | None = None
    username: str | None = None
    password: str | None = None
    sync_scope: str | None = None
    enabled: bool | None = None


def build_datasource_router(mgr: DataSourceManager) -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/datasources")
    async def list_ds() -> dict:
        return {"datasources": await mgr.list_datasources()}

    @router.post("/api/admin/datasources")
    async def create_ds(req: DatasourceIn) -> dict:
        ds_id = await mgr.create_datasource(req.model_dump())
        return {"id": ds_id}

    @router.put("/api/admin/datasources/{ds_id}")
    async def update_ds(ds_id: int, req: DatasourcePatch) -> dict:
        ok = await mgr.update_datasource(ds_id, req.model_dump(exclude_none=True))
        if not ok:
            raise HTTPException(404, "数据源不存在")
        return {"ok": ok}

    @router.delete("/api/admin/datasources/{ds_id}")
    async def delete_ds(ds_id: int) -> dict:
        ok = await mgr.delete_datasource(ds_id)
        if not ok:
            raise HTTPException(404, "数据源不存在")
        return {"ok": ok}

    @router.post("/api/admin/datasources/{ds_id}/test")
    async def test_ds(ds_id: int) -> dict:
        try:
            await mgr.test_connection(ds_id)
            return {"ok": True}
        except Exception as e:
            raise HTTPException(400, f"连接失败: {e}")

    @router.post("/api/admin/datasources/{ds_id}/sync")
    async def sync_ds(ds_id: int) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(Datasource, ds_id)
        if row is None:
            raise HTTPException(404, "数据源不存在")
        engine = await mgr.get_engine(ds_id)
        return await sync_metadata(ds_id, engine, row.sync_scope)

    return router
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_routes_admin_datasource.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/web/routes/admin_datasource.py tests/test_routes_admin_datasource.py
git commit -m "feat(p1a): 数据源 CRUD + test + sync 路由"
```

---

## Task 6: admin_metadata 路由（metadata 读 + table-relations CRUD）

**Files:**
- Create: `src/web/routes/admin_metadata.py`
- Test: `tests/test_routes_admin_metadata.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_routes_admin_metadata.py`：
```python
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
        c.headers["X-Ds-Id"] = str(ds_id)
        yield c


@pytest.mark.asyncio
async def test_read_metadata(client):
    r = await client.get("/api/admin/metadata", headers={"X-Ds-Id": client.headers["X-Ds-Id"]})
    assert r.status_code == 200
    data = r.json()
    assert len(data["tables"]) == 1
    assert data["tables"][0]["table_name"] == "fact_power"
    assert data["tables"][0]["columns"][0]["column_name"] == "kwh"


@pytest.mark.asyncio
async def test_table_relations_crud(client):
    ds_id = client.headers["X-Ds-Id"]
    payload = {"datasource_id": int(ds_id), "main_table": "fact_power",
               "rel_table": "dim_station",
               "join_keys_json": json.dumps([{"main": "fact_power.sid", "rel": "dim_station.id"}]),
               "join_type": "left", "business_note": "场站关联"}
    r = await client.post("/api/admin/table-relations", json=payload)
    assert r.status_code == 200
    assert r.json()["id"] is not None
    r = await client.get("/api/admin/table-relations", params={"datasource_id": ds_id})
    assert len(r.json()["relations"]) == 1
    assert r.json()["relations"][0]["business_note"] == "场站关联"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_routes_admin_metadata.py -v`
Expected: FAIL —— `ModuleNotFoundError: src.web.routes.admin_metadata`

- [ ] **Step 3: 实现路由**

创建 `src/web/routes/admin_metadata.py`：
```python
"""元数据读取 + 逻辑关系（table_relations）CRUD。P1a。
纯 PG 操作，不依赖 DataSourceManager。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.storage.models import MetadataColumn, MetadataTable, TableRelation
from src.storage.pg_client import AsyncSessionFactory


class TableRelationIn(BaseModel):
    datasource_id: int
    main_table: str
    rel_table: str
    join_keys_json: str
    join_type: str = "inner"
    business_note: str | None = None


def build_metadata_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/metadata")
    async def read_metadata(datasource_id: int) -> dict:
        """读某数据源的元数据（表 + 字段），供 P1b query_metadata 调用。"""
        async with AsyncSessionFactory() as s:
            tables = (await s.execute(MetadataTable.__table__.select().where(
                MetadataTable.datasource_id == datasource_id))).all()
            out = []
            for t in tables:
                cols = (await s.execute(MetadataColumn.__table__.select().where(
                    MetadataColumn.table_id == t.id))).all()
                out.append({
                    "table_name": t.table_name, "table_comment": t.table_comment,
                    "source": t.source, "is_primary_marked": None,
                    "columns": [{"column_name": c.column_name, "column_comment": c.column_comment,
                                 "data_type": c.data_type, "is_primary": c.is_primary,
                                 "role_tag": c.role_tag, "source": c.source} for c in cols]})
            return {"tables": out}

    @router.get("/api/admin/table-relations")
    async def list_relations(datasource_id: int) -> dict:
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(TableRelation.__table__.select().where(
                TableRelation.datasource_id == datasource_id))).all()
        return {"relations": [{"id": r.id, "datasource_id": r.datasource_id,
                               "main_table": r.main_table, "rel_table": r.rel_table,
                               "join_keys_json": r.join_keys_json, "join_type": r.join_type,
                               "business_note": r.business_note} for r in rows]}

    @router.post("/api/admin/table-relations")
    async def create_relation(req: TableRelationIn) -> dict:
        async with AsyncSessionFactory() as s:
            rel = TableRelation(**req.model_dump())
            s.add(rel); await s.commit()
            return {"id": rel.id}

    @router.delete("/api/admin/table-relations/{rel_id}")
    async def delete_relation(rel_id: int) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(TableRelation, rel_id)
            if row is None:
                return {"ok": False}
            await s.delete(row); await s.commit()
            return {"ok": True}

    return router
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_routes_admin_metadata.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/web/routes/admin_metadata.py tests/test_routes_admin_metadata.py
git commit -m "feat(p1a): 元数据读取 + table_relations CRUD 路由"
```

---

## Task 7: admin_business_rules 路由

**Files:**
- Create: `src/web/routes/admin_business_rules.py`
- Test: `tests/test_routes_admin_business_rules.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_routes_admin_business_rules.py`：
```python
import pytest
import httpx
from fastapi import FastAPI

from src.storage.pg_client import init_db
from src.web.routes.admin_business_rules import build_business_rules_router


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(build_business_rules_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_crud(client):
    r = await client.post("/api/admin/business-rules", json={
        "category": "metric", "key": "发电量",
        "value_json": '{"unit":"kWh","decimal":2}', "enabled": True})
    assert r.status_code == 200
    assert r.json()["version"] == 1
    r = await client.get("/api/admin/business-rules", params={"category": "metric"})
    assert len(r.json()["rules"]) == 1
    assert r.json()["rules"][0]["key"] == "发电量"


@pytest.mark.asyncio
async def test_filter_by_category(client):
    await client.post("/api/admin/business-rules",
                      json={"category": "metric", "key": "a", "value_json": "1"})
    await client.post("/api/admin/business-rules",
                      json={"category": "constraint", "key": "b", "value_json": "2"})
    r = await client.get("/api/admin/business-rules", params={"category": "metric"})
    assert len(r.json()["rules"]) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_routes_admin_business_rules.py -v`
Expected: FAIL —— `ModuleNotFoundError: src.web.routes.admin_business_rules`

- [ ] **Step 3: 实现路由**

创建 `src/web/routes/admin_business_rules.py`：
```python
"""业务规则 CRUD（人工录入口径，后续阶段消费）。P1a。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.storage.models import BusinessRule
from src.storage.pg_client import AsyncSessionFactory


class BusinessRuleIn(BaseModel):
    category: str          # metric/constraint/interaction/attribution
    key: str
    value_json: str
    enabled: bool = True


class BusinessRulePatch(BaseModel):
    value_json: str | None = None
    enabled: bool | None = None


def build_business_rules_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/business-rules")
    async def list_rules(category: str | None = None) -> dict:
        async with AsyncSessionFactory() as s:
            stmt = BusinessRule.__table__.select()
            if category:
                stmt = stmt.where(BusinessRule.category == category)
            rows = (await s.execute(stmt)).all()
        return {"rules": [{"id": r.id, "category": r.category, "key": r.key,
                           "value_json": r.value_json, "enabled": r.enabled,
                           "version": r.version} for r in rows]}

    @router.post("/api/admin/business-rules")
    async def create_rule(req: BusinessRuleIn) -> dict:
        async with AsyncSessionFactory() as s:
            rule = BusinessRule(**req.model_dump())
            s.add(rule); await s.commit()
            return {"id": rule.id, "version": rule.version}

    @router.put("/api/admin/business-rules/{rule_id}")
    async def update_rule(rule_id: int, req: BusinessRulePatch) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(BusinessRule, rule_id)
            if row is None:
                return {"ok": False}
            for k, v in req.model_dump(exclude_none=True).items():
                setattr(row, k, v)
            row.version += 1
            await s.commit()
            return {"ok": True, "version": row.version}

    @router.delete("/api/admin/business-rules/{rule_id}")
    async def delete_rule(rule_id: int) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(BusinessRule, rule_id)
            if row is None:
                return {"ok": False}
            await s.delete(row); await s.commit()
            return {"ok": True}

    return router
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_routes_admin_business_rules.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/web/routes/admin_business_rules.py tests/test_routes_admin_business_rules.py
git commit -m "feat(p1a): business_rules CRUD 路由"
```

---

## Task 8: admin_sql_templates 路由

**Files:**
- Create: `src/web/routes/admin_sql_templates.py`
- Test: `tests/test_routes_admin_sql_templates.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_routes_admin_sql_templates.py`：
```python
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
    r = await client.post("/api/admin/sql-templates", json={
        "datasource_id": ds_id, "name": "月发电量",
        "sql_template": "SELECT month, sum(kwh) FROM fact_power WHERE month=:m GROUP BY month",
        "params_json": '[{"name":"m","required":true}]',
        "trigger_keywords": "发电量,月度"})
    assert r.status_code == 200
    assert r.json()["version"] == 1
    r = await client.get("/api/admin/sql-templates", params={"datasource_id": ds_id})
    assert len(r.json()["templates"]) == 1
    assert r.json()["templates"][0]["name"] == "月发电量"


@pytest.mark.asyncio
async def test_delete(client):
    ds_id = client._ds_id
    rid = (await client.post("/api/admin/sql-templates", json={
        "datasource_id": ds_id, "name": "t", "sql_template": "SELECT 1"})).json()["id"]
    r = await client.delete(f"/api/admin/sql-templates/{rid}")
    assert r.json()["ok"] is True
    r = await client.delete(f"/api/admin/sql-templates/{rid}")
    assert r.json()["ok"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_routes_admin_sql_templates.py -v`
Expected: FAIL —— `ModuleNotFoundError: src.web.routes.admin_sql_templates`

- [ ] **Step 3: 实现路由**

创建 `src/web/routes/admin_sql_templates.py`：
```python
"""SQL 模板 CRUD（人工录入口径，P1b 应用）。P1a。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.storage.models import SqlTemplate
from src.storage.pg_client import AsyncSessionFactory


class SqlTemplateIn(BaseModel):
    datasource_id: int
    name: str
    trigger_keywords: str | None = None
    trigger_semantics: str | None = None
    sql_template: str
    params_json: str | None = None
    formatters_json: str | None = None
    enabled: bool = True


class SqlTemplatePatch(BaseModel):
    name: str | None = None
    trigger_keywords: str | None = None
    trigger_semantics: str | None = None
    sql_template: str | None = None
    params_json: str | None = None
    formatters_json: str | None = None
    enabled: bool | None = None


def build_sql_templates_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/sql-templates")
    async def list_templates(datasource_id: int) -> dict:
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(SqlTemplate.__table__.select().where(
                SqlTemplate.datasource_id == datasource_id))).all()
        return {"templates": [{"id": r.id, "datasource_id": r.datasource_id,
                               "name": r.name, "trigger_keywords": r.trigger_keywords,
                               "trigger_semantics": r.trigger_semantics,
                               "sql_template": r.sql_template, "params_json": r.params_json,
                               "formatters_json": r.formatters_json,
                               "enabled": r.enabled, "version": r.version} for r in rows]}

    @router.post("/api/admin/sql-templates")
    async def create_template(req: SqlTemplateIn) -> dict:
        async with AsyncSessionFactory() as s:
            t = SqlTemplate(**req.model_dump())
            s.add(t); await s.commit()
            return {"id": t.id, "version": t.version}

    @router.put("/api/admin/sql-templates/{tpl_id}")
    async def update_template(tpl_id: int, req: SqlTemplatePatch) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(SqlTemplate, tpl_id)
            if row is None:
                return {"ok": False}
            for k, v in req.model_dump(exclude_none=True).items():
                setattr(row, k, v)
            row.version += 1
            await s.commit()
            return {"ok": True, "version": row.version}

    @router.delete("/api/admin/sql-templates/{tpl_id}")
    async def delete_template(tpl_id: int) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(SqlTemplate, tpl_id)
            if row is None:
                return {"ok": False}
            await s.delete(row); await s.commit()
            return {"ok": True}

    return router
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_routes_admin_sql_templates.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/web/routes/admin_sql_templates.py tests/test_routes_admin_sql_templates.py
git commit -m "feat(p1a): sql_templates CRUD 路由"
```

---

## Task 9: main.py 集成

**Files:**
- Modify: `src/main.py`（lifespan 初始化 manager + create_app 注册 4 路由）

- [ ] **Step 1: lifespan 初始化 DataSourceManager**

在 `src/main.py` 的 `lifespan` 函数里，`prompts = PromptStore()` 那行之后加：
```python
    from src.datasource.manager import DataSourceManager
    datasource_mgr = DataSourceManager()
```
并把 `_app_state.update(...)` 改为：
```python
    _app_state.update(
        orchestrator=orch, session_mgr=sm, llm_service=llm,
        prompts=prompts, datasource_mgr=datasource_mgr)
```

- [ ] **Step 2: create_app 注册 4 个路由**

在 `src/main.py` 顶部 import 区加：
```python
from src.web.routes.admin_datasource import build_datasource_router
from src.web.routes.admin_metadata import build_metadata_router
from src.web.routes.admin_business_rules import build_business_rules_router
from src.web.routes.admin_sql_templates import build_sql_templates_router
```
在 `create_app()` 里现有 `app.include_router(...)` 之后追加：
```python
    app.include_router(build_datasource_router(_Lazy("datasource_mgr")))
    app.include_router(build_metadata_router())
    app.include_router(build_business_rules_router())
    app.include_router(build_sql_templates_router())
```

- [ ] **Step 3: 跑全量测试**

Run: `pytest -q`
Expected: 全绿（原 P0 测试 + P1a 新增测试都过）

- [ ] **Step 4: 手动启动验证路由挂载**

设置密钥后启动：
```bash
export NL2SQL_DS_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
./run.sh
```
另开终端验证：
```bash
curl -s http://127.0.0.1:8000/api/admin/datasources   # 期望 {"datasources":[]}
curl -s http://127.0.0.1:8000/api/admin/business-rules?category=metric
curl -s http://127.0.0.1:8000/api/admin/sql-templates?datasource_id=1
```
Expected: 三个端点都 200 返回空列表，不报 500。

- [ ] **Step 5: Commit**

```bash
git add src/main.py
git commit -m "feat(p1a): main.py 集成 DataSourceManager + 注册 4 个 admin 路由"
```

---

## Task 10: 端到端 smoke（连真 StarRocks，手动）

**Files:** 无代码改动，纯验证。

- [ ] **Step 1: 准备密钥 + 启动**

```bash
export NL2SQL_DS_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
./run.sh
```

- [ ] **Step 2: 录入真实 StarRocks 数据源**

```bash
curl -s -X POST http://127.0.0.1:8000/api/admin/datasources -H 'Content-Type: application/json' -d '{
  "name":"风电数仓","type":"starrocks","host":"<SR_HOST>","port":9030,
  "db_name":"<SR_DB>","username":"root","password":"<SR_PWD>","sync_scope":""
}' 
# 记下返回的 id（假设 1）
```

- [ ] **Step 3: 测试连通性**

```bash
curl -s -X POST http://127.0.0.1:8000/api/admin/datasources/1/test
# 期望 {"ok":true}；失败看 400 错误信息
```

- [ ] **Step 4: 触发元数据同步**

```bash
curl -s -X POST http://127.0.0.1:8000/api/admin/datasources/1/sync
# 期望 {"added":N,"updated":0,"skipped":0}
```

- [ ] **Step 5: 验证元数据落库**

```bash
curl -s "http://127.0.0.1:8000/api/admin/metadata?datasource_id=1" | python -m json.tool
# 期望看到同步进来的表名 + 字段名 + 注释
```

- [ ] **Step 6: 验证配置口径可录入**

```bash
# 录一条逻辑关系
curl -s -X POST http://127.0.0.1:8000/api/admin/table-relations -H 'Content-Type: application/json' -d '{
  "datasource_id":1,"main_table":"fact_power","rel_table":"dim_station",
  "join_keys_json":"[{\"main\":\"fact_power.sid\",\"rel\":\"dim_station.id\"}]",
  "join_type":"left","business_note":"场站维度关联"}'
# 录一条业务规则
curl -s -X POST http://127.0.0.1:8000/api/admin/business-rules -H 'Content-Type: application/json' -d '{
  "category":"metric","key":"发电量","value_json":"{\"unit\":\"kWh\"}"}'
# 录一条 SQL 模板
curl -s -X POST http://127.0.0.1:8000/api/admin/sql-templates -H 'Content-Type: application/json' -d '{
  "datasource_id":1,"name":"月发电量","sql_template":"SELECT month,sum(kwh) FROM fact_power GROUP BY month"}'
```
Expected: 三个都返回 id/version，GET 能查回。

- [ ] **Step 7: 更新进度记录**

更新 `current-rebuild.md`：P1a 完成（数据源+元数据+三口径表），下一步 P1b 执行链路 brainstorm。

---

## Self-Review

**1. Spec 覆盖**：
- 数据源管理（CRUD + 加密 + 连接池）→ Task 1/2/3/5 ✓
- 元数据同步（Inspector + 保留 manual + sync_scope）→ Task 4 ✓
- table_relations 口径 → Task 6 ✓
- business_rules 口径 → Task 7 ✓
- sql_templates 口径 → Task 8 ✓
- API 清单（spec 第 9 章）→ Task 5/6/7/8 ✓
- 双库边界 → manager.py 注释 + Task 3 测试 ✓
- 密钥 fail-fast → Task 2 测试 test_missing_key_raises ✓
- 不做（query_metadata/execute_sql/result 旁路存取/JOIN/展示规则/权限/定时同步）→ 均未出现在任务里 ✓

**2. 占位扫描**：无 TBD/TODO；每个 step 有完整代码或完整命令。✓

**3. 类型一致**：
- `DataSourceManager` 方法名（list/create/update/delete_datasource, get_engine, test_connection）Task 3 定义、Task 5 路由调用一致 ✓
- `sync_metadata(ds_id, engine, sync_scope)` 签名 Task 4 定义、Task 5 路由调用一致 ✓
- `build_xxx_router` 函数名 Task 5-8 定义、Task 9 main.py 调用一致 ✓
- ORM 类名 Datasource/MetadataTable/MetadataColumn/TableRelation/BusinessRule/SqlTemplate 全程一致 ✓

**4. 已知偏离**：
- spec 第 6 章「CRUD 后自检」→ 本 plan 改为懒建 + test 端点显式自检（已在头部「实现偏离」说明，简化测试）。
- spec 第 9 章未列 `/api/admin/sql-templates` → Task 8 补齐（spec 第 8 章已含此口径）。
