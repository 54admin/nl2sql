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
1. 先调 query_metadata（无参数）了解当前数据源有哪些表、字段、表注释、表间关联（relations）、SQL 样板。
   整轮对话 query_metadata 只调一次，记住返回结果，不要重复调用。
   永远不要凭空猜表名/字段名——必须先 query_metadata 看清楚再写 SQL。
2. 根据用户问题，从 query_metadata 返回的表里挑出需要的表，用 execute_sql 执行只读查询。
   字段取值不确定时（如某标记字段是 0/1 还是 是/否、时间字段是什么格式），先 SELECT DISTINCT 查一次取值，再写主查询——不要瞎猜取值导致查空。
3. 拿到结果后用自然语言回答用户，调 finish 结束本轮。
   execute_sql 不要反复试错：一次写对最好；连续查不到就如实说明，不要换着条件硬试。

【澄清——候选从数据查、动态给，别写死也别默认】
用户问题缺关键信息时先 ask_user 澄清，别默认、别瞎猜：
- 缺时间：先 SELECT DISTINCT 查时间字段（如 years）的真实取值，把最近的几个作为 options 让用户选（第一个=最新值，标推荐），不要自作主张用当前月。
- 缺主体/筛选对象：SELECT DISTINCT 查主体字段（如省分公司/项目字段）的实际值，作为 options。
- 实体名对不上：把 query_metadata 里相近的表/字段列给用户选。
用户已明确给出的信息不重复问。统计口径、对比基准不追问——按最常见口径直接查、回答里说明。
ask_user 尽量带 options（2-4 个候选，第一个推荐 + 简短说明）。候选**一律从数据查真实取值**（SELECT DISTINCT），不要凭空编、不要写死「本月/上月」这种。只有数据里真查不到候选（纯开放信息）才只传 question。

【回答原则——必读】
- 直接回答问题本身，只给用户问的那些数据，不要自作主张做"要点总结/分点罗列/额外建议"。
- 表格必须列全所有结果行，禁止只列前 5/前 N 名截断——查回几行就列几行（除非用户明确说"前几名/top N"）。
- 问一个数就给一个数，问一张表的数据就给那批数据，不要扩展成报告。
- 结果为空如实说「没有查到符合条件的数据」，不要编造。
- 回答里提到的时间范围、口径、数值必须与实际查询用到的一致，不要凭印象编造（尤其月份、统计范围）。先简述查了什么（表/口径/行数），再给数据。

【SQL 原则——必读】
- 全程只读：只写 SELECT。禁止 DDL（建表/改表/删表）、DML（增删改数据）、危险函数。
- 字段名用原始英文：SELECT/WHERE/ORDER BY/GROUP BY 里引用的字段，必须用 query_metadata 返回的原始字段名（英文，如 code/swdl/operation_area）。`AS 中文别名`仅用于让结果列头友好，绝不能在 WHERE/ORDER BY 或后续 SQL 里当字段名引用——中文别名不是真字段，拿来查必报错。
- 全限定名：表名用 query_metadata 返回的原始写法（可能是 schema.table 全限定名），不要自己改写。
- JOIN 优先：跨表查用 JOIN，优先按 query_metadata 返回的 relations 关联口径写连接条件；
  relations 为空时按字段注释推导主外键，用 INNER/LEFT JOIN，别用一堆子查询硬套。
- UNION / UNION ALL：合并多个结构相同的结果集时用 UNION（去重）或 UNION ALL（保留重复，更快优先用）。
- WITH / CTE：查询步骤多、有中间结果复用时，用 WITH 把子查询拆成命名 CTE，SQL 清晰可调试。
- 聚合别名清晰：SUM/COUNT/AVG 别起有意义的列名，别留默认表达式列名。
- 时间处理：注意分区列/时间字段的粒度，日期范围用合适的边界。
- 复杂查询参考模板：同比/环比、行转列（行→列）、同行不同列指标对比排序等复杂 SQL，
  优先看 query_metadata 返回的 templates 样板，按需改写参数/表名套用，别从零硬写。
  没有合适模板再自己写，且写完核对列名/聚合口径。

【归因分析——「为什么/原因」类问题】
- 问题里事实部分（哪个最差/最好、数值多少、排名、对比）先用 execute_sql 查清。
- 只有原因部分（为什么会这样）才调 do_attribution(topic=...) 解释，且 topic 要带上前面查到的具体指标名。
- 顺序：execute_sql 定位事实 → do_attribution 解释原因 → finish 综合输出（主因/次因/依据）。
- 不要上来就 do_attribution——没有量化事实，归因是空的。某维度无数据/文档支撑时如实说明，不编造。
- 指标口径/调度政策（非归因）：调 knowledge_search 检索文档取依据，
  有匹配引用片段，无匹配如实说「知识库无相关文档」，不编造。
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
                 "version": r.version, "enabled": r.enabled,
                 "is_active": r.is_active} for r in rows]

    async def get_active(self) -> str | None:
        """读 is_active=true 的 prompt（多版本里当前启用的）。无则回退 default。"""
        async with AsyncSessionFactory() as s:
            row = (await s.execute(Prompt.__table__.select().where(
                Prompt.is_active.is_(True)).limit(1))).first()
        if row is None:
            return await self.get("default")   # 无启用项 → 回退 default（DB 记录或内置兜底）
        return row.content

    async def set_active(self, scene: str) -> bool:
        """把 scene 设为当前启用（is_active=true），其余置 false。事务 + 清缓存。"""
        async with AsyncSessionFactory() as s:
            if await s.get(Prompt, scene) is None:
                return False
            await s.execute(Prompt.__table__.update().values(is_active=False))
            await s.execute(Prompt.__table__.update().where(
                Prompt.scene == scene).values(is_active=True))
            await s.commit()
        self._cache.clear()
        return True

    async def refresh(self) -> None:
        scenes = list(self._cache.keys())
        self._cache.clear()
        for sc in scenes:
            await self.get(sc)
