"""业务规则缓存（P2）。读 enabled BusinessRule 拼成文本段，
orchestrator 追加到 system_prompt 让 LLM 知晓人工口径。
TTL 刷新（照 NameStore 模式，规则低频变更）。
ponytail: 跨进程广播失效 P5 改 Redis pub/sub。"""
from __future__ import annotations

from src.logging import get_logger
from src.storage.models import BusinessRule
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)

_TTL = 30.0


class RuleStore:
    """enabled 业务规则缓存。all_text 拼成「- key: value」每行一条。"""

    def __init__(self) -> None:
        self._cache: list[tuple[str, str]] = []   # [(key, value_json), ...]
        self._loaded_at: float = 0.0

    async def _ensure_loaded(self) -> None:
        import time
        if self._cache and time.monotonic() - self._loaded_at < _TTL:
            return
        async with AsyncSessionFactory() as s:
            stmt = BusinessRule.__table__.select().where(BusinessRule.enabled.is_(True))
            rows = (await s.execute(stmt)).all()
        self._cache = [(r.key, r.value_json) for r in rows]
        self._loaded_at = time.monotonic()

    async def all_text(self) -> str:
        """拼成「- key: value」每行一条；无规则返回空串。"""
        await self._ensure_loaded()
        if not self._cache:
            return ""
        return "\n".join(f"- {k}: {v}" for k, v in self._cache)

    async def refresh(self) -> None:
        """强制下次重读 PG（admin 改完可调，立即生效）。"""
        self._loaded_at = 0.0
