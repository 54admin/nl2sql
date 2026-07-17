"""动态配置基础：通用 KV + 内存缓存 + 版本号（页面配置模型基础设施）。
- get 内存缓存优先，miss 读 PG 回填缓存
- set 写 PG + bump version + 刷新内存
ponytail: P0b 单进程内存缓存即足够；跨进程广播失效 P5 改 Redis pub/sub（预留 refresh 接口）。"""
from __future__ import annotations

import json
from typing import Any

from src.logging import get_logger
from src.storage.models import AppConfigRow
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)


class ConfigStore:
    """通用动态配置 KV。被 llm_config / prompts 等子系统复用模式，
    也可直接存任意 JSON 配置（feature flag、阈值等）。"""

    def __init__(self) -> None:
        # key -> (value, version)
        self._cache: dict[str, tuple[Any, int]] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        """读配置：内存缓存优先，miss 读 PG 回填缓存。未配置返回 default。"""
        cached = self._cache.get(key)
        if cached is not None:
            return cached[0]
        async with AsyncSessionFactory() as s:
            row = await s.get(AppConfigRow, key)
            if row is None:
                return default
            value = json.loads(row.value_json)
            version = row.version
        self._cache[key] = (value, version)
        return value

    async def set(self, key: str, value: Any) -> int:
        """写配置：upsert PG + bump version + 刷新内存。返回新版本号。"""
        async with AsyncSessionFactory() as s:
            row = await s.get(AppConfigRow, key)
            value_json = json.dumps(value, ensure_ascii=False)
            if row:
                row.value_json = value_json
                row.version += 1
                new_version = row.version
            else:
                s.add(AppConfigRow(key=key, value_json=value_json, version=1))
                new_version = 1
            await s.commit()
        self._cache[key] = (value, new_version)
        log.info("配置更新 key=%s version=%s", key, new_version)
        return new_version

    async def refresh(self) -> None:
        """清缓存重新加载已缓存的 key（admin 改完手动刷）。
        ponytail: P5 跨进程时改成订阅 Redis pub/sub 频道自动失效。"""
        keys = list(self._cache.keys())
        self._cache.clear()
        for k in keys:
            await self.get(k)
