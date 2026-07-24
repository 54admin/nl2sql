"""do_attribution 归因工具（P3a，需求 5）：解释「为什么」——对比/异动/原因类提问。
调 knowledge_search 取文档依据 → 调归因模型（attribution 配置）整合成「主因/次因/依据」
结构化结论。归因推理跑在 attribution 模型上，和分析模型分开。归因模型未配/失败则回退框架。
单 Agent loop 内完成，不引入子 Agent（spec 9.2）。"""
from __future__ import annotations

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.knowledge.store import get_knowledge_store
from src.logging import get_logger

log = get_logger(__name__)


async def do_attribution(args: dict, ctx: LoopContext,
                         cancel_token: CancelToken) -> ToolResult:
    """归因分析：知识库文档依据 + 归因模型整合主因/次因/依据。"""
    topic = args.get("topic")
    if not topic:
        return ToolResult(summary="缺少归因主题 topic")
    # 1. 知识库查归因相关文档依据
    try:
        docs = await get_knowledge_store().search(topic, k=5)
    except RuntimeError:
        docs = []
    doc_text = "\n".join(f"- {d['content'][:300]}" for d in docs) if docs else "（知识库无相关文档）"

    # 2. 调归因模型（attribution 配置）整合成主因/次因/依据；未配/失败回退框架
    prompt = (
        "你是电力经营归因分析专家。基于下列知识库文档依据，对该归因主题做因果分析，"
        "按「主因 / 次因 / 参考依据」分层输出。主因要有依据支撑，某维度无依据如实说明，不编造。\n\n"
        f"归因主题：{topic}\n\n文档依据：\n{doc_text}"
    )
    try:
        from src.llm.service import LLMService
        resp = await LLMService().chat([{"role": "user", "content": prompt}], purpose="attribution")
        analysis = (resp.content or "").strip()
    except Exception as e:
        log.warning("归因模型调用失败，回退框架（交 analysis 模型推理）: %s", e)
        analysis = ""

    if analysis:
        return ToolResult(summary=analysis)

    # 回退：归因模型未配/失败 → 返回框架 + 文档依据，agent_loop（analysis 模型）自行推理
    framework = (
        f"【归因分析】主题：{topic}\n"
        "（归因模型未启用，由你整合）请：\n"
        "1. 用 execute_sql 查量化维度（检修/限电/气象/故障台账，先 query_metadata 看表）。\n"
        f"2. 文档依据（知识库已检索）：\n{doc_text}\n"
        "3. 按主因/次因/参考依据分层输出；无依据维度如实说明，不编造。"
    )
    return ToolResult(summary=framework)


ATTRIBUTION = ToolDefinition(
    name="do_attribution",
    description=("归因分析：解释「为什么」（如发电量为什么下降/异动原因/对比差异）。"
                 "用户问归因类问题时调用。内部已检索知识库文档依据并调归因模型整合成"
                 "主因/次因/参考依据结论；你可再用 execute_sql 补量化数据。"),
    parameters={"type": "object",
                "properties": {"topic": {"type": "string",
                                         "description": "归因主题（如「6月发电量下降」）"}},
                "required": ["topic"]},
    handler=do_attribution,
)
