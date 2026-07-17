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
