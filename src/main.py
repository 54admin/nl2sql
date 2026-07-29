"""nl2sql AI问数 FastAPI 应用入口（P0b）。

启动：./run.sh  或  python3 -m uvicorn src.main:app --port 8000
配置：config/application.yml（postgres 数据库连接 / redis / llm 兜底）
接口文档：http://127.0.0.1:8000/docs
模型动态配置（apikey/model/base_url）：PUT /api/admin/llm-config 存数据库，优先于 yml。
"""
# 公司 SSL 拦截网关（内网 IP 如 10.111.86.78）用「不符合标准」的自签证书重签所有 HTTPS 流量，
# Python 默认验证、truststore（系统钥匙串）都验证不过 → 飞书 WSS 连不上。实测唯一能连是关 SSL 验证。
# 内网问数工具可接受（拦截的就是公司网关，数据本经其转发）；公网/正规 CA 环境部署时删掉这段即恢复验证。
import ssl as _ssl
_orig_ctx = _ssl.create_default_context
def _insecure_ctx(*a, **k):
    c = _orig_ctx(*a, **k); c.check_hostname = False; c.verify_mode = _ssl.CERT_NONE; return c
_ssl.create_default_context = _insecure_ctx

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
from src.web.routes.admin_knowledge import build_knowledge_router
from src.web.routes.admin_metadata import build_metadata_router
from src.web.routes.admin_business_rules import build_business_rules_router
from src.web.routes.admin_name_dict import build_name_dict_router
from src.web.routes.admin_sql_templates import build_sql_templates_router
from src.web.routes.admin_feishu import build_admin_feishu_router
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
    redis = RedisClient(cfg.redis)
    import asyncio
    # PG + Redis 并行连接（各自都是连华为云的网络耗时，并行省掉串行等待）
    await asyncio.gather(
        init_db(_pg_url(cfg.postgres), auto_migrate=cfg.auto_migrate),
        redis.connect(),
    )

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
    from src.core.name_store import NameStore, build_llm_corrector
    from src.core.rule_store import RuleStore
    name_store = NameStore()
    norm = Normalizer(
        dict_fn=name_store.lookup_exact,
        fuzzy_fn=name_store.lookup_fuzzy,
        llm_fn=build_llm_corrector(name_store, llm))
    orch = Orchestrator(norm, loop, sm, prompt_store=prompts, audit=audit,
                        rule_store=RuleStore())

    _app_state.update(
        orchestrator=orch, session_mgr=sm, llm_service=llm, prompts=prompts,
        datasource_mgr=datasource_mgr, sess_state=sess_state)
    log.info("nl2sql 启动完成 db=postgres(%s:%s/%s) redis=%s（模型配置走数据库 llm_config）",
             cfg.postgres.host, cfg.postgres.port, cfg.postgres.database,
             "可用" if redis.available else "降级内存")

    # 飞书机器人通道（旁路接入，不碰 HTTP/SSE）：配置完全走数据库 feishu_config 表（后台「飞书」tab），
    # yml 不再管开关——adapter 总是实例化待命，启停由后台「启用」热控制（reload 重连/断开）。
    import asyncio
    main_loop = asyncio.get_running_loop()
    feishu_adapter = None
    try:
        from src.feishu import FeishuAdapter
        feishu_adapter = FeishuAdapter(
            cfg.feishu, main_loop=main_loop,
            orchestrator=orch, session_mgr=sm, redis=redis)
        await feishu_adapter.start()     # 从库读：enabled 且凭证齐才真连，否则空转待命
    except Exception as e:
        log.warning("飞书机器人通道初始化失败（忽略，不影响主服务）: %s", e)
    _app_state["feishu_adapter"] = feishu_adapter   # admin 路由 _Lazy 取用，做热重连

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

    # 服务关闭：停飞书通道 + 取消后台清扫任务 + 关连接池
    if feishu_adapter:
        feishu_adapter.stop()
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
    app.include_router(build_admin_feishu_router(_Lazy("feishu_adapter")))
    app.include_router(build_session_router(_Lazy("session_mgr")))
    app.include_router(build_admin_llm_router(_Lazy("llm_service")))
    app.include_router(build_admin_prompts_router(_Lazy("prompts")))
    # P1a 新增 4 个 admin 路由：datasource 要 manager（_Lazy 延迟解析），其余 3 个纯 PG 无参
    app.include_router(build_datasource_router(_Lazy("datasource_mgr")))
    app.include_router(build_knowledge_router())   # P3b 知识库上传/检索管理
    app.include_router(build_metadata_router())
    app.include_router(build_business_rules_router())
    app.include_router(build_name_dict_router())   # P2 名称纠错别名字典 CRUD
    app.include_router(build_sql_templates_router())
    app.include_router(build_result_router())  # P1b：前端按 result_id 取全量结果
    app.include_router(build_audit_router())   # 审计统计：trace 详情/会话级/全局统计
    return app


app = create_app()
