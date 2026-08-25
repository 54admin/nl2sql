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

import hashlib
import hmac
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pathlib import Path

# 首页 HTML 启动时读一次缓存（避免每次请求读磁盘 + 路径不绑 cwd）
_INDEX_HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_bytes()

from src.config import load_config
from src.core.agent_loop import AgentLoop
from src.core.orchestrator import Orchestrator
from src.core.prompt_store import PromptStore
from src.core.session import SessionState
from src.llm.service import LLMService
from src.logging import get_logger, setup_logging
from src.memory.session import SessionManager
from src.storage.db_client import init_db
from src.storage.redis_client import RedisClient
from src.tools.catalog import build_registry
from src.web.routes.admin_llm import build_admin_llm_router
from src.web.routes.admin_prompts import build_admin_prompts_router
from src.web.routes.admin_datasource import build_datasource_router
from src.web.routes.admin_metadata import build_metadata_router
from src.web.routes.admin_business_rules import build_business_rules_router
from src.web.routes.admin_sql_templates import build_sql_templates_router
from src.web.routes.admin_feishu import build_admin_feishu_router
from src.web.routes.admin_ragflow import build_admin_ragflow_router
from src.web.routes.admin_agent_limits import build_agent_limits_router, load_agent_limits
from src.web.routes.ask import build_ask_router
from src.web.routes.result import build_result_router
from src.web.routes.session import build_session_router
from src.web.routes.admin_audit import build_audit_router

# 全局组件（lifespan 初始化，路由通过 _Lazy 延迟引用）
_app_state: dict = {}


_TOKEN_TTL = 7 * 24 * 3600   # token 有效期 7 天


