"""knowledge_search 工具：调外部 RAGFlow 检索知识库文档片段，供答疑/归因作依据。

知识库统一挪到外部 RAGFlow：检索转发 RAGFlow retrieval API，本系统不再做 embedding/向量库。
同一会话内 agent 自主决定：数据类问题用 execute_sql；知识/政策/口径/手册类问题用本工具
取文档片段后直接整合回答——即"问知识库"能力。
"""
from __future__ import annotations

import json

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.ragflow.client import get_ragflow_client


async def knowledge_search(args: dict, ctx: LoopContext,
                           cancel_token: CancelToken) -> ToolResult:
    """检索知识库：query → RAGFlow retrieval → top-k 文档片段（带来源文档名+相似度）。
    RAGFlow 负责分段/向量/混合检索；本系统拿到片段后由 LLM 整合回答。"""
    query = args.get("query")
    if not query:
        return ToolResult(summary="缺少检索 query")
    top_k = int(args.get("top_k") or 5)
    try:
        rows = await get_ragflow_client().retrieve(query, top_k=top_k)
    except Exception as e:
        return ToolResult(summary=f"知识库检索失败：{e}")
    if not rows:
        return ToolResult(summary="知识库未配置或无匹配文档片段。"
                                  "（RAGFlow 未启用/未勾选知识库，或确实无相关文档）")
    # content 截断 800 字防撑爆 prompt；带文档名来源 + 相似度，便于 LLM 判断依据可信度
    hits = [{
        "content": (r["content"] or "")[:800],
        "document": r.get("document", ""),
        "similarity": round(r.get("similarity", 0.0), 3),
    } for r in rows]
    return ToolResult(summary=json.dumps({"hits": hits}, ensure_ascii=False))


KNOWLEDGE_SEARCH = ToolDefinition(
    name="knowledge_search",
    description=("检索知识库文档（运维手册/调度政策/历史复盘/指标口径/规章制度/操作指南等）。"
                 "凡是查文档、查政策、查口径、查说明、查规定类的问题，用本工具取文档片段后整合回答，"
                 "不要去 execute_sql 查数（那是查业务数据的）。"
                 "归因（为什么下降/异常原因/波动归因）也用它取文档依据。"
                 "输入检索 query：提炼成检索友好的关键词（去掉具体年月/数值/分数，保留实体+指标+语义），"
                 "返回最相关的文档片段（带来源文档名+相似度）。"
                 "拿到片段后直接基于片段内容回答用户；片段不足/无匹配就如实说明，不编造。"),
    parameters={"type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索内容。提炼成检索友好的关键词：去掉具体年月/数值/分数，保留实体+指标+归因语义；不要直接用用户原话（数值会稀释召回）"},
                    "top_k": {"type": "integer", "description": "返回片段数（默认5）"}},
                "required": ["query"]},
    handler=knowledge_search,
)
