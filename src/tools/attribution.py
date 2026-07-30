"""do_attribution 归因工具（P3a，需求 5）：解释「为什么」——对比/异动/原因类提问。
调 knowledge_search 取文档依据 → 调归因模型（attribution 配置）整合成「主因/次因/依据」
结构化结论。归因推理跑在 attribution 模型上，和分析模型分开。归因模型未配/失败则回退框架。
单 Agent loop 内完成，不引入子 Agent（spec 9.2）。"""
from __future__ import annotations

import re

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.knowledge.store import get_knowledge_store
from src.logging import get_logger

log = get_logger(__name__)


def _refine_query(topic: str) -> str:
    """提炼归因主题成检索友好的 query：去年月/数值/百分比/得分等噪声，留实体+指标+归因语义。
    topic 常是「2026年6月新疆提质增效-增发电量得分1.515874表现差」——直接做向量检索，
    数值/时间会稀释语义、召回跑偏；提炼成「新疆 提质增效-增发电量 表现差」语义更聚焦。"""
    q = re.sub(r"\d{4}\s*年|\d{1,2}\s*月|\d+\.?\d*\s*%?|得分|分值|约为|约", " ", topic)
    q = re.sub(r"\s+", " ", q).strip()
    return q or topic


async def do_attribution(args: dict, ctx: LoopContext,
                         cancel_token: CancelToken) -> ToolResult:
    """归因分析：知识库文档依据 + 归因模型整合主因/次因/依据。"""
    topic = args.get("topic")
    if not topic:
        return ToolResult(summary="缺少归因主题 topic")
    # 1. 知识库查归因相关文档依据（topic 提炼成检索 query，去数值/时间噪声）
    search_query = _refine_query(topic)
    try:
        docs = await get_knowledge_store().search(search_query, k=5)
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
    description=("归因分析：解释「为什么」（发电量为什么下降/异动原因/对比差异）。"
                 "先用 execute_sql 定位具体异常指标/数值，再调本工具解释原因；"
                 "topic 带上查到的指标名。内部已检索知识库依据并调归因模型整合主因/次因/参考依据。"),
    parameters={"type": "object",
                "properties": {"topic": {"type": "string",
                                         "description": "归因主题。提炼成检索友好的表述：去掉具体年月/分数/数值，保留省分公司+指标名+归因语义。如「新疆 提质增效-增发电量 偏低 原因」，而非「2026年6月新疆增发电量得分1.515874为什么差」"}},
                "required": ["topic"]},
    handler=do_attribution,
)
