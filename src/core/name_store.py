"""名称纠错别名字典缓存（P2）。内存缓存 enabled 的 alias→standard，
供 normalizer dict_fn/fuzzy_fn 用；build_llm_corrector 构造 llm_fn 兜底整句改写。
TTL 刷新：admin 改完最多 TTL 秒后生效（字典低频变更，比路由耦合 refresh 省）。
ponytail: 跨进程广播失效 P5 改 Redis pub/sub。"""
from __future__ import annotations

from difflib import SequenceMatcher

from src.core.normalizer import Correction
from src.logging import get_logger
from src.storage.models import NameDict
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)

_TTL = 30.0   # 缓存有效期（秒）；过期下次 lookup 重读 PG


class NameStore:
    """alias→standard 内存缓存。lookup_exact 精确子串，lookup_fuzzy 滑窗近似。"""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}    # alias → standard
        self._loaded_at: float = 0.0

    async def _ensure_loaded(self) -> None:
        import time
        if self._cache and time.monotonic() - self._loaded_at < _TTL:
            return
        async with AsyncSessionFactory() as s:
            stmt = NameDict.__table__.select().where(NameDict.enabled.is_(True))
            rows = (await s.execute(stmt)).all()
        self._cache = {r.alias: r.standard for r in rows}
        self._loaded_at = time.monotonic()

    async def lookup_exact(self, text: str) -> Correction | None:
        """精确子串匹配：text 含某 alias 且 alias≠standard，返回首个命中。confidence 高。"""
        if not text:
            return None
        await self._ensure_loaded()
        for alias, standard in self._cache.items():
            if alias != standard and alias in text:
                return Correction(raw=alias, standard=standard,
                                  confidence=0.95, source="dict")
        return None

    async def lookup_fuzzy(self, text: str) -> Correction | None:
        """滑窗 + SequenceMatcher 近似匹配（编辑距离兜底），ratio>=0.85 才命中。
        ponytail: O(standards × len × win)；标准名几十个量级可接受，超量加前缀索引。"""
        if not text:
            return None
        await self._ensure_loaded()
        best = None
        best_ratio = 0.85
        for standard in set(self._cache.values()):
            L = len(standard)
            for win in range(max(1, L - 2), L + 3):
                if win > len(text):
                    break
                for i in range(len(text) - win + 1):
                    r = SequenceMatcher(None, standard, text[i:i + win]).ratio()
                    if r > best_ratio:
                        best_ratio = r
                        best = (standard, i, i + win)
        if best:
            standard, i, j = best
            return Correction(raw=text[i:j], standard=standard,
                              confidence=0.8, source="fuzzy")
        return None

    async def refresh(self) -> None:
        """强制下次 lookup 重读 PG（admin 改完可调，立即生效）。"""
        self._loaded_at = 0.0


def build_llm_corrector(name_store: NameStore, llm_service):
    """构造 LLM 兜底纠错 hook（normalizer llm_fn）。
    给候选标准名 + 用户文本让 LLM 改实体名错写；前两层没命中才调（normalizer 控制）。
    LLM 不可用/无修正返回 (text, [])，不抛。"""
    async def llm_fn(text: str):
        await name_store._ensure_loaded()
        candidates = sorted(set(name_store._cache.values()))
        if not candidates or not text:
            return text, []
        prompt = (
            "判断用户输入里的实体名是否有错写/别名，若有则改成下面列表里的标准名。\n"
            f"标准名候选：{candidates}\n"
            f"用户输入：{text}\n"
            "规则：只改实体名错写（如「新疆省分公司」→「新疆分公司」），"
            "不要改数字/时间/语气词；无需修改则原样返回。只输出改写后的整句，不要解释。"
        )
        try:
            resp = await llm_service.chat([{"role": "user", "content": prompt}])
            new_text = (resp.content or "").strip()
        except Exception as e:
            log.warning("LLM 纠错失败，跳过: %s", e)
            return text, []
        if not new_text or new_text == text:
            return text, []
        return new_text, [Correction(raw=text, standard=new_text,
                                     confidence=0.6, source="llm")]
    return llm_fn
