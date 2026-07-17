# P0a 基础设施 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建 nl2sql 项目的可测基础设施层——配置加载、日志、Redis（带降级）、PostgreSQL、会话管理、LLM 流式服务——为 P0b 的 Agent 内核打地基。

**Architecture:** Python 3.12 + FastAPI。配置走 YAML + profile 合并 + 环境变量覆盖。Redis 做会话热态（连不上自动降级内存），PG 做持久化（SQLAlchemy async + asyncpg），LLM 用 langchain-openai 的 ChatOpenAI 走 Qwen OpenAI 兼容网关，沿用双键流式工具调用兼容方案。

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x(async), asyncpg, redis, langchain-openai, pyyaml, pydantic, pytest, pytest-asyncio

**对应设计文档：** `docs/superpowers/specs/2026-07-17-nl2sql-ai-wenshu-design.md`

---

## File Structure

```
nl2sql/
├── config/
│   ├── application.yml          # 基线配置
│   └── application-dev.yml      # dev profile 覆盖
├── src/
│   ├── __init__.py
│   ├── config.py                # ApplicationConfig + load_config
│   ├── logging.py               # setup_logging + get_logger
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── redis_client.py      # RedisClient（带内存降级）
│   │   ├── pg_client.py         # SQLAlchemy engine + session factory + init_db
│   │   └── models.py            # ORM 模型
│   ├── memory/
│   │   ├── __init__.py
│   │   └── session.py           # SessionManager
│   └── llm/
│       ├── __init__.py
│       └── service.py           # LLMService + collect_stream_result
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_logging.py
│   ├── test_redis_client.py
│   ├── test_pg_client.py
│   ├── test_session.py
│   ├── test_llm_service.py
│   └── test_smoke.py
├── requirements.txt
├── .gitignore
└── pytest.ini
```

**职责边界：**
- `config.py`：唯一配置入口，产出 `ApplicationConfig` dataclass。
- `logging.py`：统一日志，`get_logger(name)`。
- `storage/redis_client.py`：键值存取，连接失败降级，调用方无感。
- `storage/pg_client.py` + `models.py`：DB 引擎与 ORM 模型分离。
- `memory/session.py`：会话生命周期，组合 Redis（热）+ PG（持久）。
- `llm/service.py`：LLM 调用与流式兼容，隔离 Qwen 的工具调用坑。

---

## Task 1: 项目脚手架 + 配置加载 + 日志

**Files:**
- Create: `requirements.txt`, `.gitignore`, `pytest.ini`, `config/application.yml`, `config/application-dev.yml`
- Create: `src/__init__.py`, `src/config.py`, `src/logging.py`
- Test: `tests/__init__.py`, `tests/conftest.py`, `tests/test_config.py`, `tests/test_logging.py`

- [ ] **Step 1: 初始化 git 与基础文件**

Run:
```bash
cd /Users/liuxiangwu/PycharmProjects/nl2sql
git init
mkdir -p src/storage src/memory src/llm config tests
```

`requirements.txt`:
```
fastapi>=0.110
uvicorn>=0.27
langchain-openai>=0.1
redis>=5.0
SQLAlchemy>=2.0
asyncpg>=0.29
pyyaml>=6.0
pydantic>=2.6
pytest>=8.0
pytest-asyncio>=0.23
```

`.gitignore`:
```
__pycache__/
*.pyc
.venv/
*.db
.env
```

`pytest.ini`:
```
[pytest]
asyncio_mode = auto
testpaths = tests
```

`config/application.yml`:
```yaml
app:
  name: NL2SQL AI问数
  config_dir: config
profiles:
  active: dev
llm:
  api_key: ""
  api_base: "http://10.111.32.151:3001/v1"
  model: Qwen3-235B-A22B-Instruct-2507
  temperature: 0.0
  timeout: 60
redis:
  host: "127.0.0.1"
  port: 6379
  db: 0
  password: ""
postgres:
  host: "127.0.0.1"
  port: 5432
  database: nl2sql
  username: postgres
  password: ""
```

