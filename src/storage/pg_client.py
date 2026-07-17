"""PG 引擎 + 会话工厂。生产用 asyncpg，测试可传 sqlite。"""
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
    return (f"postgresql+asyncpg://{config.username}:{config.password}"
            f"@{config.host}:{config.port}/{config.database}")


async def init_db(url: str | None = None, config: PostgresConfig | None = None):
    """初始化引擎并建表。url 优先（测试用 sqlite），否则用 config 拼 pg。"""
    global _engine, _AsyncSessionFactory
    target = url or _pg_url(config)
    kwargs = {}
    if "sqlite" in target and ":memory:" in target:
        # 内存 sqlite 必须单连接复用，否则跨 AsyncSession 看不到表
        kwargs = {"poolclass": StaticPool,
                  "connect_args": {"check_same_thread": False}}
    _engine = create_async_engine(target, echo=False, **kwargs)
    _AsyncSessionFactory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("PG 已初始化: %s", "sqlite" if url else "postgres")


def AsyncSessionFactory() -> AsyncSession:
    """用法: async with AsyncSessionFactory() as s: ..."""
    if _AsyncSessionFactory is None:
        raise RuntimeError("PG 未初始化，请先调用 init_db()")
    return _AsyncSessionFactory()
