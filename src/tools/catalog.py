"""工具目录（toolbox）：所有工具定义的单一登记入口，按名取用。

设计：工具是共享积木（execute_sql 被 nl2sql 与 attribution 共用），不能塞进单个 skill 目录；
正确模型——
  * 工具 = 扁平工具箱（本文件按名登记所有 ToolDefinition，新增工具只在此处登记一处）；
  * skill = DB prompts 表一行（tools 字段声明依赖哪些工具）；seed 由 scripts/gen_seed.py 的 SEED_SKILLS 常量生成；
  * 装配 = 取所有 enabled 的 always_on skill 声明的工具做并集，加 KERNEL_TOOL_NAMES，只注册这些
          （防 LLM 幻觉调隐藏工具；关 skill → 其工具跟着摘，与提示词开关同一份 enabled 联动）。

get_sql_template 的 description 需启动时注入模板清单（DB 实时），故经 build_catalog 入参传入。
"""
from __future__ import annotations

# 运行时从 DB 读 skill（PromptStore.list_active_skills）
from src.core.types import ToolDefinition
from src.logging import get_logger
from src.tools.builtins import ASK_USER, FINISH
from src.tools.knowledge_tool import KNOWLEDGE_SEARCH
from src.tools.metadata import QUERY_METADATA
from src.tools.registry import ToolRegistry
from src.tools.sql_engine import EXECUTE_SQL
from src.tools.sql_template import make_get_sql_template

log = get_logger(__name__)

# 内核控制流工具：结束本轮 / 澄清挂起。不属任何 skill，所有会话恒启用——
# 它们是 ReAct loop 终止与挂起的唯一出口（agent_loop 观察标志位后动作）。
KERNEL_TOOL_NAMES = ("finish", "ask_user")


def build_catalog(sql_template_desc: str | None = None) -> dict[str, ToolDefinition]:
    """全量工具 {name: ToolDefinition}。新增工具在此登记一处，即可被 skill frontmatter 引用。"""
    catalog: dict[str, ToolDefinition] = {}
    for td in (FINISH, ASK_USER, QUERY_METADATA, EXECUTE_SQL, KNOWLEDGE_SEARCH):
        catalog[td.name] = td
    catalog["get_sql_template"] = make_get_sql_template(sql_template_desc)
    return catalog


def resolve_active_tool_names(active_skills: list[dict]) -> list[str]:
    """运行时应注册的工具名（去重保序）：
    内核控制流 ∪ 所有 enabled 的 always_on skill 声明的 tools（来自 DB）。
    active_skills 已是 PromptStore.list_active_skills() 的结果（enabled+always_on 已过滤）。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for name in KERNEL_TOOL_NAMES:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    for sk in active_skills:  # 已按 order 排好序
        for t in (sk.get("tools") or []):
            if t not in seen:
                seen.add(t)
                ordered.append(t)
    return ordered


async def build_registry(sql_template_desc: str | None = None,
                      prompt_store=None,
                      active_skills: list[dict] | None = None) -> ToolRegistry:
    """装配运行时工具集（async，读 DB）：catalog 取定义 + active 工具名并集 → ToolRegistry。
    active_skills 不传则从 prompt_store.list_active_skills() 现查 DB（DB 单一真相源）。
    热刷新时 prompt_store 传 None、active_skills 传已查好的列表，避免重复查库。
    skill 声明了 catalog 里没有的名字会告警跳过（防拼错/改名的工具漏网）。"""
    from src.core.prompt_store import PromptStore  # 延迟 import 避免循环
    store = prompt_store or PromptStore()
    if active_skills is None:
        active_skills = await store.list_active_skills()
    catalog = build_catalog(sql_template_desc)
    active = resolve_active_tool_names(active_skills)
    reg = ToolRegistry()
    for name in active:
        td = catalog.get(name)
        if td is None:
            log.warning("skill 声明了未知工具 %r，跳过", name)
            continue
        reg.register(td)
    log.info("工具装配完成 active=%s", active)
    return reg
