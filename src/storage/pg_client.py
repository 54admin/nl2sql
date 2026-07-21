"""PG 引擎 + 会话工厂。生产用 asyncpg，测试可传 sqlite。"""
from urllib.parse import quote_plus

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine)
from sqlalchemy.pool import StaticPool

from src.config import PostgresConfig
from src.logging import get_logger
from src.storage.models import Base

log = get_logger(__name__)

_engine = None
_AsyncSessionFactory: async_sessionmaker[AsyncSession] | None = None


def _pg_url(config: PostgresConfig) -> str:
    # 用户名/密码 quote_plus 编码，防止 @:/ 等字符破坏 URL 解析
    user = quote_plus(config.username)
    pwd = quote_plus(config.password)
    return (f"postgresql+asyncpg://{user}:{pwd}"
            f"@{config.host}:{config.port}/{config.database}")


# 幂等迁移：DBeaver 层级范式（db_name 可空 + metadata_tables 加 schema_name + 唯一约束升级）。
# 仅对 PG 生效，SQLite 测试每次内存库重建不走这里（新模型直接生效）。
# ponytail: 不引 Alembic——一条 ALTER 一个 IF NOT EXISTS，幂等可重复跑。
_PG_MIGRATIONS = [
    "ALTER TABLE datasources ALTER COLUMN db_name DROP NOT NULL",
    "ALTER TABLE metadata_tables ADD COLUMN IF NOT EXISTS schema_name VARCHAR(128)",
    "ALTER TABLE metadata_tables DROP CONSTRAINT IF EXISTS uq_ds_table",
    "ALTER TABLE metadata_tables DROP CONSTRAINT IF EXISTS uq_ds_schema_table",
    """ALTER TABLE metadata_tables
       ADD CONSTRAINT uq_ds_schema_table UNIQUE (datasource_id, schema_name, table_name)""",
    "CREATE INDEX IF NOT EXISTS ix_metadata_tables_schema_name ON metadata_tables(schema_name)",
]


async def init_db(url: str | None = None, config: PostgresConfig | None = None):
    """初始化引擎并建表。url 优先（测试用 sqlite），否则用 config 拼 pg。"""
    global _engine, _AsyncSessionFactory
    target = url or _pg_url(config)
    kwargs = {}
    is_sqlite = "sqlite" in target
    if is_sqlite and ":memory:" in target:
        # 内存 sqlite 必须单连接复用，否则跨 AsyncSession 看不到表
        kwargs = {"poolclass": StaticPool,
                  "connect_args": {"check_same_thread": False}}
    _engine = create_async_engine(target, echo=False, **kwargs)
    _AsyncSessionFactory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if not is_sqlite:
            for stmt in _PG_MIGRATIONS:
                await conn.execute(text(stmt))
    log.info("PG 已初始化: %s", "sqlite" if is_sqlite else "postgres")


def AsyncSessionFactory() -> AsyncSession:
    """用法: async with AsyncSessionFactory() as s: ..."""
    if _AsyncSessionFactory is None:
        raise RuntimeError("PG 未初始化，请先调用 init_db()")
    return _AsyncSessionFactory()
