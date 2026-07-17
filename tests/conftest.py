import pytest

from src.logging import setup_logging
from src.storage import pg_client


@pytest.fixture(autouse=True)
def _logging():
    """统一日志初始化，所有测试可见 nl2sql 命名空间日志。"""
    setup_logging("DEBUG")


@pytest.fixture(autouse=True)
async def _dispose_pg_engine():
    """每个测试结束后释放全局 PG 引擎。
    ponytail: aiosqlite :memory: + StaticPool 持有长连接，其 worker 线程非
    daemon，不 dispose 会阻塞 threading._shutdown 让进程无法退出。测试用
    sqlite 才有此问题，生产走 asyncpg 无此困扰，故放测试侧兜底。"""
    yield
    eng = pg_client._engine
    if eng is not None:
        await eng.dispose()
        pg_client._engine = None
