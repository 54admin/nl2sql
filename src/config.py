"""配置加载：YAML 基线 + profile 合并（读 app/redis/database/feishu/auth/eam）。
llm 不在这里读——全走数据库 llm_config 表（PUT /api/admin/llm-config）。
LLMConfig dataclass 保留供 LLMService 内部装数据库读出的配置用。"""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.logging import get_logger

log = get_logger(__name__)


@dataclass
class LLMConfig:
    """LLM 配置 dataclass（供 LLMService 装数据库读出的配置；不从 yml 读）。"""
    api_key: str = ""
    api_base: str = ""
    model: str = ""
    temperature: float = 0.0
    timeout: int = 60
    max_context: int = 32000   # 模型上下文窗口（token），会话压缩按占比触发用
    # 协议：openai（/v1/chat/completions + Bearer）或 anthropic（/v1/messages + x-api-key）。
    # 同一网关常按协议分额度桶——anthropic 路径往往有额度，openai 路径易配额超限。
    protocol: str = "openai"
    # 限流（P2 主动节流，防撞网关限流）。None=该维度不限（只重试不限速）。
    rpm_limit: int | None = None
    concurrency: int | None = None


@dataclass
class RedisConfig:
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    username: str = ""    # ACL 用户名（Redis 6+ 实例）；空=无认证/仅密码（兼容生产无账号实例）
    password: str = ""


@dataclass
class PostgresConfig:
    """平台库配置（PG/MySQL 通用）。type=mysql 走 aiomysql，否则走 asyncpg。"""
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "nl2sql"
    username: str = "postgres"
    password: str = ""
    type: str = "postgres"


@dataclass
class AppConfig:
    name: str = "NL2SQL"


@dataclass
class FeishuConfig:
    """飞书机器人通道配置（旁路适配器，WebSocket 长连接，免公网）。
    enable=true 且 app_id/app_secret 非空时，main.py lifespan 才启动适配器。
    whitelist 是 open_id 列表，空=不限；密码明文存沿用项目内网工具惯例。"""
    enable: bool = False
    app_id: str = ""
    app_secret: str = ""
    whitelist: list = field(default_factory=list)
    card_throttle_ms: int = 120   # answer acontent 节流间隔（流式不计卡片 QPS，越小越顺；120ms 平衡）


@dataclass
class Account:
    """登录账号。role：admin=全权；kb_op=知识库操作员（仅知识库管理+问数，
    RAGFlow 连接配置等其余 admin 端点 403，见 main.py auth_guard）。"""
    username: str
    password: str
    role: str = "admin"


@dataclass
class AuthConfig:
    """登录配置（hmac 签名 token，无状态）。
    兼容两种写法：旧式 username/password 单账户（视为 admin）；
    新式 accounts 列表（多账号+角色）。密码在 application-{profile}.yml 配（gitignore 不入库）。"""
    username: str = "admin"
    password: str = ""
    accounts: list = field(default_factory=list)   # [{username, password, role}]，role 缺省 admin

    def account_list(self) -> list[Account]:
        """生效账号列表：accounts 优先；为空且配了 password 则视为单 admin 账号。"""
        if self.accounts:
            return [Account(a.get("username", ""), a.get("password", ""), a.get("role", "admin"))
                    for a in self.accounts if a.get("username") and a.get("password")]
        if self.password:
            return [Account(self.username, self.password, "admin")]
        return []

    def find(self, username: str, password: str) -> Account | None:
        for a in self.account_list():
            if a.username == username and a.password == password:
                return a
        return None

    def secret(self) -> str:
        """token 签名全局密钥 = 全部账号密码拼接。改任一密码 → 全员 token 失效重新登录。"""
        return "|".join(a.password for a in self.account_list())


@dataclass
class EamConfig:
    """EAM 文档管理对接（只读同步源，全部接口 2026-08-25 实测，见 docs/知识库管理设计.md 第7章）。
    鉴权：Authorization: Basic <auth_basic> 且 loginToken 头必须存在（空值即可）。"""
    base_url: str = "https://api.gw-greenenergy.com"
    auth_basic: str = ""            # base64(ak:sk)，平台签发长期凭证；sk 不进前端
    tree_api: str = "/annex/folder/tree"
    files_api: str = "/annex/file/fileList"
    download_api: str = "/annex/file/batch/downLoad"

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.auth_basic)


@dataclass
class ApplicationConfig:
    app: AppConfig = field(default_factory=AppConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    database: PostgresConfig = field(default_factory=PostgresConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    eam: EamConfig = field(default_factory=EamConfig)
    profiles: list = field(default_factory=list)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并，override 覆盖 base。"""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _build(d: dict) -> ApplicationConfig:
    # active 既可能是字符串也可能是列表，统一成 list
    active = d.get("profiles", {}).get("active") if isinstance(d.get("profiles"), dict) else None
    if isinstance(active, list):
        profiles = list(active)
    elif active:
        profiles = [active]
    else:
        profiles = []
    return ApplicationConfig(
        app=AppConfig(**d.get("app", {})),
        redis=RedisConfig(**d.get("redis", {})),
        # 平台库段名 database:（PG/MySQL 通用）。老 yml 的 postgres: 段仍兼容
        database=PostgresConfig(**(d.get("database") or d.get("postgres") or {})),
        feishu=FeishuConfig(**d.get("feishu", {})),
        auth=AuthConfig(**d.get("auth", {})),
        eam=EamConfig(**d.get("eam", {})),
        profiles=profiles,
    )


def load_config(config_dir: str = "config", profile: str | None = None) -> ApplicationConfig:
    """读 application.yml，按 profile 合并 application-{p}.yml。只读 app/redis/postgres（llm 不读）。"""
    base_path = Path(config_dir) / "application.yml"
    data = yaml.safe_load(base_path.read_text()) if base_path.exists() else {}

    active = profile or data.get("profiles", {}).get("active")
    if active:
        prof_path = Path(config_dir) / f"application-{active}.yml"
        if prof_path.exists():
            data = _deep_merge(data, yaml.safe_load(prof_path.read_text()))
        else:
            log.warning("profile 文件不存在，回退基线配置: %s", prof_path)

    return _build(data)
