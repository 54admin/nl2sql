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
from fastapi.responses import Response
from pathlib import Path

# 首页 HTML 启动时读一次缓存（避免每次请求读磁盘 + 路径不绑 cwd）
_INDEX_HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_bytes()

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
from src.web.routes.admin_datasource import build_datasource_router
from src.web.routes.admin_metadata import build_metadata_router
from src.web.routes.admin_business_rules import build_business_rules_router
from src.web.routes.admin_sql_templates import build_sql_templates_router
from src.web.routes.ask import build_ask_router
from src.web.routes.result import build_result_router
from src.web.routes.session import build_session_router
from src.web.routes.admin_audit import build_audit_router

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
    from src.datasource.manager import DataSourceManager
    datasource_mgr = DataSourceManager()
    reg = default_registry()
    sess_state = SessionState(sm)
    from src.core.audit import AuditSink
    audit = AuditSink()
    loop = AgentLoop(llm, reg, sess_state, session_manager=sm, audit=audit)
    norm = Normalizer()
    orch = Orchestrator(norm, loop, sm, prompt_store=prompts)

    _app_state.update(
        orchestrator=orch, session_mgr=sm, llm_service=llm, prompts=prompts,
        datasource_mgr=datasource_mgr, sess_state=sess_state)
    log.info("nl2sql 启动完成 db=postgres(%s:%s/%s) redis=%s（模型配置走数据库 llm_config）",
             cfg.postgres.host, cfg.postgres.port, cfg.postgres.database,
             "可用" if redis.available else "降级内存")

    # 挂起超时清扫：启动补清（上次崩溃/被 kill 遗留的孤儿 checkpoint）+ 周期后台扫。
    # 服务挂了重启也能清滞留垃圾，不依赖常驻进程一直没死。
    import asyncio
    SWEEP_INTERVAL = 300      # 5 分钟扫一次
    SWEEP_MAX_AGE = 30        # 挂起超 30 分钟没回答判过期
    try:
        cleared = await sess_state.sweep_stale_suspended(SWEEP_MAX_AGE)
        if cleared:
            log.info("启动补清挂起超时会话：%d 个", cleared)
    except Exception as e:
        log.warning("启动清扫挂起会话失败（忽略，不阻塞启动）: %s", e)

    async def _sweep_loop():
        while True:
            await asyncio.sleep(SWEEP_INTERVAL)
            try:
                await sess_state.sweep_stale_suspended(SWEEP_MAX_AGE)
            except Exception as e:
                log.warning("周期清扫挂起会话失败: %s", e)

    sweep_task = asyncio.create_task(_sweep_loop())

    yield

    # 服务关闭：取消后台清扫任务 + 关连接池
    sweep_task.cancel()
    try:
        await sweep_task
    except (asyncio.CancelledError, Exception):
        pass
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

    @app.get("/")
    async def index():
        # 开发期每次请求读文件：改 index.html 强刷即生效，不用重启
        # （_INDEX_HTML 是启动读的常量，--reload 不监听 html，故每次读文件）
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_bytes()
        return Response(content=html, media_type="text/html")

    app.include_router(build_ask_router(_Lazy("orchestrator")))
    app.include_router(build_session_router(_Lazy("session_mgr")))
    app.include_router(build_admin_llm_router(_Lazy("llm_service")))
    app.include_router(build_admin_prompts_router(_Lazy("prompts")))
    # P1a 新增 4 个 admin 路由：datasource 要 manager（_Lazy 延迟解析），其余 3 个纯 PG 无参
    app.include_router(build_datasource_router(_Lazy("datasource_mgr")))
    app.include_router(build_metadata_router())
    app.include_router(build_business_rules_router())
    app.include_router(build_sql_templates_router())
    app.include_router(build_result_router())  # P1b：前端按 result_id 取全量结果
    app.include_router(build_audit_router())   # 审计统计：trace 详情/会话级/全局统计
    return app


app = create_app()
