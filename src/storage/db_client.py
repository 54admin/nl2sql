"""平台库引擎 + 会话工厂。支持 PostgreSQL(asyncpg) 和 MySQL(aiomysql)，按 config.type 切换。

建表职责：应用启动不碰任何 DDL。生产表由 owner 跑 db/schema.sql(PG) 或
db/schema_mysql.sql(MySQL) 一次建好（schema 由 scripts/gen_schema*.py 从 ORM 编译）。
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


def _db_url(config: PostgresConfig) -> str:
    """按 config.type 构建连接 URL。type=mysql 走 aiomysql，否则走 asyncpg(PG)。
    用户名/密码 quote_plus 编码，防止 @:/ 等字符破坏 URL 解析。"""
    user = quote_plus(config.username)
    pwd = quote_plus(config.password)
    port = config.port or 5432
    db_type = getattr(config, "type", "postgres") or "postgres"
    if db_type == "mysql":
        return (f"mysql+aiomysql://{user}:{pwd}"
                f"@{config.host}:{port}/{config.database}?charset=utf8mb4")
    return (f"postgresql+asyncpg://{user}:{pwd}"
            f"@{config.host}:{port}/{config.database}")


async def init_db(config: PostgresConfig):
    """初始化引擎 + 会话工厂。只建连接，不建表（表由 schema SQL 预建）。
    死连接双重防护（华为云 MySQL 会掐空闲连接且不发 RST，长 run 期间平台库全程空闲，
    结尾落库时往死连接里写会永等——实测 run 结尾卡死无任何日志）：
    - pool_recycle=240：空闲超 4 分钟的连接借出时直接重建（掐空闲一般 5 分钟+，赶在前面）
    - pool_pre_ping：借出前探活，死连接自动换新（防 recycle 窗口内被掐）"""
    global _engine, _AsyncSessionFactory
    db_type = getattr(config, "type", "postgres") or "postgres"
    if db_type == "mysql":
        connect_args = {"connect_timeout": 10}   # aiomysql 仅支持建连超时，无读超时
    else:
        connect_args = {"timeout": 10, "command_timeout": 15}   # asyncpg connect/查询 秒
    _engine = create_async_engine(_db_url(config), echo=False,
                                  pool_pre_ping=True, pool_recycle=240,
                                  connect_args=connect_args)
    _AsyncSessionFactory = async_sessionmaker(_engine, expire_on_commit=False)
    log.info("平台库已初始化 [%s]: %s:%s/%s", db_type, config.host,
             config.port or 5432, config.database)


def AsyncSessionFactory() -> AsyncSession:
    """用法: async with AsyncSessionFactory() as s: ..."""
    if _AsyncSessionFactory is None:
        raise RuntimeError("平台库未初始化，请先调用 init_db()")
    return _AsyncSessionFactory()
