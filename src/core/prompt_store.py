"""场景化 prompt 存储 + 内存缓存（页面配置模型基础）。
orchestrator 组装 system message 时按 scene 读，默认场景 'default'。
ponytail: 单进程内存缓存；跨进程广播 P5 改 Redis pub/sub。"""
from __future__ import annotations

from src.logging import get_logger
from src.storage.models import Prompt
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)

DEFAULT_SCENE = "default"

# default 场景未在后台配置时的内置兜底：教 LLM 走「query_metadata 选表 → execute_sql 查数」两步链路。
# 后台配置了 default（enabled=True）会覆盖此兜底；attribution/correction 等非 default 场景不兜底（P3 再配）。
DEFAULT_PROMPT = """你是 NL2SQL 问数助手。用户用自然语言问业务数据，你查只读数据库后用中文回答。

【工作流程——每次必须遵守】
1. 先调 query_metadata（无参数）了解当前数据源有哪些表、字段、表注释，以及已配置的表间关联（relations）。
   永远不要凭空猜表名/字段名——必须先 query_metadata 看清楚再写 SQL。
2. 根据用户问题，从 query_metadata 返回的表里挑出需要的表，用 execute_sql 执行只读查询。
3. 拿到结果后用自然语言回答用户，必要时调 finish 结束本轮。

【澄清——缺关键信息必须先问再查】
以下情况不要硬查，先用 ask_user 问清楚用户再继续：
- 缺时间范围（如问"发电量"没说哪个月/哪年）—— 问"哪个时间范围？"
- 缺统计口径（如问"排名"没说按什么排、排什么）—— 问"按哪个指标、给什么排名？"
- 缺主体范围（如问"分公司情况"没说哪个/哪些分公司）—— 问"哪个/哪些分公司？"
- 缺可对比的基准（如问"比上月增长"没说跟谁比、什么维度）—— 问清楚
- 表里找不到用户说的实体名 —— 把 query_metadata 里相近的表/字段列给用户选
原则：宁可多问一句把问题问清楚，也不要猜着查错数据。但用户已明确给出的信息不要重复问。
ask_user 可多次调用：第一轮问的不够，拿到回答后若仍缺关键信息可再问。

【回答原则——必读】
- 直接回答问题本身，只给用户问的那些数据，**不要自作主张做"要点总结/分点罗列/额外建议"**。
- 问一个数就给一个数，问一张表的数据就给那批数据，不要扩展成报告。
- 结果为空要如实说「没有查到符合条件的数据」，不要编造。
- 拿到数据后如实呈现：先简述查了什么（表/口径/行数），再给数据，不要在数据之外加冗余分析。

【SQL 原则——必读】
- 全程只读：只写 SELECT。禁止 DDL（建表/改表/删表）、DML（增删改数据）、危险函数。
- 全限定名：表名用 query_metadata 返回的原始写法（可能是 schema.table 全限定名），不要自己改写。
- JOIN 优先：跨表查用 JOIN，优先按 query_metadata 返回的 relations 关联口径写连接条件；
  relations 为空时按字段注释推导主外键，用 INNER/LEFT JOIN，别用一堆子查询硬套。
- UNION / UNION ALL：合并多个结构相同的结果集时用 UNION（去重）或 UNION ALL（保留重复，更快优先用）。
- WITH / CTE：查询步骤多、有中间结果复用时，用 WITH 把子查询拆成命名 CTE，SQL 清晰可调试。
- 聚合别名清晰：SUM/COUNT/AVG 别起有意义的列名，别留默认表达式列名。
- 时间处理：注意分区列/时间字段的粒度，日期范围用合适的边界。
"""


class PromptStore:
    """场景化 prompt：内存缓存 + PG 持久。
    get 内存缓存优先，miss 读 PG 回填；upsert 写 PG + bump version + 刷新缓存。
    default 场景「从未配置过」时回退内置 DEFAULT_PROMPT；但后台显式禁用(enabled=False)/删除后返回 None（管理员主动关闭引导）。"""

    def __init__(self) -> None:
        # value 存 None 表示「已查过 PG 确认无记录」，和「未查过」区分开
        self._cache: dict[str, tuple[str | None, int] | None] = {}

    async def get(self, scene: str = DEFAULT_SCENE) -> str | None:
        """读场景 prompt。PG 无记录返回 None；enabled=False 返回 None。
        default 场景额外兜底：PG 从未配置过该记录（被删/禁后 cache 标记，不再兜底）。"""
        cached = self._cache.get(scene)
        if cached is not None:
            # cache 命中：直接拿缓存值（可能是 None=管理员禁用/删除后的标记）
            return cached[0]
        async with AsyncSessionFactory() as s:
            row = await s.get(Prompt, scene)
            if row is None or not row.enabled:
                # default 场景：PG 无记录 → 兜底内置 prompt；有记录但禁用 → 不兜底(返 None)
                if scene == DEFAULT_SCENE and row is None:
                    value: str | None = DEFAULT_PROMPT
                else:
                    value = None
                # 缓存标记：default 禁用/删除后不再兜底（value=None 进缓存）
                self._cache[scene] = (value, 0)
                return value
            value = row.content
            self._cache[scene] = (value, row.version)
            return value

    async def upsert(self, scene: str, content: str,
                     enabled: bool = True) -> int:
        async with AsyncSessionFactory() as s:
            row = await s.get(Prompt, scene)
            if row:
                row.content = content
                row.enabled = enabled
                row.version += 1
                new_version = row.version
            else:
                s.add(Prompt(scene=scene, content=content,
                             enabled=enabled, version=1))
                new_version = 1
            await s.commit()
        # ponytail: disabled 时清缓存而非写入，让 get miss 走 PG 看到 enabled=False 返回 None；
        # 否则缓存命中会绕过 enabled 检查。
        if enabled:
            self._cache[scene] = (content, new_version)
        else:
            self._cache.pop(scene, None)
        log.info("prompt 更新 scene=%s version=%s enabled=%s",
                 scene, new_version, enabled)
        return new_version

    async def delete(self, scene: str) -> bool:
        async with AsyncSessionFactory() as s:
            row = await s.get(Prompt, scene)
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
        self._cache.pop(scene, None)
        return True

    async def list_all(self) -> list[dict]:
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(Prompt.__table__.select())).all()
        return [{"scene": r.scene, "content": r.content,
                 "version": r.version, "enabled": r.enabled} for r in rows]

    async def refresh(self) -> None:
        scenes = list(self._cache.keys())
        self._cache.clear()
        for sc in scenes:
            await self.get(sc)
