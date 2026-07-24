"""名称纠错前置（spec 6.3）。P0b pass-through，P2 注入三层 hook 启用真实纠错。
纯函数级组件，零 PG/Redis 依赖；P2 才接 name_dict 表。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable

from src.logging import get_logger

log = get_logger(__name__)


@dataclass
class Correction:
    """单条修正记录（对应 spec 6.3 输出契约 + AuditTrace.corrections_json 元素）。"""
    raw: str            # 原值（用户输入中的错误写法）
    standard: str       # 标准值（纠错后）
    confidence: float   # 置信度 0.0-1.0
    source: str         # typo/homophone/admin_area/llm


# P2 三层 hook 签名（P0b 全 None = pass-through）：
DictFn = Callable[[str], Awaitable["Correction | None"]]      # 字典层：精确命中
FuzzyFn = Callable[[str], Awaitable["Correction | None"]]     # 模糊层：Levenshtein+拼音
LLMFn = Callable[[str], Awaitable["tuple[str, list[Correction]]"]]  # LLM 兜底：语义改写


class Normalizer:
    """名称纠错前置。orchestrator 在 user_msg 进 agent_loop 之前调用。
    P0b 默认 pass-through：不传 hook 则 normalize() 原样返回 (text, [])。
    P2 注入 dict_fn/fuzzy_fn/llm_fn 后启用三层管线（spec 6.3）。"""

    def __init__(self, dict_fn: DictFn | None = None,
                 fuzzy_fn: FuzzyFn | None = None,
                 llm_fn: LLMFn | None = None) -> None:
        self._dict_fn = dict_fn
        self._fuzzy_fn = fuzzy_fn
        self._llm_fn = llm_fn

    async def normalize(self, text: str) -> tuple[str, list[Correction]]:
        """返回 (标准化文本, 修正记录)。P0b 无 hook → 原样返回。"""
        if not text or not (self._dict_fn or self._fuzzy_fn or self._llm_fn):
            return text or "", []
        return await self._apply_layers(text)

    async def _apply_layers(self, text: str) -> tuple[str, list[Correction]]:
        """三层管线：字典(精确) → 模糊(Levenshtein+拼音) → LLM(语义兜底)。
        P0b 仅字典层最小可用（confidence>=0.9 才替换），模糊/LLM 留 P2。"""
        out, corrections = text, []
        if self._dict_fn:
            cor = await self._dict_fn(text)
            if cor and cor.confidence >= 0.9:
                out = out.replace(cor.raw, cor.standard)
                corrections.append(cor)
        # 模糊层：字典没命中才试（编辑距离滑窗近似），confidence>=0.8 才替换
        if self._fuzzy_fn and not corrections:
            cor = await self._fuzzy_fn(out)
            if cor and cor.confidence >= 0.8:
                out = out.replace(cor.raw, cor.standard)
                corrections.append(cor)
        # LLM 层：前两层都没修正时整句语义改写兜底（给候选+上下文让 LLM 选）
        if self._llm_fn and not corrections:
            new_text, llm_corrections = await self._llm_fn(out)
            if llm_corrections:
                out = new_text
                corrections.extend(llm_corrections)
        return out, corrections


def corrections_to_json(corrections: list[Correction]) -> str:
    """供 orchestrator 落 AuditTrace.corrections_json（JSON 字符串）。"""
    return json.dumps([asdict(c) for c in corrections], ensure_ascii=False)
