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
            # username/password 都可空：生产实例无认证 → 两个都 None 直连；
            # 仅密码 → AUTH default 用户；ACL 实例 → username+password（redis-py 自动 HELLO/AUTH）。
            self._backend = aioredis.Redis(
                host=self._config.host, port=self._config.port,
                db=self._config.db, username=self._config.username or None,
                password=self._config.password or None,
                socket_connect_timeout=1,
                socket_timeout=3,          # 读写超时：长 run 期间连接被中间设备静默掐断
                                            # （华为云代理层掐空闲，不发 RST），无超时会永等 recvfrom
                socket_keepalive=True,     # TCP keepalive 探测死连接，配合 socket_timeout 快速失败
                retry_on_timeout=True,     # 超时自动重连重试一次（redis-py 内建）
                decode_responses=True)  # 真 redis 返回 str，对齐 _InMemory
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
        # 入口统一 ttl 语义：<=0 立即过期(等价 delete)；None 永久；>0 按秒
        if ttl is not None and ttl <= 0:
            await self.delete(key)
            return
        if self.available:
            await self._backend.set(key, value, ex=ttl if ttl else None)
        else:
            await self._backend.set(key, value, ttl=ttl)

    async def delete(self, key: str):
        await self._backend.delete(key)