`config/application-dev.yml`:
```yaml
app:
  name: NL2SQL AI问数 (dev)
```

空 `src/__init__.py`、`tests/__init__.py`。

- [ ] **Step 2: 写失败测试 `tests/test_config.py`**

```python
import textwrap
from pathlib import Path

from src.config import load_config


def test_load_config_merges_profile(tmp_path):
    (tmp_path / "application.yml").write_text(textwrap.dedent("""
        app:
          name: base
        profiles:
          active: dev
        llm:
          api_key: ""
          api_base: http://x/v1
          model: base-model
        redis:
          host: base-redis
        postgres:
          database: base-db
    """))
    (tmp_path / "application-dev.yml").write_text("app:\n  name: dev-name\n")

    cfg = load_config(str(tmp_path))

    assert cfg.app.name == "dev-name"          # profile 覆盖
    assert cfg.llm.model == "base-model"        # 基线保留
    assert cfg.redis.host == "base-redis"
    assert cfg.profiles == ["dev"]


def test_load_config_env_override(tmp_path, monkeypatch):
    (tmp_path / "application.yml").write_text(
        "llm:\n  api_key: \"\"\n  api_base: http://x/v1\n  model: m\n"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    cfg = load_config(str(tmp_path), profile=None)
    assert cfg.llm.api_key == "sk-from-env"
```

- [ ] **Step 3: 运行测试验证失败**

Run: `pytest tests/test_config.py -v`
Expected: FAIL（`ModuleNotFoundError: src.config`）

- [ ] **Step 4: 实现 `src/config.py`**

```python
"""配置加载：YAML 基线 + profile 合并 + 环境变量覆盖。"""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class LLMConfig:
    api_key: str = ""
    api_base: str = ""
    model: str = ""
    temperature: float = 0.0
    timeout: int = 60


@dataclass
class RedisConfig:
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    password: str = ""


@dataclass
class PostgresConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "nl2sql"
    username: str = "postgres"
    password: str = ""


@dataclass
class AppConfig:
    name: str = "NL2SQL"
    config_dir: str = "config"


@dataclass
class ApplicationConfig:
    app: AppConfig = field(default_factory=AppConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    profiles: list = field(default_factory=list)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并，override 覆盖 base。"""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _build(d: dict) -> ApplicationConfig:
    return ApplicationConfig(
        app=AppConfig(**d.get("app", {})),
        llm=LLMConfig(**d.get("llm", {})),
        redis=RedisConfig(**d.get("redis", {})),
        postgres=PostgresConfig(**d.get("postgres", {})),
        profiles=d.get("profiles", {}).get("active", []) if isinstance(
            d.get("profiles"), dict) else [],
    )


def load_config(config_dir: str = "config", profile: str | None = None) -> ApplicationConfig:
    """读 application.yml，按 profile 合并 application-{p}.yml，环境变量最后覆盖。"""
    base_path = Path(config_dir) / "application.yml"
    data = yaml.safe_load(base_path.read_text()) if base_path.exists() else {}

    active = profile or data.get("profiles", {}).get("active")
    if active:
        prof_path = Path(config_dir) / f"application-{active}.yml"
        if prof_path.exists():
            data = _deep_merge(data, yaml.safe_load(prof_path.read_text()))

    # 环境变量覆盖（OPENAI_* 覆盖 llm 段）
    if os.getenv("OPENAI_API_KEY"):
        data.setdefault("llm", {})["api_key"] = os.environ["OPENAI_API_KEY"]

    return _build(data)
```

- [ ] **Step 5: 运行测试验证通过**

Run: `pytest tests/test_config.py -v`
Expected: PASS（2 passed）

- [ ] **Step 6: 写失败测试 `tests/test_logging.py`**

```python
import logging

from src.logging import setup_logging, get_logger


def test_get_logger_returns_namespaced_logger():
    setup_logging("DEBUG")
    log = get_logger("sub.module")
    assert log.name == "nl2sql.sub.module"
    assert log.getEffectiveLevel() == logging.DEBUG
```

