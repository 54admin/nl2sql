"""nl2sql AI问数 FastAPI 应用入口（P0b）。

启动：./run.sh  或  python3 -m uvicorn src.main:app --port 8000
配置：config/application.yml（postgres 数据库连接 / redis / llm 兜底）
接口文档：http://127.0.0.1:8000/docs
模型动态配置（apikey/model/base_url）：PUT /api/admin/llm-config 存数据库，优先于 yml。
"""
from contextlib import asynccontextmanager
from urllib.parse import quote_plus

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import load_config
from src.core.agent_loop import AgentLoop
from src.core.normalizer import Normalizer
from src.core.orchestrator import Orchestrator
from src.core.prompt_store import PromptStore
from src.core.session import SessionState
from src.llm.service import LLMService
from src.logging import get_logger, setup_logging
from src.memory.session import SessionManager
from src.storage.pg_client import init_db
from src.storage.redis_client import RedisClient
from src.tools.builtins import default_registry
from src.web.routes.admin_llm import build_admin_llm_router
from src.web.routes.admin_prompts import build_admin_prompts_router
from src.web.routes.ask import build_ask_router
from src.web.routes.session import build_session_router

# 全局组件（lifespan 初始化，路由通过 _Lazy 延迟引用）
_app_state: dict = {}


def _pg_url(pc) -> str:
    """拼 PostgreSQL asyncpg URL，用户名密码做 URL 编码。"""
    return (f"postgresql+asyncpg://{quote_plus(pc.username)}:{quote_plus(pc.password)}"
            f"@{pc.host}:{pc.port}/{pc.database}")


class _Lazy:
    """延迟解析到 lifespan 初始化的全局组件。
    路由在 create_app(同步) 时注册，组件在 lifespan(async) 才初始化，
    用 _Lazy 包装让请求到达时才解析到真实组件。"""
    def __init__(self, key: str):
        self._key = key

    def __getattr__(self, name):
        return getattr(_app_state[self._key], name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config("config")
    setup_logging("INFO")
    log = get_logger("main")

    # 数据库连接走 config/application.yml 的 postgres 段
    await init_db(_pg_url(cfg.postgres))

    redis = RedisClient(cfg.redis)
    await redis.connect()

    sm = SessionManager(redis)
    llm = LLMService()                     # 配置全走数据库 llm_config 表
    prompts = PromptStore()
    reg = default_registry()
    sess_state = SessionState(sm)
    loop = AgentLoop(llm, reg, sess_state)
    norm = Normalizer()
    orch = Orchestrator(norm, loop, sm, prompt_store=prompts)

    _app_state.update(
        orchestrator=orch, session_mgr=sm, llm_service=llm, prompts=prompts)
    log.info("nl2sql 启动完成 db=postgres(%s:%s/%s) redis=%s（模型配置走数据库 llm_config）",
             cfg.postgres.host, cfg.postgres.port, cfg.postgres.database,
             "可用" if redis.available else "降级内存")
    yield
    from src.storage import pg_client
    if pg_client._engine is not None:
        await pg_client._engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="nl2sql AI问数", version="P0b", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"],
        allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/health")
    async def health():
        return {"ok": True, "ready": "orchestrator" in _app_state}

    app.include_router(build_ask_router(_Lazy("orchestrator")))
    app.include_router(build_session_router(_Lazy("session_mgr")))
    app.include_router(build_admin_llm_router(_Lazy("llm_service")))
    app.include_router(build_admin_prompts_router(_Lazy("prompts")))
    return app


app = create_app()
