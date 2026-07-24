import json

import pytest

from src.core.normalizer import Correction, Normalizer, corrections_to_json


@pytest.mark.asyncio
async def test_passthrough_no_hooks():
    n = Normalizer()
    text, corrections = await n.normalize("新疆省分公司6月发电量")
    assert text == "新疆省分公司6月发电量"
    assert corrections == []


@pytest.mark.asyncio
async def test_passthrough_empty_string():
    n = Normalizer()
    text, corrections = await n.normalize("")
    assert text == ""
    assert corrections == []


@pytest.mark.asyncio
async def test_passthrough_none():
    n = Normalizer()
    text, corrections = await n.normalize(None)
    assert text == ""
    assert corrections == []


@pytest.mark.asyncio
async def test_passthrough_whitespace_only():
    n = Normalizer()
    text, corrections = await n.normalize("   ")
    assert corrections == []


def test_correction_fields():
    c = Correction(raw="新疆省", standard="新疆", confidence=0.95, source="typo")
    assert c.raw == "新疆省"
    assert c.standard == "新疆"
    assert c.confidence == 0.95
    assert c.source == "typo"


def test_corrections_to_json_empty():
    assert corrections_to_json([]) == "[]"


def test_corrections_to_json_non_empty():
    c = Correction(raw="新疆省", standard="新疆", confidence=0.95, source="typo")
    out = json.loads(corrections_to_json([c]))
    assert out == [{"raw": "新疆省", "standard": "新疆",
                    "confidence": 0.95, "source": "typo"}]


@pytest.mark.asyncio
async def test_dict_fn_minimal_layer():
    async def fake_dict(text):
        return Correction(raw="新疆省", standard="新疆", confidence=0.99, source="typo")

    n = Normalizer(dict_fn=fake_dict)
    text, corrections = await n.normalize("新疆省发电量")
    assert "新疆" in text
    assert "新疆省" not in text
    assert len(corrections) == 1
    assert corrections[0].standard == "新疆"


@pytest.mark.asyncio
async def test_dict_fn_low_confidence_no_replace():
    async def fake_dict(text):
        return Correction(raw="新疆省", standard="新疆", confidence=0.5, source="typo")

    n = Normalizer(dict_fn=fake_dict)
    text, corrections = await n.normalize("新疆省发电量")
    assert text == "新疆省发电量"
    assert corrections == []


# ===== P2 fuzzy / llm 层 + 短路 =====

@pytest.mark.asyncio
async def test_fuzzy_fn_fallback_when_dict_miss():
    """dict 没命中时 fuzzy 兜底（confidence>=0.8 替换）。"""
    async def fake_fuzzy(text):
        return Correction(raw="新疆分公", standard="新疆分公司", confidence=0.85, source="fuzzy")

    n = Normalizer(fuzzy_fn=fake_fuzzy)
    text, corrections = await n.normalize("查新疆分公发电量")
    assert "新疆分公司" in text
    assert corrections[0].source == "fuzzy"


@pytest.mark.asyncio
async def test_fuzzy_low_confidence_no_replace():
    async def fake_fuzzy(text):
        return Correction(raw="x", standard="y", confidence=0.7, source="fuzzy")

    n = Normalizer(fuzzy_fn=fake_fuzzy)
    text, corrections = await n.normalize("原始文本")
    assert corrections == []


@pytest.mark.asyncio
async def test_llm_fn_fallback_when_dict_fuzzy_miss():
    """前两层都没修正时 llm 整句改写兜底。"""
    async def fake_llm(text):
        return "改写后文本", [Correction(raw=text, standard="改写后文本",
                                       confidence=0.6, source="llm")]

    n = Normalizer(llm_fn=fake_llm)
    text, corrections = await n.normalize("原始文本")
    assert text == "改写后文本"
    assert corrections[0].source == "llm"


@pytest.mark.asyncio
async def test_dict_hit_skips_fuzzy_and_llm():
    """dict 命中后不走 fuzzy/llm（避免过度改写）。"""
    calls = {"fuzzy": 0, "llm": 0}

    async def fake_dict(text):
        return Correction(raw="A省", standard="A", confidence=0.99, source="dict")

    async def fake_fuzzy(text):
        calls["fuzzy"] += 1
        return None

    async def fake_llm(text):
        calls["llm"] += 1
        return text, []

    n = Normalizer(dict_fn=fake_dict, fuzzy_fn=fake_fuzzy, llm_fn=fake_llm)
    await n.normalize("A省发电量")
    assert calls == {"fuzzy": 0, "llm": 0}
