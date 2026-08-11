# -*- coding: utf-8 -*-
"""知识库参考来源(citations)单元测试：URL 拼接 + 文档级去重 + 聚合。

URL 模板按 RAGFlow v0.26.4 前端路由 /document/:id?ext= 校准（web/src/routes.tsx）。"""
from src.tools.knowledge_tool import build_doc_url, _build_references
from src.core.agent_loop import _dedupe_citations


class TestBuildDocUrl:
    def test_normal_with_ext(self):
        # document_name 带扩展名 → 拼 ?ext=pdf；路径参数 document_id，无 dataset
        assert build_doc_url("http://10.0.0.1:9380", "doc1", "运维手册.pdf") == \
            "http://10.0.0.1:9380/document/doc1?ext=pdf"

    def test_strip_trailing_slash(self):
        assert build_doc_url("http://x:9380/", "doc", "a.docx") == \
            "http://x:9380/document/doc?ext=docx"

    def test_uppercase_ext_lowered(self):
        assert build_doc_url("http://x", "doc", "REPORT.PDF") == \
            "http://x/document/doc?ext=pdf"

    def test_no_ext_no_query(self):
        # 文档名无扩展名 → 不带 ?ext
        assert build_doc_url("http://x", "doc", "无扩展名文件") == "http://x/document/doc"

    def test_no_base_url(self):
        assert build_doc_url("", "doc", "a.pdf") == ""

    def test_no_document_id(self):
        assert build_doc_url("http://x", "", "a.pdf") == ""


class TestBuildReferences:
    def test_dedupe_keep_highest(self):
        rows = [
            {"document": "A.pdf", "similarity": 0.9, "document_id": "d1", "dataset_id": "ds", "content": "高分片段"},
            {"document": "A.pdf", "similarity": 0.7, "document_id": "d1", "dataset_id": "ds", "content": "低分片段"},
            {"document": "B.docx", "similarity": 0.8, "document_id": "d2", "dataset_id": "ds", "content": "B 片段"},
        ]
        refs = _build_references(rows, "http://x")
        assert len(refs) == 2
        assert refs[0]["document"] == "A.pdf" and refs[0]["similarity"] == 0.9
        assert refs[1]["document"] == "B.docx" and refs[1]["similarity"] == 0.8
        # URL 用 document_id 拼，ext 从文档名提取；不含 dataset_id
        assert refs[0]["url"] == "http://x/document/d1?ext=pdf"
        assert refs[1]["url"] == "http://x/document/d2?ext=docx"
        # content 取最高相似度那条的片段
        assert refs[0]["content"] == "高分片段"
        assert refs[1]["content"] == "B 片段"

    def test_content_truncated(self):
        from src.tools.knowledge_tool import SNIPPET_LIMIT
        long = "字" * (SNIPPET_LIMIT + 400)
        rows = [{"document": "A.pdf", "similarity": 0.9, "document_id": "d1", "dataset_id": "ds", "content": long}]
        refs = _build_references(rows, "http://x")
        assert len(refs[0]["content"]) == SNIPPET_LIMIT + 1   # 截断 + …
        assert refs[0]["content"].endswith("…")
        assert refs[0]["content"][:SNIPPET_LIMIT] == long[:SNIPPET_LIMIT]

    def test_empty(self):
        assert _build_references([], "http://x") == []

    def test_skip_unnamed(self):
        rows = [{"document": "", "similarity": 0.9, "document_id": "d1", "dataset_id": "ds"}]
        assert _build_references(rows, "http://x") == []


class TestDedupeCitations:
    def test_cross_call_dedupe(self):
        # 一次 run 内两次 knowledge_search 命中同一文档 → 去重取最高相似度（content 随之保留）
        citations = [
            {"document": "A", "similarity": 0.7, "url": "u1", "content": "片段甲"},
            {"document": "A", "similarity": 0.9, "url": "u1", "content": "片段乙"},
            {"document": "B", "similarity": 0.8, "url": "u2", "content": "片段丙"},
        ]
        out = _dedupe_citations(citations)
        assert len(out) == 2
        assert out[0]["document"] == "A" and out[0]["similarity"] == 0.9
        assert out[0]["content"] == "片段乙"
        assert out[1]["document"] == "B"

    def test_empty(self):
        assert _dedupe_citations([]) == []
