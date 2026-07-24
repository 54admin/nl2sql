"""知识库 admin 路由（P3b）：上传文档 / 列表 / 启停 / 删除。
上传走 UploadFile（TXT/MD），KnowledgeStore 分段 embedding 入库。
无参工厂（用 get_knowledge_store 单例，避免 main 装配注入）。"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from src.knowledge.parsing import parse_to_text
from src.knowledge.store import get_knowledge_store


def build_knowledge_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/knowledge/docs")
    async def list_docs(category: str | None = None) -> dict:
        return {"docs": await get_knowledge_store().list_documents(category)}

    @router.post("/api/admin/knowledge/upload")
    async def upload(file: UploadFile = File(...),
                     category: str = Form("general")) -> dict:
        if not file.filename:
            raise HTTPException(400, "缺少文件名")
        raw = await file.read()
        content = parse_to_text(file.filename, raw) if raw else ""
        try:
            doc_id = await get_knowledge_store().add_document(
                file.filename, content, category)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))   # embedding 失败（未配置/网关不通）
        return {"id": doc_id, "name": file.filename}

    @router.put("/api/admin/knowledge/docs/{doc_id}")
    async def set_enabled(doc_id: int, enabled: bool) -> dict:
        if not await get_knowledge_store().set_enabled(doc_id, enabled):
            raise HTTPException(404, "文档不存在")
        return {"ok": True}

    @router.delete("/api/admin/knowledge/docs/{doc_id}")
    async def delete_doc(doc_id: int) -> dict:
        if not await get_knowledge_store().delete_document(doc_id):
            raise HTTPException(404, "文档不存在")
        return {"ok": True}

    return router
