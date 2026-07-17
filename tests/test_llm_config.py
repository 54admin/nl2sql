import pytest

from src.llm.service import LLMService
from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory, init_db


@pytest.fixture
async def svc():
    await init_db("sqlite+aiosqlite:///:memory:")
    return LLMService()  # 配置全数据库，构造无参


@pytest.mark.asyncio
async def test_no_config_raises(svc):
    """数据库无配置 → _resolve_config 抛错（提示 PUT）。"""
    with pytest.raises(RuntimeError, match="未配置"):
        await svc._resolve_config()


@pytest.mark.asyncio
async def test_disabled_config_raises(svc):
    """enabled=False 视同未配置 → 抛错。"""
    async with AsyncSessionFactory() as s:
        s.add(LlmConfigRow(id="default", model="m", base_url="u", api_key="k",
                           temperature=0.0, timeout=60, enabled=False, version=1))
        await s.commit()
    with pytest.raises(RuntimeError):
        await svc._resolve_config()


@pytest.mark.asyncio
async def test_dynamic_config_used(svc):
    """数据库 enabled 配置 → _resolve_config 返回该配置。"""
    async with AsyncSessionFactory() as s:
        s.add(LlmConfigRow(id="default", model="dyn-model", base_url="dyn-url",
                           api_key="dyn-key", temperature=0.7, timeout=120,
                           enabled=True, version=1))
        await s.commit()
    cfg = await svc._resolve_config()
    assert cfg.model == "dyn-model"
    assert cfg.api_base == "dyn-url"
    assert cfg.api_key == "dyn-key"
    assert cfg.temperature == 0.7
    assert cfg.timeout == 120


@pytest.mark.asyncio
async def test_reset_dynamic_reloads(svc):
    """reset_dynamic 后下次读最新 PG。"""
    async with AsyncSessionFactory() as s:
        s.add(LlmConfigRow(id="default", model="v1", base_url="u1", api_key="k",
                           temperature=0.0, timeout=60, enabled=True, version=1))
        await s.commit()
    cfg = await svc._resolve_config()
    assert cfg.model == "v1"
    async with AsyncSessionFactory() as s:
        row = await s.get(LlmConfigRow, "default")
        row.model = "v2"
        await s.commit()
    cfg = await svc._resolve_config()
    assert cfg.model == "v1"  # 未 reset 读缓存
    svc.reset_dynamic()
    cfg = await svc._resolve_config()
    assert cfg.model == "v2"  # reset 后读最新


@pytest.mark.asyncio
async def test_pg_failure_raises(svc):
    """PG 异常 → _load_dynamic 返回 None → _resolve_config 抛错（不静默兜底）。"""
    import src.llm.service as svc_mod

    class BoomFactory:
        def __call__(self):
            raise RuntimeError("pg down")

    orig = svc_mod.AsyncSessionFactory
    svc_mod.AsyncSessionFactory = BoomFactory()
    try:
        with pytest.raises(RuntimeError):
            await svc._resolve_config()
    finally:
        svc_mod.AsyncSessionFactory = orig
