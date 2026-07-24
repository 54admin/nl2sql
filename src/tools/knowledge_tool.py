"""knowledge_search 工具（P3b）：语义检索知识库文档片段，供归因/答疑作依据。
归因（为什么下降/异常）、指标口径、调度政策类问题调它取文档依据。"""
from __future__ import annotations

import json

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.knowledge.store import get_knowledge_store


async def knowledge_search(args: dict, ctx: LoopContext,
                           cancel_token: CancelToken) -> ToolResult:
    """语义检索知识库：query → embedding → 近邻 top-k 文档片段。"""
    query = args.get("query")
    if not query:
        return ToolResult(summary="缺少检索 query")
    k = int(args.get("top_k") or 5)
    try:
        rows = await get_knowledge_store().search(query, k)
    except RuntimeError as e:
        return ToolResult(summary=f"知识库检索失败：{e}")
    if not rows:
        return ToolResult(summary="知识库无匹配文档片段（可能未上传文档或未启用）。")
    # content 截断 800 字防撑爆 prompt
    hits = [{"content": r["content"][:800], "doc_id": r["doc_id"]} for r in rows]
    return ToolResult(summary=json.dumps({"hits": hits}, ensure_ascii=False))


KNOWLEDGE_SEARCH = ToolDefinition(
    name="knowledge_search",
    description=("语义检索知识库文档（运维手册/调度政策/历史复盘/指标口径）。"
                 "归因（为什么下降/异常原因/波动归因）、指标口径、政策类问题用它取文档依据。"
                 "输入检索 query（自然语言），返回最相关的文档片段。"),
    parameters={"type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索内容（自然语言描述）"},
                    "top_k": {"type": "integer", "description": "返回片段数（默认5）"}},
                "required": ["query"]},
    handler=knowledge_search,
)
