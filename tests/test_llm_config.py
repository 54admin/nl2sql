import pytest

from src.config import LLMConfig
from src.llm.service import LLMService
from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory, init_db


@pytest.fixture
async def svc():
    await init_db("sqlite+aiosqlite:///:memory:")
    yaml_cfg = LLMConfig(api_key="yaml-key", api_base="yaml-url",
                         model="yaml-model", temperature=0.0, timeout=60)
    return LLMService(yaml_cfg)


@pytest.mark.asyncio
async def test_fallback_to_yaml_when_no_dynamic(svc):
    cfg = await svc._resolve_config()
    assert cfg.model == "yaml-model"
    assert cfg.api_base == "yaml-url"
    assert cfg.api_key == "yaml-key"


@pytest.mark.asyncio
async def test_dynamic_overrides_yaml(svc):
    async with AsyncSessionFactory() as s:
        s.add(LlmConfigRow(id="default", model="dyn-model", base_url="dyn-url",
                           api_key="dyn-key", temperature=0.7, timeout=120,
                           enabled=True, version=1))
        await s.commit()
    cfg = await svc._resolve_config()
    assert cfg.model == "dyn-model"
    assert cfg.api_base == "dyn-url"
    assert cfg.temperature == 0.7
    assert cfg.timeout == 120


@pytest.mark.asyncio
async def test_disabled_dynamic_falls_back_to_yaml(svc):
    async with AsyncSessionFactory() as s:
        s.add(LlmConfigRow(id="default", model="dyn", base_url="dyn",
                           api_key="x", temperature=0.0, timeout=60,
                           enabled=False, version=1))
        await s.commit()
    cfg = await svc._resolve_config()
    assert cfg.model == "yaml-model"


@pytest.mark.asyncio
async def test_reset_dynamic_reloads_on_next_call(svc):
    async with AsyncSessionFactory() as s:
        s.add(LlmConfigRow(id="default", model="v1", base_url="u1",
                           api_key="k", temperature=0.0, timeout=60,
                           enabled=True, version=1))
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
    assert cfg.model == "v2"


@pytest.mark.asyncio
async def test_load_dynamic_failure_falls_back_silently(svc):
    import src.llm.service as svc_mod

    class BoomFactory:
        def __call__(self):
            raise RuntimeError("pg down")

    orig = svc_mod.AsyncSessionFactory
    svc_mod.AsyncSessionFactory = BoomFactory()
    try:
        cfg = await svc._resolve_config()
        assert cfg.model == "yaml-model"
    finally:
        svc_mod.AsyncSessionFactory = orig
