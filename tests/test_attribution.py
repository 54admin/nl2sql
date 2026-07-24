import pytest

import src.knowledge.store as ks_module
from src.tools.attribution import do_attribution


class Ctx:
    session_id = "s"


class FakeStore:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, k=5):
        return self._hits


@pytest.mark.asyncio
async def test_attribution_with_docs(monkeypatch):
    """有文档依据时，框架含文档 + 主次因分层引导。"""
    monkeypatch.setattr(ks_module, "_store_singleton",
                        FakeStore([{"content": "夏季检修导致出力下降", "doc_id": 1}]))
    res = await do_attribution({"topic": "6月发电量下降"}, Ctx(), None)
    assert "归因" in res.summary
    assert "夏季检修导致出力下降" in res.summary   # 文档依据进了框架
    assert "主因" in res.summary and "次因" in res.summary


@pytest.mark.asyncio
async def test_attribution_no_docs(monkeypatch):
    """无文档时如实标注，不编造。"""
    monkeypatch.setattr(ks_module, "_store_singleton", FakeStore([]))
    res = await do_attribution({"topic": "X"}, Ctx(), None)
    assert "无相关文档" in res.summary


@pytest.mark.asyncio
async def test_attribution_kb_failure_falls_back(monkeypatch):
    """知识库检索异常（如未配 embedding）不炸归因，回退无文档。"""
    class BoomStore:
        async def search(self, q, k=5):
            raise RuntimeError("embedding 未配置")
    monkeypatch.setattr(ks_module, "_store_singleton", BoomStore())
    res = await do_attribution({"topic": "X"}, Ctx(), None)
    assert "无相关文档" in res.summary   # 异常被吞，回退


@pytest.mark.asyncio
async def test_attribution_missing_topic():
    res = await do_attribution({}, Ctx(), None)
    assert "topic" in res.summary
