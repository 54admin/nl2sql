"""Redis 客户端，连接失败降级到内存 dict。调用方无感。"""
import time

from src.config import RedisConfig
from src.logging import get_logger

log = get_logger(__name__)


class _InMemory:
    """进程内降级后端，近似 TTL。"""

    def __init__(self):
        self._store: dict[str, tuple[str, float]] = {}  # key -> (value, expire_at|0)

    async def get(self, key):
        item = self._store.get(key)
        if not item:
            return None
        value, expire_at = item
        if expire_at and time.monotonic() > expire_at:
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key, value, ttl=None):
        if ttl == 0:
            # ttl=0 立即过期：等价删除，get 必返回 None
            self._store.pop(key, None)
            return
        expire_at = time.monotonic() + ttl if ttl and ttl > 0 else 0
        self._store[key] = (value, expire_at)

    async def delete(self, key):
        self._store.pop(key, None)


class RedisClient:
    def __init__(self, config: RedisConfig):
        self._config = config
        self._backend = None
        self.available = False

    async def connect(self):
        try:
            import redis.asyncio as aioredis
            self._backend = aioredis.Redis(
                host=self._config.host, port=self._config.port,
                db=self._config.db, password=self._config.password or None,
                socket_connect_timeout=1)
            await self._backend.ping()
            self.available = True
            log.info("Redis 已连接")
        except Exception as e:
            log.warning("Redis 连接失败，降级到内存后端: %s", e)
            self._backend = _InMemory()
            self.available = False

    async def get(self, key: str):
        return await self._backend.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None):
        if self.available and ttl:
            await self._backend.set(key, value, ex=ttl)
        elif self.available:
            await self._backend.set(key, value)
        else:
            await self._backend.set(key, value, ttl=ttl)

    async def delete(self, key: str):
        await self._backend.delete(key)
