"""配置加载：YAML 基线 + profile 合并 + 环境变量覆盖。"""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.logging import get_logger

log = get_logger(__name__)


@dataclass
class LLMConfig:
    api_key: str = ""
    api_base: str = ""
    model: str = ""
    temperature: float = 0.0
    timeout: int = 60


@dataclass
class RedisConfig:
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    password: str = ""


@dataclass
class PostgresConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "nl2sql"
    username: str = "postgres"
    password: str = ""


@dataclass
class AppConfig:
    name: str = "NL2SQL"


@dataclass
class ApplicationConfig:
    app: AppConfig = field(default_factory=AppConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
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
    # active 既可能是字符串也可能是列表，统一成 list，方便后续消费
    active = d.get("profiles", {}).get("active") if isinstance(d.get("profiles"), dict) else None
    if isinstance(active, list):
        profiles = list(active)
    elif active:
        profiles = [active]
    else:
        profiles = []
    return ApplicationConfig(
        app=AppConfig(**d.get("app", {})),
        llm=LLMConfig(**d.get("llm", {})),
        redis=RedisConfig(**d.get("redis", {})),
        postgres=PostgresConfig(**d.get("postgres", {})),
        profiles=profiles,
    )


def load_config(config_dir: str = "config", profile: str | None = None) -> ApplicationConfig:
    """读 application.yml，按 profile 合并 application-{p}.yml，环境变量最后覆盖。"""
    base_path = Path(config_dir) / "application.yml"
    data = yaml.safe_load(base_path.read_text()) if base_path.exists() else {}

    active = profile or data.get("profiles", {}).get("active")
    if active:
        prof_path = Path(config_dir) / f"application-{active}.yml"
        if prof_path.exists():
            data = _deep_merge(data, yaml.safe_load(prof_path.read_text()))
        else:
            log.warning("profile 文件不存在，回退基线配置: %s", prof_path)

    # 环境变量覆盖（OPENAI_API_KEY 覆盖 llm.api_key）
    if os.getenv("OPENAI_API_KEY"):
        data.setdefault("llm", {})["api_key"] = os.environ["OPENAI_API_KEY"]

    return _build(data)
