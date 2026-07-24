import httpx
import pytest
from fastapi import FastAPI

import src.knowledge.store as ks_module
from src.knowledge.store import KnowledgeStore, chunk_text
from src.storage.models import KnowledgeChunk
from src.storage.pg_client import AsyncSessionFactory, init_db
from src.web.routes.admin_knowledge import build_knowledge_router


class FakeLLM:
    """字符 bag 假 embedding：共享字符多的文本向量相近（测检索排序，非真语义）。"""
    VOCAB = list("发电量下降检修限电夏季窗口手册政策")

    async def embed(self, texts):
        return [[float(t.count(c)) for c in self.VOCAB] for t in texts]


@pytest.fixture
async def db():
    await init_db("sqlite+aiosqlite:///:memory:")
    yield


# ===== chunk_text 分段 =====

def test_chunk_text_by_paragraph():
    chunks = chunk_text("第一段短内容。\n\n第二段也短。")
    assert chunks == ["第一段短内容。", "第二段也短。"]


def test_chunk_text_long_paragraph_slides():
    para = "字" * 1200   # 超 CHUNK_SIZE(500)，滑窗切
    chunks = chunk_text(para)
    assert len(chunks) >= 2
    assert all(len(c) <= 500 for c in chunks)


def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   ") == []
    assert chunk_text(None) == []


# ===== KnowledgeStore 入库/检索/删除/启停 =====

@pytest.mark.asyncio
async def test_add_and_list(db):
    store = KnowledgeStore(FakeLLM())
    doc_id = await store.add_document("手册.md", "发电量下降原因\n\n夏季检修窗口", "manual")
    docs = await store.list_documents()
    assert docs[0]["id"] == doc_id
    assert docs[0]["name"] == "手册.md"
    assert docs[0]["chunk_count"] == 2


@pytest.mark.asyncio
async def test_search_returns_relevant(db):
    store = KnowledgeStore(FakeLLM())
    await store.add_document("d", "发电量下降\n\n夏季检修窗口")
    rows = await store.search("发电量下降", k=2)
    assert rows
    # query 与 chunk "发电量下降" 字符完全重叠 → cosine 最高 → 排第一
    assert "发电量下降" in rows[0]["content"]


@pytest.mark.asyncio
async def test_search_empty_when_no_doc(db):
    assert await KnowledgeStore(FakeLLM()).search("随便", k=3) == []


@pytest.mark.asyncio
async def test_delete_cascades_chunks(db):
    store = KnowledgeStore(FakeLLM())
    doc_id = await store.add_document("d", "段一\n\n段二")
    assert await store.delete_document(doc_id) is True
    assert await store.list_documents() == []
    async with AsyncSessionFactory() as s:
        rows = (await s.execute(KnowledgeChunk.__table__.select())).all()
    assert rows == []


@pytest.mark.asyncio
async def test_disabled_doc_excluded_from_search(db):
    store = KnowledgeStore(FakeLLM())
    doc_id = await store.add_document("d", "发电量下降")
    assert await store.set_enabled(doc_id, False) is True
    assert await store.search("发电量下降", k=3) == []


# ===== admin 路由（上传/列表/启停/删除）=====

@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    # 路由用 get_knowledge_store 单例：注入 fake，避免 lazy LLMService 连真网关
    ks_module._store_singleton = KnowledgeStore(FakeLLM())
    app = FastAPI()
    app.include_router(build_knowledge_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    ks_module._store_singleton = None


@pytest.mark.asyncio
async def test_upload_list_disable_delete(client):
    resp = await client.post(
        "/api/admin/knowledge/upload",
        files={"file": ("policy.md", "发电量下降原因".encode("utf-8"), "text/plain")},
        data={"category": "policy"},
    )
    assert resp.status_code == 200
    doc_id = resp.json()["id"]

    resp = await client.get("/api/admin/knowledge/docs")
    docs = resp.json()["docs"]
    assert docs[0]["id"] == doc_id
    assert docs[0]["category"] == "policy"

    assert (await client.put(f"/api/admin/knowledge/docs/{doc_id}",
                             params={"enabled": "false"})).status_code == 200
    assert (await client.delete(f"/api/admin/knowledge/docs/{doc_id}")).status_code == 200
    assert (await client.get("/api/admin/knowledge/docs")).json()["docs"] == []
