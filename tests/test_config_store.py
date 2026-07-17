import json

import pytest

from src.config_store.store import ConfigStore
from src.storage.models import AppConfigRow
from src.storage.pg_client import AsyncSessionFactory, init_db


@pytest.fixture
async def store():
    await init_db("sqlite+aiosqlite:///:memory:")
    return ConfigStore()


@pytest.mark.asyncio
async def test_get_returns_default_when_absent(store):
    assert await store.get("nope", default="fallback") == "fallback"


@pytest.mark.asyncio
async def test_get_default_none_when_no_default(store):
    assert await store.get("nope") is None


@pytest.mark.asyncio
async def test_set_then_get(store):
    await store.set("k1", {"v": 1})
    assert await store.get("k1") == {"v": 1}


@pytest.mark.asyncio
async def test_set_bumps_version(store):
    v1 = await store.set("k", "a")
    assert v1 == 1
    v2 = await store.set("k", "b")
    assert v2 == 2
    v3 = await store.set("k", "c")
    assert v3 == 3


@pytest.mark.asyncio
async def test_get_uses_cache_after_set(store):
    """set 后立即写入内存缓存，后续 get 不查 PG。"""
    await store.set("k", "v")
    assert await store.get("k") == "v"
    assert await store.get("k") == "v"


@pytest.mark.asyncio
async def test_refresh_reloads_from_pg(store):
    """绕过 store 直接改 PG 模拟外部写入，refresh 后读到新值。"""
    await store.set("k", "v1")
    assert await store.get("k") == "v1"
    async with AsyncSessionFactory() as s:
        row = await s.get(AppConfigRow, "k")
        row.value_json = json.dumps("v2")
        await s.commit()
    await store.refresh()
    assert await store.get("k") == "v2"


@pytest.mark.asyncio
async def test_set_handles_complex_value(store):
    await store.set("complex", {"nested": [1, 2, {"x": "y"}]})
    assert await store.get("complex") == {"nested": [1, 2, {"x": "y"}]}


@pytest.mark.asyncio
async def test_new_key_first_version_is_one(store):
    """首次 set 某个 key 时版本从 1 起。"""
    v = await store.set("fresh", "v")
    assert v == 1
