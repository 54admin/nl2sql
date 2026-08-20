"""配置加载：YAML 基线 + profile 合并（只读 app/redis/postgres）。
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
class AuthConfig:
    """全站单账户登录配置（hmac 签名 cookie 认证）。
    默认 username=admin；password 必须在 application-{profile}.yml 配（文件已 gitignore，密码不入库）。"""
    username: str = "admin"
    password: str = ""


@dataclass
class ApplicationConfig:
    app: AppConfig = field(default_factory=AppConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    database: PostgresConfig = field(default_factory=PostgresConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
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
