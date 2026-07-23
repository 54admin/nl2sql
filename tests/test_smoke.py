"""冒烟集成测试：串联 config / pg / redis / session / llm 全链路。"""
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

    # PG（sqlite 内存）
    await init_db("sqlite+aiosqlite:///:memory:")
    # Redis（连不上自动降级到内存后端；create_session 内部已覆盖 set/get）
    redis = RedisClient(cfg.redis)
    await redis.connect()

    # 会话：create -> append -> get
    mgr = SessionManager(redis)
    sid = await mgr.create_session(user_id="u1", channel="web")
    await mgr.append_message(sid, "user", "你好", trace_id="t1")
    msgs = await mgr.get_messages(sid)
    assert len(msgs) == 1

    # LLM 服务能构造（不打网关）
    svc = LLMService()
    assert svc._clients == {}  # 配置全数据库，构造无参即可

    log.info("冒烟通过: session=%s msgs=%d", sid, len(msgs))
