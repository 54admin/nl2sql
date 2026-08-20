"""knowledge_search 工具：调外部 RAGFlow 检索知识库文档片段，供答疑/查政策/查口径作依据。

知识库统一挪到外部 RAGFlow：检索转发 RAGFlow retrieval API，本系统不再做 embedding/向量库。
同一会话内 agent 自主决定：数据类问题用 execute_sql；知识/政策/口径/手册类问题用本工具
取文档片段后直接整合回答——即"问知识库"能力。
"""
from __future__ import annotations

import json
import os

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.ragflow.client import get_ragflow_client


# RAGFlow v0.26.x 文档预览页 URL 模板：路由 /document/:id（路径参数 = document_id），
# ext(query) 决定预览器（pdf/docx/xlsx/md/...），不需要 dataset_id。
# 已按 v0.26.4 前端路由 web/src/routes.tsx 校准；RAGFlow 版本升级后需复核。
DOC_PREVIEW_PATH = "/document/{document_id}"


def _ext_from_name(name: str) -> str:
    """从文档名提取扩展名（小写、去前导点）。RAGFlow document_keyword 即文件名。"""
    _, ext = os.path.splitext(name or "")
    return ext.lstrip(".").lower()


def build_doc_url(base_url: str, document_id: str, document_name: str = "") -> str:
    """拼 RAGFlow 文档预览页跳转链接：{base}/document/{document_id}?ext={扩展名}。
    ext 从文档名提取，取不到则不带 query（页面可能空白，但 URL 可见可调）。
    base_url/document_id 任一缺失返回 ""（前端降级为不可点）。"""
    base = (base_url or "").strip().rstrip("/")
    if not base or not document_id:
        return ""
    path = DOC_PREVIEW_PATH.format(document_id=document_id)
    ext = _ext_from_name(document_name)
    return f"{base}{path}?ext={ext}" if ext else f"{base}{path}"


SNIPPET_LIMIT = 600   # 参考来源片段预览字数上限（防 done 事件/飞书卡片过大）


def _build_references(rows: list[dict], base_url: str) -> list[dict]:
    """把检索片段聚合成文档级引用（同一文档多片段只留相似度最高的一条），带跳转 URL + 命中片段。
    按 similarity 降序。供 ToolResult.references，done 时再聚合成 citations。
    content：该文档最高相似度片段原文（截断 SNIPPET_LIMIT），供前端展开查看——不依赖 RAGFlow 登录态。"""
    best: dict[str, dict] = {}
    for r in rows:
        doc = r.get("document", "")
        if not doc:
            continue
        sim = r.get("similarity", 0.0)
        prev = best.get(doc)
        if prev is None or sim > prev.get("similarity", 0.0):
            raw = r.get("content") or ""
            best[doc] = {
                "document": doc,
                "similarity": round(sim, 3),
                "document_id": r.get("document_id", ""),
                "dataset_id": r.get("dataset_id", ""),
                "url": build_doc_url(base_url, r.get("document_id", ""),
                                     r.get("document", "")),
                "content": raw[:SNIPPET_LIMIT] + ("…" if len(raw) > SNIPPET_LIMIT else ""),
            }
    return sorted(best.values(), key=lambda x: x["similarity"], reverse=True)


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
    # references：结构化引用来源（文档级去重 + 跳转 URL），不回灌 LLM，只供 done 聚合后两端渲染
    base_url = ""
    try:
        cfg = await get_ragflow_client().load_config()
        base_url = (cfg.base_url if cfg else "")
    except Exception:
        base_url = ""
    references = _build_references(rows, base_url)
    return ToolResult(summary=json.dumps({"hits": hits}, ensure_ascii=False),
                      references=references)


KNOWLEDGE_SEARCH = ToolDefinition(
    name="knowledge_search",
    description=("【查文档，不是查数据】用户要「某份文件/资料里写了什么」时用本工具：规章制度、管理办法、"
                 "操作手册、技术标准、指标口径、移交资料、验收文档、缺陷清单、会议纪要、复盘材料等。"
                 "判别：问题含 制度/规定/办法/标准/手册/流程/口径/移交/验收/纪要/资料/怎么规定的 等词，"
                 "或问某个具体事项（如『X 移交生产的缺陷』）→ 用本工具，"
                 "不要去 execute_sql 拿实体名模糊匹配。"
                 "\n"
                 "用法：query 提炼成检索关键词（保留实体+事项语义，去掉具体年月/数值/分数）；"
                 "返回最相关文档片段（带来源文档名+相似度）；基于片段回答，无匹配就如实说明。"
                 "\n"
                 "不适用：指标数值波动的【归因】走归因素材表，不查本工具。"),
    parameters={"type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索内容。提炼成检索友好的关键词：去掉具体年月/数值/分数，保留实体+指标+归因语义；不要直接用用户原话（数值会稀释召回）"},
                    "top_k": {"type": "integer", "description": "返回片段数（默认5）"}},
                "required": ["query"]},
    handler=knowledge_search,
)
