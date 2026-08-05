"""get_sql_template 工具：按名取 SQL 模板详情（usage + SQL + 参数），供 LLM 写复杂查询时套用。

替代「全 enabled 模板全量塞 system_prompt 的【SQL样板】段」——裸 SQL 塞 prompt 占上下文且易被忽略。
模板清单拼进工具 description（启动时从 DB 读，main.py lifespan 调 build_template_desc），
LLM 看 tools schema 即知有哪些模板；选定后传 template_name 取完整详情。

热更新边界：handler 每次 list_enabled_templates 实时查库 → 模板内容（改 SQL/usage）即时生效；
工具 description 里的清单摘要是启动快照，新增/改名模板后需重启才刷到 schema（内容仍实时）。"""
from __future__ import annotations

import json

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.storage.models import SqlTemplate
from src.storage.pg_client import AsyncSessionFactory

_GET_SQL_TEMPLATE_PARAMS = {
    "type": "object",
    "properties": {"template_name": {"type": "string",
                                     "description": "要取的模板名（见本工具说明里的现有模板清单）"}},
    "required": ["template_name"],
}


async def list_enabled_templates() -> list:
    """读所有 enabled 模板（main.py 拼 description、handler 取详情，共用此入口）。"""
    async with AsyncSessionFactory() as s:
        return (await s.execute(SqlTemplate.__table__.select().where(
            SqlTemplate.enabled.is_(True)))).all()


async def get_sql_template(args: dict, ctx: LoopContext,
                           cancel_token: CancelToken) -> ToolResult:
    """工具 handler。args: {template_name}。返回该模板的 usage + SQL。
    一次读全表（模板表极小），Python 里按名筛；找不到用同一批行拼现有模板名兜底，不抛异常。"""
    name = (args.get("template_name") or "").strip()
    if not name:
        return ToolResult(summary="错误：未提供 template_name。")
    rows = await list_enabled_templates()
    row = next((r for r in rows if r.name == name), None)
    if row is None:
        hint = "、".join(r.name for r in rows) if rows else "（暂无）"
        return ToolResult(summary=f"无名为「{name}」的模板。现有模板：{hint}")
    detail = {"name": row.name, "usage": row.usage or "",
              "sql_template": row.sql_template}
    return ToolResult(summary=json.dumps(detail, ensure_ascii=False, default=str))


def build_template_desc(tpls) -> str:
    """把 enabled 模板拼成工具 description。每行「- name：usage首句」。
    无模板时返回引导 LLM 不必强用的兜底文案。"""
    if not tpls:
        return ("查复杂查询的现成 SQL 样板。当前无可用模板；"
                "无合适样板时直接用 execute_sql 自行编写只读查询。")
    lines = []
    for t in tpls:
        # usage 首句/首行作清单摘要，避免 description 过长
        usage = (t.usage or "").strip()
        first = usage.split("\n")[0].split("。")[0] if usage else "（无说明）"
        lines.append(f"- {t.name}：{first}")
    return ("查复杂查询的现成 SQL 样板。遇到以下场景【必须先调本工具取样板，不要自己从零写】：\n"
            "- 多指标里找最值/排名/对比（宽表每个指标一列，如『哪个指标最差/最好』）→ 宽表列转行(unpivot)\n"
            "- 两期对比/环比/综合分变化/排名升降 → 环比对比+指标拆解\n"
            "- 本期有上期没有（或退出）→ 两期差集\n"
            "- 全局最优下钻到明细 TopN → 跨层级钻取\n"
            "现有模板：\n"
            + "\n".join(lines)
            + "\n调时传 template_name 取该模板的完整 SQL + 参数 + 改造说明，"
              "按 usage 改表名/参数后用 execute_sql 执行。一条样板能搞定的别拆成多条 SELECT。")


def make_get_sql_template(desc: str | None = None) -> ToolDefinition:
    """构造 get_sql_template 工具。desc 为拼好的模板清单；缺省用空模板兜底文案（统一一处）。"""
    return ToolDefinition(
        name="get_sql_template",
        description=desc or build_template_desc([]),
        parameters=_GET_SQL_TEMPLATE_PARAMS,
        handler=get_sql_template,
    )