- [ ] **Step 7: 实现 `src/logging.py`**

```python
"""统一日志。所有 logger 挂在 nl2sql 命名空间下。"""
import logging
import sys

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    root = logging.getLogger("nl2sql")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(handler)
        _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("nl2sql"):
        name = f"nl2sql.{name}"
    return logging.getLogger(name)
```

运行 `pytest tests/test_logging.py -v` → PASS。

- [ ] **Step 8: 提交**

```bash
git add -A
git commit -m "feat(p0a): 配置加载与日志基础设施"
```

---

## Task 2: Redis 客户端（带内存降级）

**Files:**
- Create: `src/storage/__init__.py`, `src/storage/redis_client.py`
- Test: `tests/test_redis_client.py`

**设计要点：** Redis 连不上时自动降级到进程内 dict，调用方无感（满足 spec"优雅降级"）。TTL 在内存模式下用记录的过期时间戳近似。

- [ ] **Step 1: 写失败测试**

```python
import pytest

from src.storage.redis_client import RedisClient
from src.config import RedisConfig


@pytest.fixture
def client():
    # 指向不存在的 host，强制走降级路径
    c = RedisClient(RedisConfig(host="unreachable.invalid", port=1))
    return c


@pytest.mark.asyncio
async def test_fallback_set_get(client):
    await client.connect()                 # 连不上，降级
    assert client.available is False
    await client.set("k", "v", ttl=60)
    assert await client.get("k") == "v"


@pytest.mark.asyncio
async def test_fallback_delete(client):
    await client.connect()
    await client.set("k", "v")
    await client.delete("k")
    assert await client.get("k") is None


@pytest.mark.asyncio
async def test_fallback_ttl_expires(client):
    await client.connect()
    await client.set("k", "v", ttl=0)      # ttl=0 立即过期
    assert await client.get("k") is None
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_redis_client.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `src/storage/redis_client.py`**

```python
"""Redis 客户端，连接失败降级到内存 dict。调用方无感。"""
import time

from src.config import RedisConfig
from src.logging import get_logger

log = get_logger(__name__)


class _InMemory:
    """进程内降级后端，近似 TTL。"""

    def __init__(self):
        self._store: dict[str, tuple[str, float]] = {}  # key -> (value, expire_at|0)

    async def get(self, key):
        item = self._store.get(key)
        if not item:
            return None
        value, expire_at = item
        if expire_at and time.monotonic() > expire_at:
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key, value, ttl=None):
        expire_at = time.monotonic() + ttl if ttl and ttl > 0 else 0
        self._store[key] = (value, expire_at)

    async def delete(self, key):
        self._store.pop(key, None)


class RedisClient:
    def __init__(self, config: RedisConfig):
        self._config = config
        self._backend = None
        self.available = False

    async def connect(self):
        try:
            import redis.asyncio as aioredis
            self._backend = aioredis.Redis(
                host=self._config.host, port=self._config.port,
                db=self._config.db, password=self._config.password or None,
                socket_connect_timeout=1)
            await self._backend.ping()
            self.available = True
            log.info("Redis 已连接")
        except Exception as e:
            log.warning("Redis 连接失败，降级到内存后端: %s", e)
            self._backend = _InMemory()
            self.available = False

    async def get(self, key: str):
        return await self._backend.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None):
        if self.available and ttl:
            await self._backend.set(key, value, ex=ttl)
        elif self.available:
            await self._backend.set(key, value)
        else:
            await self._backend.set(key, value, ttl=ttl)

    async def delete(self, key: str):
        await self._backend.delete(key)
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_redis_client.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add src/storage tests/test_redis_client.py
git commit -m "feat(p0a): Redis 客户端带内存降级"
```

---

## Task 3: PostgreSQL 客户端 + ORM 模型

**Files:**
- Create: `src/storage/pg_client.py`, `src/storage/models.py`
- Test: `tests/test_pg_client.py`

**设计要点：** 用 SQLAlchemy 2.x async。单测用 in-memory sqlite 跑（SQLAlchemy 抽象屏蔽 dialect 差异），生产连 PG（asyncpg）。模型覆盖会话/消息/审计/loop checkpoint/结果旁路。

- [ ] **Step 1: 写失败测试**

```python
import pytest

