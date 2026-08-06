"""RAGFlow 外部知识库客户端：文档管理 + 向量检索全转发 RAGFlow HTTP API（/api/v1）。

知识库统一挪到外部 RAGFlow：文档上传/解析/分段/embedding 全由 RAGFlow 做，
本系统只负责「取配置 → 调 RAGFlow → 给上层统一数据结构」。问答（基于检索片段生成答案）
仍走本系统 LLMService，不依赖 RAGFlow 的 chat 能力——复用本系统双协议+限流+SSE+审计。

API 规范（RAGFlow 官方 docs/references/http_api_reference.md）：
  认证: Authorization: Bearer <api_key>   base: {base_url}/api/v1
  GET  /datasets?include_parsing_status=true          → {code:0, data:[{id,name,document_count,chunk_count,...}], total_datasets}
  POST /datasets/{id}/documents (multipart file=@)    → {code:0, data:[{id,name,run:"UNSTART",...}]}
  POST /datasets/{id}/chunks  body{document_ids}      → 触发解析 {code:0}
  GET  /datasets/{id}/documents?page=&page_size=      → {code:0, data:[{id,name,run,chunk_count,...}]}
  DELETE /datasets/{id}/documents body{ids}           → {code:0}
  POST /retrieval body{question,dataset_ids,top_k,...} → {code:0, data:{chunks:[{content,document_keyword,similarity,...}], total}}

配置走数据库 ragflow_config 表（热更新，同 feishu_config 套路）：
  base_url / api_key / dataset_ids / 检索参数 / enabled。
未配置或 enabled=false 时：load_config 返回 None，上层优雅降级（工具提示「知识库未配置」）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from src.logging import get_logger
from src.storage.models import RagflowConfigRow
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)

DEFAULT_TIMEOUT = 30.0   # RAGFlow 解析/检索超时（秒）

# ---------- httpx 连接池单例 ----------
# 复用 TCP 连接 + keep-alive，避免每次请求新建 client（原 _request/upload 每次都
# httpx.AsyncClient()，连接无法复用）。lifespan 关闭时 aclose（见 main.py）。
# 懒加载兜底：即便没走 lifespan 也能用（首次调用现建）。
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """进程级 httpx 连接池单例。"""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    return _http_client


async def close_http_client() -> None:
    """lifespan 关闭时调用，释放连接池。"""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


@dataclass
class RagflowConfig:
    """RAGFlow 运行配置（从 ragflow_config 表读出）。"""
    base_url: str = ""
    api_key: str = ""
    dataset_ids: list[str] = None
    top_k: int = 5
    similarity_threshold: float = 0.2
    vector_similarity_weight: float = 0.3
    enabled: bool = False

    def __post_init__(self):
        if self.dataset_ids is None:
            self.dataset_ids = []

    @property
    def ready(self) -> bool:
        """配置是否可用：启用 + 地址/密钥/至少一个 dataset 齐全。"""
        return (self.enabled and bool(self.base_url) and bool(self.api_key)
                and len(self.dataset_ids) > 0)

    @property
    def api_base(self) -> str:
        """拼 /api/v1 前缀。

        ⚠ 不能用 rstrip("/api/v1")：它按【字符集】{/,a,p,i,v,1} 剥尾，不是删子串——
        会把端口 9381 的末位 1、或 base 末尾的 a/p/i/v 一起啃掉（如 9381→938、/api→空）。
        改成按子串：摘掉 base 已带的 /api/v1 或 /v1 后缀，再统一补回 /api/v1。
        """
        b = (self.base_url or "").strip().rstrip("/")
        for suf in ("/api/v1", "/v1"):
            if b.endswith(suf):
                b = b[: -len(suf)]
                break
        return b + "/api/v1"


def _row_to_cfg(r) -> RagflowConfig:
    return RagflowConfig(
        base_url=(r.base_url or "").strip(),
        api_key=(r.api_key or "").strip(),
        dataset_ids=list(r.dataset_ids or []),
        top_k=r.top_k,
        similarity_threshold=r.similarity_threshold,
        vector_similarity_weight=r.vector_similarity_weight,
        enabled=bool(r.enabled),
    )


class RagflowError(RuntimeError):
    """RAGFlow 调用失败（含非 0 code / HTTP 错误 / 未配置）。"""


class RagflowClient:
    """RAGFlow HTTP 客户端。每次调用现读配置（admin 改完即生效，热更新，无需重启）。"""

    # ---------- 配置 ----------
    async def load_config(self) -> RagflowConfig | None:
        """读 ragflow_config 表（单行 default）。无记录/未配置返回 None。"""
        async with AsyncSessionFactory() as s:
            row = await s.get(RagflowConfigRow, "default")
        if row is None:
            return None
        cfg = _row_to_cfg(row)
        return cfg if cfg.enabled else None

    async def _require(self) -> RagflowConfig:
        """取配置并校验可用，不可用抛 RagflowError（上层捕获转友好提示）。"""
        cfg = await self.load_config()
        if cfg is None or not cfg.ready:
            raise RagflowError("RAGFlow 知识库未配置或未启用：请在管理后台「知识库」配置地址/API Key 并勾选知识库。")
        return cfg

    def _headers(self, cfg: RagflowConfig) -> dict:
        return {"Authorization": f"Bearer {cfg.api_key}"}

    # ---------- 底层请求 ----------
    async def _request(self, cfg: RagflowConfig, method: str, path: str, **kw) -> dict:
        url = f"{cfg.api_base}{path}"
        kw.setdefault("timeout", DEFAULT_TIMEOUT)
        try:
            resp = await get_http_client().request(
                method, url, headers=self._headers(cfg), **kw)
        except httpx.HTTPError as e:
            raise RagflowError(f"RAGFlow 连接失败：{e}") from e
        if resp.status_code >= 400:
            raise RagflowError(f"RAGFlow HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError as e:
            raise RagflowError(f"RAGFlow 响应非 JSON: {resp.text[:200]}") from e
        if body.get("code", 0) != 0:
            raise RagflowError(f"RAGFlow 错误 code={body.get('code')}: {body.get('message', '')[:200]}")
        return body

    # ---------- 知识库(dataset)管理 ----------
    async def list_datasets(self) -> list[dict]:
        """列出 RAGFlow 所有知识库（含解析状态计数）。admin 后台勾选用。"""
        cfg = await self._require()
        body = await self._request(
            cfg, "GET", "/datasets",
            params={"page": 1, "page_size": 200, "include_parsing_status": "true"})
        return body.get("data", []) or []

    async def create_dataset(self, name: str, description: str = "",
                             chunk_method: str = "naive") -> dict:
        """创建知识库。chunk_method 默认 naive（通用切块）。"""
        cfg = await self._require()
        body = await self._request(cfg, "POST", "/datasets", json={
            "name": name, "description": description, "chunk_method": chunk_method,
            "permission": "me"})
        return body.get("data", {}) or {}

    # ---------- 文档管理 ----------
    async def upload_document(self, dataset_id: str, filename: str,
                              content: bytes) -> list[dict]:
        """上传文档到指定知识库（multipart/form-data）。RAGFlow 自己解析，无需本系统分段。
        上传后需调 parse_documents 触发解析。返回上传结果（含 document_id）。"""
        cfg = await self._require()
        files = {"file": (filename, content)}
        # httpx multipart：headers 不要手动设 content-type，让它自动带 boundary
        url = f"{cfg.api_base}/datasets/{dataset_id}/documents"
        try:
            resp = await get_http_client().request(
                "POST", url, headers=self._headers(cfg),
                timeout=DEFAULT_TIMEOUT, files=files)
        except httpx.HTTPError as e:
            raise RagflowError(f"RAGFlow 上传失败：{e}") from e
        if resp.status_code >= 400:
            raise RagflowError(f"RAGFlow 上传 HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if body.get("code", 0) != 0:
            raise RagflowError(f"RAGFlow 上传错误: {body.get('message', '')[:200]}")
        return body.get("data", []) or []

    async def parse_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        """触发文档解析（分段+embedding）。上传后必须调，否则不可检索。"""
        cfg = await self._require()
        await self._request(cfg, "POST", f"/datasets/{dataset_id}/chunks",
                            json={"document_ids": document_ids})

    async def list_documents(self, dataset_id: str,
                             page: int = 1, page_size: int = 200) -> list[dict]:
        """列出某知识库的文档（含解析状态 run/chunk_count）。"""
        cfg = await self._require()
        body = await self._request(
            cfg, "GET", f"/datasets/{dataset_id}/documents",
            params={"page": page, "page_size": page_size})
        return body.get("data", []) or []

    async def delete_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        """删除文档（按 id）。"""
        cfg = await self._require()
        await self._request(cfg, "DELETE", f"/datasets/{dataset_id}/documents",
                            json={"ids": document_ids})

    # ---------- 检索（核心）----------
    async def retrieve(self, question: str, top_k: int | None = None,
                       dataset_ids: list[str] | None = None,
                       similarity_threshold: float | None = None,
                       vector_similarity_weight: float | None = None,
                       keyword: bool = True) -> list[dict]:
        """向量+关键词混合检索 RAGFlow。返回片段列表：
        [{content, document_keyword(文档名), similarity, document_id, dataset_id, highlight}, ...]
        按相似度降序。未配置/无结果返回 []（不抛，上层按空处理给友好提示）。"""
        cfg = await self.load_config()
        if cfg is None or not cfg.ready:
            return []   # 未配置：静默返回空，上层提示「知识库未配置」
        ds = dataset_ids or cfg.dataset_ids
        if not ds:
            return []
        payload = {
            "question": question,
            "dataset_ids": ds,
            "top_k": top_k if top_k is not None else cfg.top_k,
            "similarity_threshold": (similarity_threshold
                                     if similarity_threshold is not None else cfg.similarity_threshold),
            "vector_similarity_weight": (vector_similarity_weight
                                         if vector_similarity_weight is not None else cfg.vector_similarity_weight),
            "keyword": keyword,
            "page": 1,
            "page_size": top_k if top_k is not None else cfg.top_k,
        }
        try:
            body = await self._request(cfg, "POST", "/retrieval", json=payload)
        except RagflowError as e:
            log.warning("RAGFlow 检索失败（返回空）: %s", e)
            return []
        data = body.get("data", {}) or {}
        chunks = data.get("chunks", []) or []
        return [{
            "content": c.get("content", ""),
            "document": c.get("document_keyword", ""),
            "similarity": c.get("similarity", 0.0),
            "document_id": c.get("document_id", ""),
            "dataset_id": c.get("dataset_id", ""),
            "highlight": c.get("highlight", ""),
        } for c in chunks]


# 进程级单例（工具/路由共享同一客户端，配置现读热更新）
_client_singleton: RagflowClient | None = None


def get_ragflow_client() -> RagflowClient:
    """进程级单例。配置每次现读 ragflow_config 表（admin 改完即生效）。"""
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = RagflowClient()
    return _client_singleton
