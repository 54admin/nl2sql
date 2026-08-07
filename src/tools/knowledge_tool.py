"""knowledge_search 工具：调外部 RAGFlow 检索知识库文档片段，供答疑/查政策/查口径作依据。

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
        return ToolResult(summary="缺少检索 query。如非必要不要重试 knowledge_search。")
    top_k = int(args.get("top_k") or 5)
    try:
        rows = await get_ragflow_client().retrieve(query, top_k=top_k)
    except Exception as e:
        return ToolResult(summary=f"知识库检索失败：{e}")
    if not rows:
        return ToolResult(summary="知识库未配置或无匹配文档片段。"
                                  "（RAGFlow 未启用/未勾选知识库，或确实无相关文档）。"
                                  "不要再调 knowledge_search（换 query 也是空）；归因/答疑直接基于已有数据。")
    # content 截断 800 字防撑爆 prompt；带文档名来源 + 相似度，便于 LLM 判断依据可信度
    hits = [{
        "content": (r["content"] or "")[:800],
        "document": r.get("document", ""),
        "similarity": round(r.get("similarity", 0.0), 3),
    } for r in rows]
    return ToolResult(summary=json.dumps({"hits": hits}, ensure_ascii=False))


KNOWLEDGE_SEARCH = ToolDefinition(
    name="knowledge_search",
    description=("【查文档/资料/制度/口径——不是查数据】用户要的是「某份文件/资料里写了什么」时用本工具："
                 "规章制度、管理办法、操作手册、技术标准、指标口径、项目移交资料、验收文档、缺陷清单、"
                 "会议纪要、复盘材料、培训资料、采购规范等。"
                 "判别信号：问题含 制度/规定/办法/标准/手册/流程/口径/移交/验收/缺陷清单/纪要/复盘/资料/"
                 "怎么规定的 等词，或问某个具体事项（如『X 移交生产的缺陷』『运维手册怎么规定的』）"
                 "→ 这是查文档，用本工具，不要去 execute_sql 拿实体名模糊匹配。"
                 "\n"
                 "用法：query 提炼成检索关键词（保留实体+事项语义，去掉具体年月/数值/分数）；"
                 "返回最相关文档片段（带来源文档名+相似度）；基于片段回答，无匹配就如实说明。"
                 "\n"
                 "不适用：指标数值波动的【归因】走数据表 cause_text 列，不查本工具。"),
    parameters={"type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索内容。提炼成检索友好的关键词：去掉具体年月/数值/分数，保留实体+指标+归因语义；不要直接用用户原话（数值会稀释召回）"},
                    "top_k": {"type": "integer", "description": "返回片段数（默认5）"}},
                "required": ["query"]},
    handler=knowledge_search,
)