from src.storage.pg_client import init_db, AsyncSessionFactory
from src.storage.models import Session, Message


@pytest.mark.asyncio
async def test_init_db_creates_tables(tmp_path, monkeypatch):
    # 用 sqlite 内存库，验证 schema 能建、能写能读
    monkeypatch.setenv("TEST_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await init_db("sqlite+aiosqlite:///:memory:")

    async with AsyncSessionFactory() as s:
        sess = Session(id="s1", user_id="u1", channel="web", status="idle")
        s.add(sess)
        await s.commit()
        msg = Message(id="m1", session_id="s1", role="user",
                      content="你好", trace_id="t1")
        s.add(msg)
        await s.commit()

        loaded = (await s.execute(
            Session.__table__.select().where(Session.id == "s1")
        )).first()
        assert loaded.user_id == "u1"
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_pg_client.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `src/storage/models.py`**

```python
"""ORM 模型。对应 spec 第 12 章核心表。"""
from datetime import datetime

from sqlalchemy import String, Text, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="idle")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())
    ttl_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))     # system/user/assistant/tool
    content: Mapped[str] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditTrace(Base):
    __tablename__ = "audit_traces"
    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    raw_input: Mapped[str] = mapped_column(Text)
    normalized_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrections_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    knowledge_hits_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attribution_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sse_log_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(nullable=True)
    cost_tokens: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LoopCheckpoint(Base):
    """ask_user 挂起时的 loop 上下文快照。P0b 用。"""
    __tablename__ = "loop_checkpoints"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    messages_json: Mapped[str] = mapped_column(Text)
    pending_tool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class QueryResult(Base):
    """execute_sql 全量结果旁路。P1 用，P0a 先建表。"""
    __tablename__ = "query_results"
    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    columns_json: Mapped[str] = mapped_column(Text)
    rows_json: Mapped[str] = mapped_column(Text)
    total: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

- [ ] **Step 4: 实现 `src/storage/pg_client.py`**

```python
"""PG 引擎 + 会话工厂。生产用 asyncpg，测试可传 sqlite。"""
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine)

from src.config import PostgresConfig
from src.logging import get_logger
from src.storage.models import Base

log = get_logger(__name__)

_engine = None
_AsyncSessionFactory: async_sessionmaker[AsyncSession] | None = None


def _pg_url(config: PostgresConfig) -> str:
    return (f"postgresql+asyncpg://{config.username}:{config.password}"
            f"@{config.host}:{config.port}/{config.database}")


async def init_db(url: str | None = None, config: PostgresConfig | None = None):
    """初始化引擎并建表。url 优先（测试用 sqlite），否则用 config 拼_pg。"""
    global _engine, _AsyncSessionFactory
    target = url or _pg_url(config)
    _engine = create_async_engine(target, echo=False)
    _AsyncSessionFactory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("PG 已初始化: %s", "sqlite" if url else "postgres")


def AsyncSessionFactory() -> AsyncSession:
    """用法: async with AsyncSessionFactory() as s: ..."""
    if _AsyncSessionFactory is None:
        raise RuntimeError("PG 未初始化，请先调用 init_db()")
    return _AsyncSessionFactory()
```

注意：`requirements.txt` 补充 `aiosqlite`（sqlite 异步驱动，测试用）。

更新 `requirements.txt`，追加：
```
aiosqlite>=0.20
```

- [ ] **Step 5: 运行验证通过**

Run: `pip install aiosqlite && pytest tests/test_pg_client.py -v`
Expected: PASS（1 passed）

- [ ] **Step 6: 提交**

```bash
git add src/storage/pg_client.py src/storage/models.py tests/test_pg_client.py requirements.txt
git commit -m "feat(p0a): PG 客户端与 ORM 模型"
```

---

## Task 4: 会话管理 SessionManager

**Files:**
- Create: `src/memory/__init__.py`, `src/memory/session.py`
- Test: `tests/test_session.py`

**设计要点：** Redis 存热态（会话状态 + 最近消息，TTL），PG 持久全量消息。`create_session` 两边都写；`append_message` 两边都写；`get_messages` 优先 Redis 命中。TTL 到期会话可被清理（spec 需求 1.6）。P0a 用 uuid4 生成 id（`Date`/`random` 限制仅限 workflow 脚本，普通代码可用）。

- [ ] **Step 1: 写失败测试**

```python
import json
import pytest

