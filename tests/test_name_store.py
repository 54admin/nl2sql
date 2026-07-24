import httpx
import pytest
from fastapi import FastAPI

from src.core.name_store import NameStore
from src.storage.models import NameDict
from src.storage.pg_client import AsyncSessionFactory, init_db
from src.web.routes.admin_name_dict import build_name_dict_router


async def _seed(rows):
    async with AsyncSessionFactory() as s:
        for r in rows:
            s.add(NameDict(**r))
        await s.commit()


@pytest.fixture
async def db():
    await init_db("sqlite+aiosqlite:///:memory:")
    yield


@pytest.mark.asyncio
async def test_lookup_exact_hit(db):
    await _seed([{"alias": "疆分公司", "standard": "新疆分公司", "enabled": True}])
    cor = await NameStore().lookup_exact("查一下疆分公司6月发电量")
    assert cor is not None
    assert cor.raw == "疆分公司"
    assert cor.standard == "新疆分公司"
    assert cor.source == "dict"


@pytest.mark.asyncio
async def test_lookup_exact_miss(db):
    await _seed([{"alias": "西藏分公司", "standard": "西藏", "enabled": True}])
    assert await NameStore().lookup_exact("查新疆发电量") is None


@pytest.mark.asyncio
async def test_lookup_exact_alias_eq_standard_skipped(db):
    """alias==standard 不算纠错（防自映射）。"""
    await _seed([{"alias": "新疆分公司", "standard": "新疆分公司", "enabled": True}])
    assert await NameStore().lookup_exact("新疆分公司发电量") is None


@pytest.mark.asyncio
async def test_lookup_fuzzy_hit(db):
    """未精确命中时，编辑距离近似命中（漏字场景）。"""
    await _seed([{"alias": "别名占位", "standard": "新疆分公司", "enabled": True}])
    cor = await NameStore().lookup_fuzzy("查新疆分公的发电量")
    assert cor is not None
    assert cor.standard == "新疆分公司"
    assert cor.source == "fuzzy"


@pytest.mark.asyncio
async def test_lookup_only_enabled(db):
    """disabled 的别名不进缓存。"""
    await _seed([{"alias": "疆分公司", "standard": "新疆分公司", "enabled": False}])
    assert await NameStore().lookup_exact("疆分公司发电量") is None


# ===== admin_name_dict 路由 CRUD =====

@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(build_name_dict_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_crud(client):
    r = await client.post("/api/admin/name-dict", json={
        "alias": "疆分公司", "standard": "新疆分公司", "category": "table"})
    assert r.status_code == 200
    item_id = r.json()["id"]

    r = await client.get("/api/admin/name-dict")
    assert r.json()["items"][0]["alias"] == "疆分公司"

    r = await client.put(f"/api/admin/name-dict/{item_id}", json={"enabled": False})
    assert r.status_code == 200

    r = await client.delete(f"/api/admin/name-dict/{item_id}")
    assert r.status_code == 200
    r = await client.get("/api/admin/name-dict")
    assert r.json()["items"] == []
