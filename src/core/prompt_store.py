"""skill 单一真相源：DB prompts 表即权威，运行时只读 DB。
seed 由 scripts/gen_seed.py 的 SEED_SKILLS 常量灌库（首次部署），之后运行时只读 DB。
orchestrator 调 assemble_system_prompt 装配（KERNEL + enabled 的 always_on skill）。
admin 改 content/enabled/tools 后失效缓存 + reload_registry 热生效。
ponytail: 单进程内存缓存；跨进程广播 P5 改 Redis pub/sub。"""
from __future__ import annotations

# 注意：load_skills 仅 scripts/gen_seed.py 用，运行时不读 md（DB 即真相源）
from src.logging import get_logger
from src.storage.models import Prompt
from src.storage.db_client import AsyncSessionFactory

log = get_logger(__name__)

_ASSEMBLED_KEY = "__assembled__"  # assemble_system_prompt 缓存 key（orchestrator 每轮调）

# ============================================================================
# 内核协议（KERNEL）：纯 agent 协议——角色 + ReAct 运行规则，零业务方法论。
# 永远在最前（稳定缓存前缀的一部分），不随业务变动。新能力不进这里，走 skill。
# ============================================================================
KERNEL_PROMPT = """你是企业智能助手，帮用户查业务数据、查文档资料、做归因分析。用户用自然语言提问，你通过调用工具获取真实数据或文档依据后，用中文准确回答。

你是 ReAct 智能体：思考 → 调用工具 → 查看返回 → 再思考，循环到能给出可信回答为止。
你有一套工具（见工具列表）和各能力的方法论（见下文技能段）。每个工具的描述写明了它管什么、不管什么——
根据用户问题自己分辨该用哪个工具，不要被对话历史带偏（上一轮在查数据不代表这轮也查数据）。

运行铁律：
- 思考链（thinking/reasoning）与回答一律用中文。分析字段/SQL 时可保留英文标识符，
  但推理句子、步骤说明必须是中文——用户会实时看到你的思考过程。
- 一切数据/结论必须来自工具返回，绝不凭印象编造数值、字段名或结论。
- 查不到就如实说明，不凑数、不猜测、不用「大概/约」。
- 工具拿到的才是事实，回答里每个数值/结论都要有工具结果支撑。
- 缺关键信息无法可靠行动时，调 ask_user 向用户澄清（尽量给 2-4 个候选 options，别硬猜）；用户已明确给的不重复问。
- 拿到可信结果就调 finish 给出最终回答、结束本轮——不无限循环、不堆砌无关内容。
- 不要被对话历史带偏：每个新问题都独立判断类型，上一轮在查数据不代表这轮也是查数据。

下面各段是具体能力的方法论，结合用户问题按需遵循。"""

# Skills（领域方法论）存 DB prompts 表；seed 由 gen_seed.py 的 SEED_SKILLS 常量灌入。
# 每个 skill 自包含（frontmatter 元数据 + 提示词正文）；DB prompts 表可覆盖正文（admin 热改）。

class PromptStore:
    """skill 提示词：内存缓存 + PG 持久。
    get 内存缓存优先，miss 读 PG 回填；upsert 写 PG + bump version + 刷新缓存。"""

    def __init__(self) -> None:
        # value 存 None 表示「已查过 PG 确认无记录」，和「未查过」区分开
        self._cache: dict[str, tuple[str | None, int] | None] = {}

    async def get(self, scene: str) -> str | None:
        """读 DB 原始 content（admin 编辑/预览用，不过滤 enabled）。
        有行就返 content（哪怕 enabled=False，admin 要看见存的啥才能编辑/重新启用）；无行返 None。"""
        cached = self._cache.get(scene)
        if cached is not None:
            return cached[0]
        async with AsyncSessionFactory() as s:
            row = await s.get(Prompt, scene)
        value = row.content if row else None
        self._cache[scene] = (value, row.version if row else 0)
        return value

    async def upsert(self, scene: str, content: str, *,
                     tools: list[str] | None = None,
                     mode: str | None = None,
                     order: int | None = None,
                     enabled: bool | None = None) -> int:
        """改 skill（DB 单一真相源）。content 必填；tools/mode/order/enabled 不传=不动。
        写库后失效装配缓存；caller（admin 路由）负责 reload_registry 热刷工具。"""
        async with AsyncSessionFactory() as s:
            row = await s.get(Prompt, scene)
            if row:
                row.content = content
                if tools is not None:
                    row.tools = tools
                if mode is not None:
                    row.mode = mode
                if order is not None:
                    row.order = order
                if enabled is not None:
                    row.enabled = enabled
                row.version += 1
                new_version = row.version
            else:
                s.add(Prompt(scene=scene, content=content,
                             tools=tools if tools is not None else [],
                             mode=mode or "always_on",
                             order=order if order is not None else 99,
                             enabled=True if enabled is None else enabled,
                             version=1))
                new_version = 1
            await s.commit()
        self._cache.pop(_ASSEMBLED_KEY, None)  # 装配缓存 stale
        self._cache.pop(scene, None)            # 单 skill raw 缓存 stale
        log.info("skill 更新 scene=%s version=%s tools=%s mode=%s enabled=%s",
                 scene, new_version, tools, mode, enabled)
        return new_version

    async def delete(self, scene: str) -> bool:
        async with AsyncSessionFactory() as s:
            row = await s.get(Prompt, scene)
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
        self._cache.pop(scene, None)
        self._cache.pop(_ASSEMBLED_KEY, None)  # 装配缓存 stale
        return True

    async def list_all(self) -> list[dict]:
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(Prompt.__table__.select().order_by(Prompt.scene))).all()
        return [{"scene": r.scene, "content": r.content, "tools": r.tools,
                 "mode": r.mode, "order": r.order,
                 "version": r.version, "enabled": r.enabled} for r in rows]

    async def list_active_skills(self) -> list[dict]:
        """DB 唯一真相源：返回所有 enabled=True 且 mode=always_on 的 skill 行（按 order 排序）。
        orchestrator 装配 system prompt、catalog 装配工具都调它——单一数据源，不再回退 md。"""
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(
                Prompt.__table__.select()
                .where(Prompt.enabled.is_(True))
                .where(Prompt.mode == "always_on")
                .order_by(Prompt.order)
            )).all()
        return [{"scene": r.scene, "content": r.content, "tools": r.tools,
                 "order": r.order} for r in rows]

    async def assemble_system_prompt(self) -> str:
        """组装 system prompt：内核协议 + 所有 enabled 的 always_on skill（按 order 顺序拼接）。
        DB 唯一真相源——直接遍历 list_active_skills()。
        orchestrator 每轮调，走 _ASSEMBLED_KEY 缓存（miss 才查库）；upsert/delete 失效。
        顺序固定 → 内核+skills 构成稳定缓存前缀，日期等可变项由 orchestrator 追加在尾部。"""
        cached = self._cache.get(_ASSEMBLED_KEY)
        if cached is not None:
            return cached[0]
        parts = [KERNEL_PROMPT]
        for sk in await self.list_active_skills():
            if sk["content"]:
                parts.append(sk["content"])
        assembled = "\n\n".join(parts)
        self._cache[_ASSEMBLED_KEY] = (assembled, 0)
        return assembled

    async def refresh(self) -> None:
        scenes = list(self._cache.keys())
        self._cache.clear()
        for sc in scenes:
            await self.get(sc)