from src.memory.session import SessionManager
from src.storage.redis_client import RedisClient
from src.config import RedisConfig
from src.storage.pg_client import init_db, AsyncSessionFactory


@pytest.fixture
async def mgr():
    await init_db("sqlite+aiosqlite:///:memory:")
    redis = RedisClient(RedisConfig(host="unreachable.invalid", port=1))
    await redis.connect()  # 降级到内存
    return SessionManager(redis)


@pytest.mark.asyncio
async def test_create_and_get_session(mgr):
    sid = await mgr.create_session(user_id="u1", channel="web")
    sess = await mgr.get_session(sid)
    assert sess["user_id"] == "u1"
    assert sess["status"] == "idle"


@pytest.mark.asyncio
async def test_append_and_list_messages(mgr):
    sid = await mgr.create_session(user_id="u1", channel="web")
    await mgr.append_message(sid, role="user", content="你好", trace_id="t1")
    await mgr.append_message(sid, role="assistant", content="在的", trace_id="t1")
    msgs = await mgr.get_messages(sid)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["content"] == "在的"


@pytest.mark.asyncio
async def test_status_persisted(mgr):
    sid = await mgr.create_session(user_id="u1", channel="web")
    await mgr.set_status(sid, "running")
    sess = await mgr.get_session(sid)
    assert sess["status"] == "running"


@pytest.mark.asyncio
async def test_delete_session(mgr):
    sid = await mgr.create_session(user_id="u1", channel="web")
    await mgr.delete_session(sid)
    assert await mgr.get_session(sid) is None
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_session.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `src/memory/session.py`**

