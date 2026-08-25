"""admin RAGFlow 知识库路由：配置 CRUD + 文档管理转发 RAGFlow。

对齐 admin_feishu 的动态配置范式：配置走数据库 ragflow_config 表（单行 default），
admin 后台改完即热生效（agent 每次 retrieve 现读配置，无需 reload/重启）。
文档列表/上传/删除/解析直接转发 RAGFlow HTTP API，本系统不存文档元数据。
ponytail: 鉴权层 P5 管理后台再补。"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from src.logging import get_logger
from src.ragflow.client import get_ragflow_client
from src.storage.models import RagflowConfigRow
from src.storage.db_client import AsyncSessionFactory

log = get_logger(__name__)
DEFAULT_ID = "default"


class RagflowConfigPayload(BaseModel):
    base_url: str = ""
    api_key: str = ""
    dataset_ids: list[str] = []
    top_k: int = 5
    similarity_threshold: float = 0.2
    vector_similarity_weight: float = 0.3
    enabled: bool = False


def _row_to_dict(r) -> dict:
    return {"id": r.id, "base_url": r.base_url, "api_key": r.api_key,
            "dataset_ids": r.dataset_ids or [], "top_k": r.top_k,
            "similarity_threshold": r.similarity_threshold,
            "vector_similarity_weight": r.vector_similarity_weight,
            "enabled": r.enabled, "version": r.version}


class RenameDatasetPayload(BaseModel):
    name: str
    description: str = ""


class CreateDatasetPayload(BaseModel):
    name: str
    description: str = ""
    chunk_method: str = "naive"   # naive=通用（默认）


class DocumentsEnabledPayload(BaseModel):
    dataset_id: str
    document_ids: list[str] | None = None   # None/空 = 整库
    enabled: bool


class ParsePayload(BaseModel):
    dataset_id: str
    document_ids: list[str]


def build_admin_ragflow_router() -> APIRouter:
    """构造 admin RAGFlow 路由。配置每次现读 ragflow_config 表（agent 检索也现读，天然热更新）。"""
    router = APIRouter()

    # ---------- 配置 CRUD ----------

    @router.get("/api/admin/ragflow-config")
    async def get_config() -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(RagflowConfigRow, DEFAULT_ID)
        return _row_to_dict(row) if row else {
            "id": DEFAULT_ID, "base_url": "", "api_key": "", "dataset_ids": [],
            "top_k": 5, "similarity_threshold": 0.2,
            "vector_similarity_weight": 0.3, "enabled": False, "version": 0}

    @router.put("/api/admin/ragflow-config")
    async def save_config(payload: RagflowConfigPayload) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(RagflowConfigRow, DEFAULT_ID)
            if row is None:
                row = RagflowConfigRow(id=DEFAULT_ID, version=0)
                s.add(row)
            row.base_url = payload.base_url.strip()
            row.api_key = payload.api_key.strip()
            row.dataset_ids = payload.dataset_ids
            row.top_k = payload.top_k
            row.similarity_threshold = payload.similarity_threshold
            row.vector_similarity_weight = payload.vector_similarity_weight
            row.enabled = payload.enabled
            row.version += 1
            await s.commit()
            version = row.version
        # 配置 agent 现读，无需 reload；下次 retrieve 自动用新配置
        return {"ok": True, "version": version}

    # ---------- 知识库(dataset) / 文档管理：转发 RAGFlow ----------

    @router.get("/api/admin/ragflow/datasets")
    async def list_datasets() -> dict:
        """列出 RAGFlow 所有知识库（含每库解析状态计数；检索默认全部库，无需勾选）。"""
        try:
            return {"datasets": await get_ragflow_client().list_datasets()}
        except Exception as e:
            raise HTTPException(400, f"RAGFlow 列知识库失败：{e}")


    @router.post("/api/admin/ragflow/datasets")
    async def create_dataset(payload: CreateDatasetPayload) -> dict:
        """新建知识库。新库即刻参与检索（默认全部库）。"""
        if not payload.name.strip():
            raise HTTPException(400, "库名不能为空")
        try:
            ds = await get_ragflow_client().create_dataset(
                payload.name.strip(), payload.description.strip(), payload.chunk_method)
        except Exception as e:
            raise HTTPException(400, f"RAGFlow 建库失败：{e}")
        return {"ok": True, "dataset": ds}

    @router.put("/api/admin/ragflow/datasets/{dataset_id}")
    async def rename_dataset(dataset_id: str, payload: RenameDatasetPayload) -> dict:
        """重命名/改描述。"""
        if not payload.name.strip():
            raise HTTPException(400, "库名不能为空")
        try:
            await get_ragflow_client().rename_dataset(
                dataset_id, payload.name.strip(), payload.description.strip())
        except Exception as e:
            raise HTTPException(400, f"RAGFlow 重命名失败：{e}")
        return {"ok": True}

    @router.delete("/api/admin/ragflow/datasets")
    async def delete_datasets(dataset_ids: str) -> dict:
        """删除知识库（query 传 dataset_ids，逗号分隔；库内文档与分段一并删除，不可恢复）。"""
        ids = [x for x in dataset_ids.split(",") if x.strip()]
        if not ids:
            raise HTTPException(400, "未指定要删除的知识库")
        try:
            await get_ragflow_client().delete_datasets(ids)
        except Exception as e:
            raise HTTPException(400, f"RAGFlow 删除知识库失败：{e}")
        return {"ok": True}


    @router.put("/api/admin/ragflow/documents-enabled")
    async def set_documents_enabled(payload: DocumentsEnabledPayload) -> dict:
        """文档启停统一端点：单文件（传一个 id）/整库（ids 缺省）。
        逐项容错，返回部分成功报告。禁用后 RAGFlow 检索不召回（nl2sql 检索自动跟随）。"""
        try:
            ids = payload.document_ids or None
            result = await get_ragflow_client().set_documents_enabled(
                payload.dataset_id, ids, payload.enabled)
        except Exception as e:
            raise HTTPException(400, f"RAGFlow 启停失败：{e}")
        return {"ok": True, **result}

    @router.get("/api/admin/ragflow/documents")
    async def list_documents(dataset_id: str, page: int = 1, page_size: int = 100) -> dict:   # 该 RAGFlow 版本上限 100
        """列出某知识库的文档（含解析状态 run / chunk_count）。"""
        try:
            return {"documents": await get_ragflow_client().list_documents(
                dataset_id, page=page, page_size=page_size)}
        except Exception as e:
            raise HTTPException(400, f"RAGFlow 列文档失败：{e}")

    @router.post("/api/admin/ragflow/documents")
    async def upload_document(dataset_id: str = Form(...), file: UploadFile = File(...)) -> dict:
        """上传文档到指定知识库（multipart）。上传后需调 /parse 触发解析才可检索。"""
        content = await file.read()
        if not content:
            raise HTTPException(400, "空文件")
        try:
            data = await get_ragflow_client().upload_document(dataset_id, file.filename, content)
        except Exception as e:
            raise HTTPException(400, f"RAGFlow 上传失败：{e}")
        return {"ok": True, "documents": data}


    @router.post("/api/admin/ragflow/parse")
    async def parse_documents(payload: ParsePayload) -> dict:
        """触发文档解析（分段 + embedding）。上传后必须调，否则不可检索。"""
        try:
            await get_ragflow_client().parse_documents(payload.dataset_id, payload.document_ids)
        except Exception as e:
            raise HTTPException(400, f"RAGFlow 解析失败：{e}")
        return {"ok": True}

    @router.delete("/api/admin/ragflow/documents")
    async def delete_documents(dataset_id: str, document_ids: list[str]) -> dict:
        """删除文档（query 传 dataset_id，body 传 document_ids 列表）。"""
        try:
            await get_ragflow_client().delete_documents(dataset_id, document_ids)
        except Exception as e:
            raise HTTPException(400, f"RAGFlow 删除失败：{e}")
        return {"ok": True}

    return router
