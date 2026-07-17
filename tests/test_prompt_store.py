import pytest

from src.core.prompt_store import PromptStore
from src.storage.pg_client import init_db


@pytest.fixture
async def store():
    await init_db("sqlite+aiosqlite:///:memory:")
    return PromptStore()


@pytest.mark.asyncio
async def test_get_returns_none_when_absent(store):
    assert await store.get("default") is None


@pytest.mark.asyncio
async def test_upsert_then_get(store):
    v = await store.upsert("default", "你是问数助手")
    assert v == 1
    assert await store.get("default") == "你是问数助手"


@pytest.mark.asyncio
async def test_upsert_bumps_version(store):
    v1 = await store.upsert("default", "v1")
    assert v1 == 1
    v2 = await store.upsert("default", "v2")
    assert v2 == 2


@pytest.mark.asyncio
async def test_disabled_returns_none(store):
    await store.upsert("default", "x", enabled=True)
    await store.upsert("default", "x", enabled=False)
    assert await store.get("default") is None


@pytest.mark.asyncio
async def test_delete(store):
    await store.upsert("default", "x")
    assert await store.delete("default") is True
    assert await store.get("default") is None
    assert await store.delete("default") is False


@pytest.mark.asyncio
async def test_list_all(store):
    await store.upsert("default", "d")
    await store.upsert("attribution", "a")
    items = await store.list_all()
    scenes = {it["scene"] for it in items}
    assert scenes == {"default", "attribution"}


@pytest.mark.asyncio
async def test_get_uses_cache(store):
    await store.upsert("default", "v1")
    assert await store.get("default") == "v1"
    from src.storage.models import Prompt as PromptRow
    from src.storage.pg_client import AsyncSessionFactory
    async with AsyncSessionFactory() as s:
        row = await s.get(PromptRow, "default")
        row.content = "v2"
        await s.commit()
    assert await store.get("default") == "v1"
    await store.refresh()
    assert await store.get("default") == "v2"


@pytest.mark.asyncio
async def test_multiple_scenes_independent(store):
    await store.upsert("default", "D")
    await store.upsert("correction", "C")
    assert await store.get("default") == "D"
    assert await store.get("correction") == "C"
