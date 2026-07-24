"""do_attribution 归因工具（P3a，需求 5）：解释「为什么」——对比/异动/原因类提问。
内部调 knowledge_search 取文档依据 + 给出归因维度框架（检修/限电/气象/故障台账），
引导 LLM 用 execute_sql 查量化数据，按主次因分层整合。无依据时如实告知不编造。
单 Agent loop 内完成，不引入子 Agent（spec 9.2）。"""
from __future__ import annotations

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.knowledge.store import get_knowledge_store


async def do_attribution(args: dict, ctx: LoopContext,
                         cancel_token: CancelToken) -> ToolResult:
    """归因分析：查知识库文档依据 + 返回归因维度框架，引导 LLM 查结构化数据 + 按主次因整合。"""
    topic = args.get("topic")
    if not topic:
        return ToolResult(summary="缺少归因主题 topic")
    # 1. 知识库查归因相关文档依据
    try:
        docs = await get_knowledge_store().search(topic, k=5)
    except RuntimeError:
        docs = []
    if docs:
        doc_text = "\n".join(f"- {d['content'][:300]}" for d in docs)
    else:
        doc_text = "（知识库无相关文档）"
    # 2. 归因框架：引导查结构化维度 + 主次因分层（数据查询交给 execute_sql，不重复 NL2SQL 逻辑）
    framework = (
        f"【归因分析】主题：{topic}\n"
        "请按以下完成归因：\n"
        "1. 量化数据：先用 query_metadata 看有哪些表/字段，再用 execute_sql 查相关维度"
        "（检修工单 / 限电记录 / 气象 / 故障台账等，按主题相关性选）。\n"
        f"2. 文档依据（知识库已检索）：\n{doc_text}\n"
        "3. 整合输出，按主次因分层：\n"
        "   - 主因：有量化数据支撑的核心原因\n"
        "   - 次因：辅助因素\n"
        "   - 参考依据：文档/政策口径\n"
        "4. 若某维度无数据/无文档支撑，如实说明「无 X 数据支撑」，不要编造。"
    )
    return ToolResult(summary=framework)


ATTRIBUTION = ToolDefinition(
    name="do_attribution",
    description=("归因分析：解释「为什么」（如发电量为什么下降/异动原因/对比差异）。"
                 "用户问归因类问题时调用。内部已检索知识库文档依据并给出归因维度框架，"
                 "你再配合 execute_sql 查量化数据，按主因/次因/参考依据分层输出。"),
    parameters={"type": "object",
                "properties": {"topic": {"type": "string",
                                         "description": "归因主题（如「6月发电量下降」）"}},
                "required": ["topic"]},
    handler=do_attribution,
)
