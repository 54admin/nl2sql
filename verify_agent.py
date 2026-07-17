"""最小 Agent 验证：组装 P0b 全链路跑 1 个 case，实时打印 loop 事件，看自主 ReAct。"""
import asyncio

from src.config import load_config
from src.core.agent_loop import AgentLoop
from src.core.session import SessionState
from src.core.types import CancelToken
from src.llm.service import LLMService
from src.logging import setup_logging
from src.memory.session import SessionManager
from src.storage.pg_client import init_db
from src.storage.redis_client import RedisClient
from src.tools.builtins import default_registry


async def main():
    setup_logging("WARNING")  # 只看 Agent 事件，不看 debug 日志
    cfg = load_config("config")
    from src.storage import pg_client
    await init_db("sqlite+aiosqlite:///:memory:")
    try:
        redis = RedisClient(cfg.redis)
        await redis.connect()
        mgr = SessionManager(redis)
        sid = await mgr.create_session("u1", "web")

        llm = LLMService(cfg.llm)
        reg = default_registry()
        state = SessionState(mgr)
        loop = AgentLoop(llm, reg, state, max_turns=6)

        question = "你好，请用一句话介绍你自己，再说一下 2+3 等于几"
        print(f"用户：{question}", flush=True)
        print("----- Agent 事件流 -----", flush=True)
        async for ev in loop.run(sid, "u1", question, "t1", CancelToken()):
            print(f"[{ev.type}] {ev.data}", flush=True)
        print("----- 结束 -----", flush=True)
    finally:
        # aiosqlite 内存库不 dispose 会 hang 住进程退出（非 daemon worker 线程）
        if pg_client._engine is not None:
            await pg_client._engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
