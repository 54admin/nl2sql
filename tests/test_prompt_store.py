import pytest

from src.core.prompt_store import PromptStore
from src.storage.pg_client import init_db


@pytest.fixture
async def store():
    await init_db("sqlite+aiosqlite:///:memory:")
    return PromptStore()


@pytest.mark.asyncio
async def test_get_default_falls_back_to_builtin_when_absent(store):
    """default 场景未配置时返回内置兜底（保证 LLM 有两步链路引导），而非 None。"""
    from src.core.prompt_store import DEFAULT_PROMPT
    got = await store.get("default")
    assert got is not None
    assert got == DEFAULT_PROMPT
    assert "query_metadata" in got


@pytest.mark.asyncio
async def test_get_unknown_scene_returns_none(store):
    """非 default 场景未配置仍返回 None（attribution 等场景走 P3 再配）。"""
    assert await store.get("attribution") is None


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
async def test_delete_falls_back_to_builtin(store):
    """删除 default 后回到「从未配置」状态 → 兜底内置 prompt（管理员想关引导用禁用，删除=重置）。"""
    from src.core.prompt_store import DEFAULT_PROMPT
    await store.upsert("default", "x")
    assert await store.delete("default") is True
    got = await store.get("default")
    assert got == DEFAULT_PROMPT
    assert await store.delete("default") is False  # 已无记录


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
