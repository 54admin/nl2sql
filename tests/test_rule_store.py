import pytest

from src.core.rule_store import RuleStore
from src.storage.models import BusinessRule
from src.storage.pg_client import AsyncSessionFactory, init_db


async def _seed(rows):
    async with AsyncSessionFactory() as s:
        for r in rows:
            s.add(BusinessRule(**r))
        await s.commit()


@pytest.fixture
async def db():
    await init_db("sqlite+aiosqlite:///:memory:")
    yield


@pytest.mark.asyncio
async def test_all_text_empty(db):
    assert await RuleStore().all_text() == ""


@pytest.mark.asyncio
async def test_all_text_joins_only_enabled(db):
    await _seed([
        {"category": "metric", "key": "发电量单位", "value_json": '"万kWh"', "enabled": True},
        {"category": "metric", "key": "停用规则", "value_json": '"x"', "enabled": False},
    ])
    text = await RuleStore().all_text()
    assert "发电量单位" in text and "万kWh" in text
    assert "停用规则" not in text    # disabled 不进