```python
"""会话管理：Redis 热态 + PG 持久。"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from src.logging import get_logger
from src.storage.models import Session as SessionRow, Message
from src.storage.pg_client import AsyncSessionFactory
from src.storage.redis_client import RedisClient

log = get_logger(__name__)

SESSION_TTL = 3600  # 秒，需求 1.6 长时间无操作清空
SESSION_KEY = "session:{sid}"
MSGS_KEY = "session:{sid}:messages"


class SessionManager:
    def __init__(self, redis: RedisClient):
        self._redis = redis

    async def create_session(self, user_id: str, channel: str) -> str:
        sid = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        ttl_at = now + timedelta(seconds=SESSION_TTL)
        # PG 持久
        async with AsyncSessionFactory() as s:
            s.add(SessionRow(id=sid, user_id=user_id, channel=channel,
                             status="idle", ttl_at=ttl_at))
            await s.commit()
        # Redis 热态
        await self._redis.set(SESSION_KEY.format(sid=sid),
                              json.dumps({"user_id": user_id, "channel": channel,
                                          "status": "idle"}),
                              ttl=SESSION_TTL)
        await self._redis.set(MSGS_KEY.format(sid=sid), json.dumps([]),
                              ttl=SESSION_TTL)
        log.info("创建会话 %s (user=%s)", sid, user_id)
        return sid

    async def get_session(self, sid: str) -> dict | None:
        raw = await self._redis.get(SESSION_KEY.format(sid=sid))
        if raw:
            return json.loads(raw)
        # Redis 未命中，回查 PG 并回填
        async with AsyncSessionFactory() as s:
            row = (await s.execute(
                SessionRow.__table__.select().where(SessionRow.id == sid)
            )).first()
            if not row:
                return None
            data = {"user_id": row.user_id, "channel": row.channel,
                    "status": row.status}
        await self._redis.set(SESSION_KEY.format(sid=sid),
                              json.dumps(data), ttl=SESSION_TTL)
        return data

    async def set_status(self, sid: str, status: str):
        sess = await self.get_session(sid)
        if not sess:
            return
        sess["status"] = status
        await self._redis.set(SESSION_KEY.format(sid=sid),
                              json.dumps(sess), ttl=SESSION_TTL)
        async with AsyncSessionFactory() as s:
            row = await s.get(SessionRow, sid)
            if row:
                row.status = status
                await s.commit()

    async def append_message(self, sid: str, role: str, content: str,
                             trace_id: str):
        mid = uuid.uuid4().hex
        # PG
        async with AsyncSessionFactory() as s:
            s.add(Message(id=mid, session_id=sid, role=role,
                          content=content, trace_id=trace_id))
            await s.commit()
        # Redis 最近消息（热态）
        msgs = json.loads(await self._redis.get(MSGS_KEY.format(sid=sid)) or "[]")
        msgs.append({"role": role, "content": content})
        await self._redis.set(MSGS_KEY.format(sid=sid), json.dumps(msgs),
                              ttl=SESSION_TTL)

    async def get_messages(self, sid: str) -> list[dict]:
        raw = await self._redis.get(MSGS_KEY.format(sid=sid))
        if raw:
            return json.loads(raw)
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(
                Message.__table__.select().where(Message.session_id == sid)
                .order_by(Message.created_at)
            )).all()
            msgs = [{"role": r.role, "content": r.content} for r in rows]
        await self._redis.set(MSGS_KEY.format(sid=sid), json.dumps(msgs),
                              ttl=SESSION_TTL)
        return msgs

    async def delete_session(self, sid: str):
        await self._redis.delete(SESSION_KEY.format(sid=sid))
        await self._redis.delete(MSGS_KEY.format(sid=sid))
        async with AsyncSessionFactory() as s:
            row = await s.get(SessionRow, sid)
            if row:
                await s.delete(row)
                await s.commit()
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_session.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add src/memory tests/test_session.py
git commit -m "feat(p0a): 会话管理 Redis 热态+PG 持久"
```

---

## Task 5: LLM 服务（Qwen 流式兼容）

**Files:**
- Create: `src/llm/__init__.py`, `src/llm/service.py`
- Test: `tests/test_llm_service.py`

**设计要点：** 沿用 nl2sql 2/agent_platform 验证过的 `collect_stream_result` 双键兼容方案——Qwen3 流式工具调用参数在 `tool_call_chunks` 里用 `args` 键，部分场景首块为空，需 post-loop 兜底从任意块的 `tool_calls[i].args` 取。单测用 fake chunk 流，不打真实网关。

- [ ] **Step 1: 写失败测试**

```python
import pytest
from dataclasses import dataclass

from src.llm.service import LLMService, collect_stream_result


@dataclass
class FakeChunk:
    """模拟 langchain AIMessageChunk 的工具调用流式分片。"""
    tool_call_chunks: list = None
    tool_calls: list = None
    content: str = ""


def test_collect_stream_result_merges_streamed_args():
    # Qwen3 场景：首块 args=None，后续块增量拼接
    chunks = [
        FakeChunk(tool_call_chunks=[{"name": "execute_sql", "args": None,
                                     "index": 0, "id": "call_1"}]),
        FakeChunk(tool_call_chunks=[{"args": '{"sql": "SELECT', "index": 0}]),
        FakeChunk(tool_call_chunks=[{"args": ' 1"}', "index": 0}]),
    ]
    result = collect_stream_result(chunks)
    assert result["name"] == "execute_sql"
    assert result["id"] == "call_1"
    assert result["arguments"] == '{"sql": "SELECT 1"}'


def test_collect_stream_result_fallback_on_empty():
    # 流式块全空，从 tool_calls[0].args 兜底
    chunks = [
        FakeChunk(tool_call_chunks=[{"name": "finish", "args": None,
                                     "index": 0, "id": "call_2"}],
                  tool_calls=[{"name": "finish", "args": {"ok": True}, "id": "call_2"}]),
    ]
    result = collect_stream_result(chunks)
    assert result["arguments"] == '{"ok": true}'


def test_collect_stream_result_no_tool_call():
    chunks = [FakeChunk(tool_call_chunks=None, content="你好")]
    result = collect_stream_result(chunks)
    assert result is None  # 无工具调用
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_llm_service.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `src/llm/service.py`**

```python
"""LLM 服务：ChatOpenAI 封装 + Qwen 流式工具调用兼容。"""
import json

