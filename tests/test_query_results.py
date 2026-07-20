"""result 旁路存取测试。sqlite PG + Redis 全 mock，不依赖真 Redis。"""
import json

import pytest

from src.storage import query_results
from src.storage.pg_client import init_db


@pytest.fixture
async def db():
    await init_db("sqlite+aiosqlite:///:memory:")


class _FakeRedis:
    """最小内存 Redis stub，仅实现 get/set 协议。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None):
        self.store[key] = value


async def _no_redis():
    """monkeypatch 用：让 _get_redis 返回 None，强制走 PG。"""
    return None


@pytest.mark.asyncio
async def test_save_then_get_roundtrip(db, monkeypatch):
    """PG round-trip：save 后 get 回来字段一致。Redis 关掉走 PG。"""
    monkeypatch.setattr(query_results, "_get_redis", _no_redis)
    rid = await query_results.save_result(
        "sess1", ["kwh", "month"], [{"kwh": 100, "month": "06"}])
    assert rid
    r = await query_results.get_result(rid)
    assert r is not None
    assert r["columns"] == ["kwh", "month"]
    assert r["rows"] == [{"kwh": 100, "month": "06"}]
    assert r["total"] == 1


@pytest.mark.asyncio
async def test_get_missing_returns_none(db, monkeypatch):
    """不存在的 result_id 返回 None。"""
    monkeypatch.setattr(query_results, "_get_redis", _no_redis)
    assert await query_results.get_result("nonexistent") is None


@pytest.mark.asyncio
async def test_redis_hit_skips_pg(db, monkeypatch):
    """Redis 命中直接返回，不走 PG（save 写入 Redis 的 payload 直接被 get 读回）。"""
    fake = _FakeRedis()

    async def _fake_get_redis():
        return fake

    monkeypatch.setattr(query_results, "_get_redis", _fake_get_redis)
    rid = await query_results.save_result(
        "sess2", ["a"], [{"a": 1}, {"a": 2}])
    # payload 已在 fake Redis 里
    assert f"result:{rid}" in fake.store
    r = await query_results.get_result(rid)
    assert r is not None
    assert r["columns"] == ["a"]
    assert r["rows"] == [{"a": 1}, {"a": 2}]
    assert r["total"] == 2


@pytest.mark.asyncio
async def test_redis_failure_falls_back_to_pg(db, monkeypatch):
    """Redis 读抛异常时降级到 PG（主链路不崩）。"""
    # 先 PG 存一条
    monkeypatch.setattr(query_results, "_get_redis", _no_redis)
    rid = await query_results.save_result("sess3", ["x"], [{"x": 9}])

    class _BoomRedis:
        async def get(self, key):
            raise RuntimeError("redis 挂了")

        async def set(self, key, value, ttl=None):
            raise RuntimeError("redis 挂了")

    async def _boom_get_redis():
        return _BoomRedis()

    monkeypatch.setattr(query_results, "_get_redis", _boom_get_redis)
    # save 再写一次同 id（Redis 写失败但 PG 兜底应不抛）；get 读 Redis 抛错应回退 PG
    rid2 = await query_results.save_result("sess3", ["x"], [{"x": 9}])
    r = await query_results.get_result(rid2)
    assert r is not None
    assert r["rows"] == [{"x": 9}]


@pytest.mark.asyncio
async def test_save_result_len0(db, monkeypatch):
    """空结果集 total=0，round-trip 仍正常。"""
    monkeypatch.setattr(query_results, "_get_redis", _no_redis)
    rid = await query_results.save_result("sess4", [], [])
    r = await query_results.get_result(rid)
    assert r is not None
    assert r["total"] == 0
    assert r["rows"] == []
