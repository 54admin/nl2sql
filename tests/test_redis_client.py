import pytest

from src.storage.redis_client import RedisClient
from src.config import RedisConfig


@pytest.fixture
def client():
    # 指向不存在的 host，强制走降级路径
    c = RedisClient(RedisConfig(host="unreachable.invalid", port=1))
    return c


@pytest.mark.asyncio
async def test_fallback_set_get(client):
    await client.connect()                 # 连不上，降级
    assert client.available is False
    await client.set("k", "v", ttl=60)
    assert await client.get("k") == "v"


@pytest.mark.asyncio
async def test_fallback_delete(client):
    await client.connect()
    await client.set("k", "v")
    await client.delete("k")
    assert await client.get("k") is None


@pytest.mark.asyncio
async def test_fallback_ttl_expires(client):
    await client.connect()
    await client.set("k", "v", ttl=0)      # ttl=0 立即过期
    assert await client.get("k") is None