from src.config import LLMConfig
from src.logging import get_logger

log = get_logger(__name__)


def collect_stream_result(chunks: list) -> dict | None:
    """合并流式工具调用分片。

    Qwen3-235B 经 OpenAI 兼容网关流式时：
    - tool_call_chunks[0].args 首块常为 None
    - 实际参数在后续块的 tool_call_chunks 增量到达
    - 兜底：流式仍空则用任意块的 tool_calls[i].args

    返回 {id, name, arguments(json 字符串)} 或 None（无工具调用）。
    """
    merged_args = {}
    name = None
    call_id = None

    for chunk in chunks:
        tcc = getattr(chunk, "tool_call_chunks", None) or []
        for tc in tcc:
            if tc.get("name"):
                name = tc["name"]
            if tc.get("id"):
                call_id = tc["id"]
            # 兼容 args / arguments 两种键
            arg = tc.get("args")
            if arg is None:
                arg = tc.get("arguments")
            if isinstance(arg, str) and arg:
                idx = tc.get("index", 0)
                merged_args[idx] = merged_args.get(idx, "") + arg

    if not name and not merged_args:
        return None

    arguments = merged_args.get(0, "")
    # 兜底：流式仍为空，从 tool_calls 取
    if not arguments:
        for chunk in chunks:
            tcs = getattr(chunk, "tool_calls", None) or []
            for tc in tcs:
                args = tc.get("args") or tc.get("arguments")
                if args:
                    arguments = json.dumps(args, ensure_ascii=False)
                    break
            if arguments:
                break

    return {"id": call_id, "name": name, "arguments": arguments}


class LLMService:
    def __init__(self, config: LLMConfig):
        self._config = config
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from langchain_openai import ChatOpenAI
            self._client = ChatOpenAI(
                api_key=config_api_key(self._config),
                base_url=self._config.api_base,
                model=self._config.model,
                temperature=self._config.temperature,
                timeout=self._config.timeout,
                streaming=True,
            )
        return self._client

    async def chat(self, messages: list[dict], tools: list | None = None):
        """非流式一次调用（loop 主用），返回完整响应。"""
        client = self._ensure_client()
        kwargs = {"messages": messages}
        if tools:
            kwargs["tools"] = tools
        return await client.ainvoke(**kwargs)

    async def chat_stream(self, messages: list[dict], tools: list | None = None):
        """流式生成，yield chunk。调用方自行 collect。"""
        client = self._ensure_client()
        kwargs = {"messages": messages}
        if tools:
            kwargs["tools"] = tools
        async for chunk in client.astream(**kwargs):
            yield chunk


def config_api_key(cfg: LLMConfig) -> str:
    key = cfg.api_key or ""
    if not key:
        log.warning("LLM api_key 为空，确认环境变量 OPENAI_API_KEY 已设置")
    return key
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_llm_service.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add src/llm tests/test_llm_service.py
git commit -m "feat(p0a): LLM 服务与 Qwen 流式工具调用兼容"
```

---

## Task 6: 冒烟集成测试

**Files:**
- Create: `tests/conftest.py`（补充）, `tests/test_smoke.py`

**设计要点：** 串联 Task 1-5，验证基础设施能协同：加载配置 → 初始化日志/PG → 建会话 → 持久化消息 → 回读。LLM 不打真实网关，只验证服务能构造（不调用）。

- [ ] **Step 1: 写集成测试**

`tests/conftest.py`（如已存在则合并）:
```python
import pytest

from src.logging import setup_logging


