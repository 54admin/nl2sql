"""场景化 prompt 存储 + 内存缓存（页面配置模型基础）。
orchestrator 组装 system message 时按 scene 读，默认场景 'default'。
ponytail: 单进程内存缓存；跨进程广播 P5 改 Redis pub/sub。"""
from __future__ import annotations

from src.logging import get_logger
from src.storage.models import Prompt
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)

DEFAULT_SCENE = "default"


class PromptStore:
    """场景化 prompt：内存缓存 + PG 持久。
    get 内存缓存优先，miss 读 PG 回填；upsert 写 PG + bump version + 刷新缓存。"""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, int]] = {}

    async def get(self, scene: str = DEFAULT_SCENE) -> str | None:
        """读场景 prompt。未配置或 enabled=False 返回 None。"""
        cached = self._cache.get(scene)
        if cached is not None:
            return cached[0]
        async with AsyncSessionFactory() as s:
            row = await s.get(Prompt, scene)
            if row is None or not row.enabled:
                return None
            content, version = row.content, row.version
        self._cache[scene] = (content, version)
        return content

    async def upsert(self, scene: str, content: str,
                     enabled: bool = True) -> int:
        async with AsyncSessionFactory() as s:
            row = await s.get(Prompt, scene)
            if row:
                row.content = content
                row.enabled = enabled
                row.version += 1
                new_version = row.version
            else:
                s.add(Prompt(scene=scene, content=content,
                             enabled=enabled, version=1))
                new_version = 1
            await s.commit()
        # ponytail: disabled 时清缓存而非写入，让 get miss 走 PG 看到 enabled=False 返回 None；
        # 否则缓存命中会绕过 enabled 检查。
        if enabled:
            self._cache[scene] = (content, new_version)
        else:
            self._cache.pop(scene, None)
        log.info("prompt 更新 scene=%s version=%s enabled=%s",
                 scene, new_version, enabled)
        return new_version

    async def delete(self, scene: str) -> bool:
        async with AsyncSessionFactory() as s:
            row = await s.get(Prompt, scene)
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
        self._cache.pop(scene, None)
        return True

    async def list_all(self) -> list[dict]:
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(Prompt.__table__.select())).all()
        return [{"scene": r.scene, "content": r.content,
                 "version": r.version, "enabled": r.enabled} for r in rows]

    async def refresh(self) -> None:
        scenes = list(self._cache.keys())
        self._cache.clear()
        for sc in scenes:
            await self.get(sc)
