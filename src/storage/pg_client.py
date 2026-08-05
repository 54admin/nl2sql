"""PG 引擎 + 会话工厂。生产用 asyncpg。

建表职责：应用启动不碰任何 DDL。生产表由 owner 跑 db/schema.sql 一次建好
（schema.sql 由 scripts/gen_schema.py 从 ORM 编译生成，单一事实源）。
init_db 只建连接池 + 会话工厂，不建表、不迁移、不刷注释。
"""
from urllib.parse import quote_plus

from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine)

from src.config import PostgresConfig
from src.logging import get_logger

log = get_logger(__name__)

_engine = None
_AsyncSessionFactory: async_sessionmaker[AsyncSession] | None = None


def _pg_url(config: PostgresConfig) -> str:
    # 用户名/密码 quote_plus 编码，防止 @:/ 等字符破坏 URL 解析
    user = quote_plus(config.username)
    pwd = quote_plus(config.password)
    port = config.port or 5432
    return (f"postgresql+asyncpg://{user}:{pwd}"
            f"@{config.host}:{port}/{config.database}")


async def init_db(config: PostgresConfig):
    """初始化引擎 + 会话工厂。只建连接，不建表（表由 db/schema.sql 预建）。"""
    global _engine, _AsyncSessionFactory
    _engine = create_async_engine(_pg_url(config), echo=False)
    _AsyncSessionFactory = async_sessionmaker(_engine, expire_on_commit=False)
    log.info("PG 已初始化: %s:%s/%s", config.host, config.port or 5432, config.database)


def AsyncSessionFactory() -> AsyncSession:
    """用法: async with AsyncSessionFactory() as s: ..."""
    if _AsyncSessionFactory is None:
        raise RuntimeError("PG 未初始化，请先调用 init_db()")
    return _AsyncSessionFactory()