@pytest.fixture(autouse=True)
def _logging():
    setup_logging("DEBUG")
```

`tests/test_smoke.py`:
```python
import pytest

from src.config import load_config
from src.logging import get_logger
from src.storage.pg_client import init_db
from src.storage.redis_client import RedisClient
from src.memory.session import SessionManager
from src.llm.service import LLMService

log = get_logger(__name__)


@pytest.mark.asyncio
async def test_infrastructure_wires_together(tmp_path, monkeypatch):
    # 用临时配置目录，避免依赖真实 config/application.yml
    (tmp_path / "application.yml").write_text(
        "llm:\n  api_key: sk-test\n  api_base: http://x/v1\n  model: m\n"
        "profiles:\n  active: dev\n")
    cfg = load_config(str(tmp_path))

    # PG（sqlite）
    await init_db("sqlite+aiosqlite:///:memory:")
    # Redis（降级）
    redis = RedisClient(cfg.redis)
    await redis.connect()
    # 会话
    mgr = SessionManager(redis)
    sid = await mgr.create_session(user_id="u1", channel="web")
    await mgr.append_message(sid, "user", "你好", trace_id="t1")
    msgs = await mgr.get_messages(sid)
    assert len(msgs) == 1

    # LLM 服务能构造（不打网关）
    svc = LLMService(cfg.llm)
    assert svc._config.model == "m"

    log.info("冒烟通过: session=%s msgs=%d", sid, len(msgs))
```

- [ ] **Step 2: 运行全部测试**

Run: `pytest -v`
Expected: 全部 PASS（config 2 + logging 1 + redis 3 + pg 1 + session 4 + llm 3 + smoke 1 = 15 passed）

- [ ] **Step 3: 提交**

```bash
git add tests/test_smoke.py tests/conftest.py
git commit -m "test(p0a): 基础设施冒烟集成测试"
```

---

## Self-Review

**1. Spec 覆盖（P0a 范围）：**
- 配置三层/优雅降级 → Task 1（YAML+profile+env）、Task 2（Redis 降级）✓
- Redis 热态 + PG 持久 → Task 2/3/4 ✓
- 会话隔离/TTL/状态 → Task 4 ✓
- LLM 流式兼容（Qwen 双键）→ Task 5 ✓
- 核心数据模型（sessions/messages/audit/checkpoint/result）→ Task 3 ✓

P0a 不覆盖：ToolRegistry/Agent Loop/状态机/SSE/路由/工具/spike —— 这些属 P0b，下个 plan。

**2. 占位符扫描：** 无 TBD/TODO，每个步骤含可运行代码与命令。

**3. 类型一致性：** 
- `load_config` → `ApplicationConfig`（含 `llm/redis/postgres`），Task 4/5/6 一致使用 ✓
- `RedisClient.get/set/delete` 签名跨 Task 2/4 一致 ✓
- `SessionManager.create_session/get_session/set_status/append_message/get_messages/delete_session` 签名 Task 4 定义、Task 6 使用一致 ✓
- `LLMService.chat/chat_stream`、`collect_stream_result` 签名一致 ✓
- `AsyncSessionFactory()` 用法（`async with`）跨 Task 3/4/6 一致 ✓

**注意点（执行时关注）：**
- Task 5 的 `LLMService._ensure_client` 仅在真实调用时才 import `langchain_openai`，测试不触发，故 test_llm_service 不依赖该包。
- PG 单测用 sqlite，生产 PG 用 asyncpg；`DateTime`/`func.now()` 在 sqlite 也工作。
- `redis`、`langchain-openai`、`asyncpg` 在 `pip install -r requirements.txt` 时安装。

---

## Execution Handoff

Plan 已保存到 `docs/superpowers/plans/2026-07-17-p0a-infrastructure.md`。两种执行方式：

**1. Subagent-Driven（推荐）** — 每个 Task 派一个新 subagent 实现，任务间审查，迭代快

**2. Inline Execution** — 本会话内逐 Task 执行，批量推进 + 检查点

选哪种？选定后我用对应子技能开始执行 P0a。