def _sign_token(username: str, role: str, secret: str) -> str:
    """签名 token：username:role:expire:hmac(secret, ...)。登录返回，前端存 localStorage，
    请求带 Authorization: Bearer <token>。无状态——服务端不存，登出靠前端丢弃。
    role：admin=全权 / kb_op=知识库操作员（auth_guard 按矩阵拦截）。"""
    expire = int(time.time()) + _TOKEN_TTL
    payload = f"{username}:{role}:{expire}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_token(val: str | None, secret: str) -> tuple[str, str] | None:
    """校验 token 返回 (username, role)；格式错/签名不符/过期都返回 None。"""
    if not val or not secret:
        return None
    parts = val.split(":")
    if len(parts) != 4:
        return None
    username, role, expire, sig = parts
    expect = hmac.new(secret.encode(), f"{username}:{role}:{expire}".encode(),
                      hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    if not expire.isdigit() or int(expire) < time.time():
        return None
    return username, role


def _bearer(req: Request) -> str | None:
    """从 Authorization: Bearer <token> 提取 token。"""
    h = req.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else None


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

    # 平台库连接走 config/application-{profile}.yml 的 database 段（老 yml 的 postgres 段仍兼容）
    redis = RedisClient(cfg.redis)
    import asyncio
    # 平台库 + Redis 并行连接（各自都是连华为云的网络耗时，并行省掉串行等待）
    await asyncio.gather(
        init_db(cfg.database),
        redis.connect(),
    )

    sm = SessionManager(redis)
    llm = LLMService()                     # 配置全走数据库 llm_config 表
    prompts = PromptStore()
    from src.datasource.manager import get_manager
    datasource_mgr = get_manager()
    # 读 enabled SQL 模板清单拼进 get_sql_template 工具 description（LLM 看 schema 即知有哪些模板）
    from src.tools.sql_template import build_template_desc, list_enabled_templates
    reg = await build_registry(sql_template_desc=build_template_desc(await list_enabled_templates()),
                                  prompt_store=prompts)
    sess_state = SessionState(sm)
    from src.core.audit import AuditSink
    audit = AuditSink()
    agent_limits = await load_agent_limits()
    # 透传所有 max_* 上限给 AgentLoop（dict 里其余 key 如 id/version 不以 max_ 开头，自动排除）
    loop = AgentLoop(llm, reg, sess_state,
                     **{k: v for k, v in agent_limits.items() if k.startswith("max_")},
                     session_manager=sm, audit=audit)
    orch = Orchestrator(loop, sm, prompt_store=prompts, audit=audit)

    from src.eam.client import EamClient
    _app_state.update(
        orchestrator=orch, loop=loop, session_mgr=sm, llm_service=llm, prompts=prompts,
        datasource_mgr=datasource_mgr, sess_state=sess_state, auth=cfg.auth,
        eam_client=EamClient(cfg.eam))   # EAM 只读同步源（yml eam 段配置，改配置需重启）
    _db_type = getattr(cfg.database, "type", "postgres") or "postgres"
    log.info("nl2sql 启动完成 db=%s(%s:%s/%s) redis=%s（模型配置走数据库 llm_config）",
             _db_type, cfg.database.host, cfg.database.port, cfg.database.database,
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
    from src.storage import db_client
    if db_client._engine is not None:
        await db_client._engine.dispose()
    # 关 RAGFlow httpx 连接池（P1：进程级单例，启动复用、关闭释放）
    from src.ragflow.client import close_http_client
    await close_http_client()


def create_app() -> FastAPI:
    app = FastAPI(title="nl2sql AI问数", version="P0b", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"],
        allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

    @app.get("/")
    async def index():
        # 开发期每次请求读文件：改 index.html 强刷即生效，不用重启
        # （_INDEX_HTML 是启动读的常量，--reload 不监听 html，故每次读文件）
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_bytes()
        return Response(content=html, media_type="text/html")

    # ---- 登录（多账号+角色，hmac 签名 token，Authorization: Bearer）----
    @app.post("/api/login")
    async def login(req: Request):
        body = await req.json()
        auth = _app_state.get("auth")
        account = auth.find(body.get("username", ""), body.get("password", "")) if auth else None
        if account is None:
            raise HTTPException(401, "账号或密码错误")
        return {"ok": True, "token": _sign_token(account.username, account.role, auth.secret()),
                "role": account.role}

    @app.post("/api/logout")
    async def logout():
        return {"ok": True}   # token 无状态：登出靠前端丢弃 localStorage

    @app.get("/api/me")
    async def whoami(req: Request):
        # 受中间件保护（需 Bearer token）；到这里说明已登录
        return {"logged_in": True, "username": req.state.username, "role": req.state.role}

    @app.middleware("http")
    async def auth_guard(req: Request, call_next):
        # 白名单：首页 / 登录 / 预检；其余（含 logout）验 Authorization: Bearer <token>
        p, m = req.url.path, req.method
        if p in ("/", "/favicon.ico") or (p == "/api/login" and m == "POST") or m == "OPTIONS":
            return await call_next(req)
        auth = _app_state.get("auth")
        accounts = auth.account_list() if auth else []
        if not accounts:   # 未配任何账号 → 不启用认证（开发兜底）
            return await call_next(req)
        verified = _verify_token(_bearer(req), auth.secret())
        if verified is None:
            return JSONResponse({"detail": "未登录"}, status_code=401)
        req.state.username, req.state.role = verified
        # kb_op（知识库操作员）：只做知识库管理——对话/统计/其余 admin 全拦
        # （统计本就在 admin 前缀下被拦；对话链路 ask/session/result 也一并 403）
        if req.state.role == "kb_op":
            if p.startswith(("/api/ask", "/api/session", "/api/result")):
                return JSONResponse(
                    {"detail": "无权限：知识库操作员不开放对话问数"}, status_code=403)
            if p.startswith("/api/admin"):
                allowed = p.startswith(("/api/admin/ragflow/datasets",
                                        "/api/admin/ragflow/documents",
                                        "/api/admin/ragflow/parse",
                                        "/api/admin/eam"))
                if not allowed:
                    return JSONResponse(
                        {"detail": "无权限：知识库操作员仅能访问知识库管理相关功能"}, status_code=403)
        return await call_next(req)

    app.include_router(build_ask_router(_Lazy("orchestrator")))
    app.include_router(build_admin_feishu_router(_Lazy("feishu_adapter")))
    app.include_router(build_admin_ragflow_router())  # P3：RAGFlow 知识库配置+文档管理
    from src.web.routes.admin_eam import build_admin_eam_router
    app.include_router(build_admin_eam_router(_Lazy("eam_client")))  # EAM 只读同步（树/清单/同步）
    app.include_router(build_agent_limits_router(_Lazy("loop")))   # 查询上限可配（PUT 后热刷新 AgentLoop）
    app.include_router(build_session_router(_Lazy("session_mgr")))
    app.include_router(build_admin_llm_router(_Lazy("llm_service")))
    app.include_router(build_admin_prompts_router(_Lazy("prompts"), _Lazy("loop")))
    # P1a 新增 4 个 admin 路由：datasource 要 manager（_Lazy 延迟解析），其余 3 个纯 PG 无参
    app.include_router(build_datasource_router(_Lazy("datasource_mgr")))
    app.include_router(build_metadata_router())
    app.include_router(build_business_rules_router())
    app.include_router(build_sql_templates_router())
    app.include_router(build_result_router())  # P1b：前端按 result_id 取全量结果
    app.include_router(build_audit_router())   # 审计统计：trace 详情/会话级/全局统计
    return app


app = create_app()
