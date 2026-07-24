"""知识库（P3b）：文档分段 + embedding 入库 + 向量检索。
借鉴 RagFlow：分段优先按段落/标题（保留结构），长段落按字滑窗带 overlap 兜底；
检索 PG 走 pgvector cosine（<=>），sqlite 走 Python cosine（测试兼容）。
ponytail: 第一版纯向量检索；混合检索（中文关键词 BM25）待 zhparser/pg_jieba 确认后加。"""
from __future__ import annotations

import json
import math

from sqlalchemy import text

from src.logging import get_logger
from src.storage.models import KnowledgeChunk, KnowledgeDoc
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)

CHUNK_SIZE = 500      # 每段目标字符数（中文按字）
CHUNK_OVERLAP = 80    # 段间重叠（保上下文连续性，防切断语义）


def chunk_text(content: str, size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """分段：优先按段落（双换行）切；段落超 size 再按字滑窗切（带 overlap 保上下文）。
    ponytail: 单策略（段落优先 + 字滑窗兜底）；RagFlow 的 manual/laws 等多模板后续按 category 加。"""
    if not content or not content.strip():
        return []
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    chunks: list[str] = []
    for para in paragraphs:
        if len(para) <= size:
            chunks.append(para)
        else:
            _slide_append(para, size, overlap, chunks)
    # 兜底：无段落分隔的纯文本，整篇按字滑窗
    if not chunks:
        _slide_append(content, size, overlap, chunks)
    return chunks


def _slide_append(s: str, size: int, overlap: int, out: list[str]) -> None:
    """长文本按字滑窗切段（步长=size-overlap），带 overlap 保上下文。"""
    step = max(1, size - overlap)
    for i in range(0, len(s), step):
        seg = s[i:i + size]
        if seg.strip():
            out.append(seg)
        if i + size >= len(s):
            break


class KnowledgeStore:
    """知识库读写：文档分段 embedding 入库 + 向量检索。依赖 LLMService.embed。"""

    def __init__(self, llm=None):
        self._llm = llm   # LLMService（调 embed）；None 时首次 embed lazy 新建（工具自包含）

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """批量 embedding。llm 未注入则 lazy 新建 LLMService（从 PG 读配置）。"""
        if self._llm is None:
            from src.llm.service import LLMService
            self._llm = LLMService()
        return await self._llm.embed(texts)

    async def add_document(self, name: str, content: str,
                           category: str = "general") -> int:
        """文档 → 分段 → embedding → 入库。返回 doc_id。"""
        chunks = chunk_text(content)
        if not chunks:
            raise ValueError("文档内容为空或分段后无有效段落")
        vecs = await self._embed(chunks)
        if len(vecs) != len(chunks):
            raise RuntimeError(f"embedding 数 {len(vecs)} ≠ 分段数 {len(chunks)}")
        async with AsyncSessionFactory() as s:
            doc = KnowledgeDoc(name=name, category=category, enabled=True,
                               chunk_count=len(chunks))
            s.add(doc)
            await s.flush()                       # 拿 doc.id
            for i, (chunk, vec) in enumerate(zip(chunks, vecs)):
                s.add(KnowledgeChunk(doc_id=doc.id, chunk_index=i,
                                     content=chunk, embedding=vec))
            await s.commit()
        log.info("知识库入库: doc=%s chunks=%d", name, len(chunks))
        return doc.id

    async def list_documents(self, category: str | None = None) -> list[dict]:
        async with AsyncSessionFactory() as s:
            stmt = KnowledgeDoc.__table__.select()
            if category:
                stmt = stmt.where(KnowledgeDoc.category == category)
            rows = (await s.execute(stmt)).all()
        return [{"id": r.id, "name": r.name, "category": r.category,
                 "enabled": r.enabled, "chunk_count": r.chunk_count,
                 "version": r.version} for r in rows]

    async def set_enabled(self, doc_id: int, enabled: bool) -> bool:
        async with AsyncSessionFactory() as s:
            doc = await s.get(KnowledgeDoc, doc_id)
            if doc is None:
                return False
            doc.enabled = enabled
            doc.version += 1
            await s.commit()
        return True

    async def delete_document(self, doc_id: int) -> bool:
        """删除文档 + 级联删其所有分段。"""
        async with AsyncSessionFactory() as s:
            doc = await s.get(KnowledgeDoc, doc_id)
            if doc is None:
                return False
            await s.execute(KnowledgeChunk.__table__.delete().where(
                KnowledgeChunk.doc_id == doc_id))
            await s.delete(doc)
            await s.commit()
        return True

    async def search(self, query: str, k: int = 5) -> list[dict]:
        """语义检索：query → embedding → 近邻 top-k（只查 enabled doc 的 chunks）。
        PG 走 pgvector cosine 距离（<=>，越小越相似）；sqlite 走 Python cosine（测试兼容）。"""
        vec = (await self._embed([query]))[0]
        async with AsyncSessionFactory() as s:
            if s.bind.dialect.name == "sqlite":
                rows = await self._search_sqlite(s, vec, k)
            else:
                rows = await self._search_pg(s, vec, k)
        return rows

    async def _search_pg(self, s, vec: list[float], k: int) -> list[dict]:
        """PG：pgvector cosine 距离算子 <=>，join enabled doc 过滤，取 top-k。"""
        sql = text("""SELECT c.doc_id, c.content, (c.embedding <=> (:vec)::vector) AS score
                      FROM knowledge_chunks c JOIN knowledge_docs d ON c.doc_id = d.id
                      WHERE d.enabled = true
                      ORDER BY c.embedding <=> (:vec)::vector LIMIT :k""")
        res = await s.execute(sql, {"vec": json.dumps(vec), "k": k})
        return [{"doc_id": r.doc_id, "content": r.content, "score": r.score} for r in res]

    async def _search_sqlite(self, s, vec: list[float], k: int) -> list[dict]:
        """sqlite：embedding 存 JSON list，Python cosine 近邻（测试用，量小可接受）。"""
        res = await s.execute(text("""SELECT c.doc_id, c.content, c.embedding
                                      FROM knowledge_chunks c JOIN knowledge_docs d ON c.doc_id = d.id
                                      WHERE d.enabled = true"""))
        scored = []
        for r in res:
            emb = r.embedding if isinstance(r.embedding, list) else json.loads(r.embedding)
            scored.append((_cosine(vec, emb), r.doc_id, r.content))
        scored.sort(key=lambda x: x[0], reverse=True)   # 相似度越大越好
        return [{"doc_id": d, "content": c, "score": 1.0 - sim}   # 转距离与 PG 一致（越小越好）
                for sim, d, c in scored[:k] if sim > 0]


_store_singleton: "KnowledgeStore | None" = None


def get_knowledge_store() -> "KnowledgeStore":
    """进程级单例（工具/路由共享；首次 embed lazy 建 LLMService）。"""
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = KnowledgeStore()
    return _store_singleton


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（-1~1，越大越相似）。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
