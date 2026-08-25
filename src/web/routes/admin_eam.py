"""admin EAM 路由：目录树 / 文件清单 / 同步到 RAGFlow（全部只读 EAM，写入只发生在 RAGFlow 侧）。

EamClient 由 main.py lifespan 构造（配置来自 yml eam 段，改配置需重启——ak/sk 长期凭证，可接受）。
kb_op 角色可访问本组端点（auth_guard 矩阵放行 /api/admin/eam/*）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.eam.client import EamClient, EamError
from src.logging import get_logger
from src.ragflow.client import get_ragflow_client

log = get_logger(__name__)

SYNC_MAX_FILES = 20   # 单批同步文件上限（前端同样硬限，防大文件批量拖垮请求）


class SyncItem(BaseModel):
    id: str
    name: str = ""


class SyncPayload(BaseModel):
    items: list[SyncItem]          # EAM 文件（document/list 的 id + name）
    dataset_ids: list[str]         # 目标知识库（可多个，同一文件分别进入每个库）


def build_admin_eam_router(eam: EamClient) -> APIRouter:
    """构造 admin EAM 路由。eam 由 _Lazy("eam_client") 延迟解析。"""
    router = APIRouter()

    @router.get("/api/admin/eam/tree")
    async def tree() -> dict:
        """EAM 全量目录树（上传弹窗的文档树用）。"""
        try:
            return {"tree": await eam.tree()}
        except EamError as e:
            raise HTTPException(400, f"EAM 获取目录树失败：{e}")

    @router.get("/api/admin/eam/files")
    async def files() -> dict:
        """EAM 全量文件清单（按 parentId 挂到树上；fileType 供前端格式过滤）。"""
        try:
            return {"files": await eam.files()}
        except EamError as e:
            raise HTTPException(400, f"EAM 获取文件清单失败：{e}")

    @router.post("/api/admin/eam/sync")
    async def sync(payload: SyncPayload) -> dict:
        """同步 EAM 文件到知识库：逐 (文件×库) 执行「EAM 下载 → RAGFlow 上传 → 触发解析」，
        逐项容错不中断批次，返回每项结果。同步请求内串行完成（内网下载+上传快，慢的是
        RAGFlow 解析——进度由文档列表轮询展示）。"""
        if not payload.items:
            raise HTTPException(400, "未选择文件")
        if len(payload.items) > SYNC_MAX_FILES:
            raise HTTPException(400, f"单次最多同步 {SYNC_MAX_FILES} 个文件，请分批")
        if not payload.dataset_ids:
            raise HTTPException(400, "未选择目标知识库")
        results, ok_count = [], 0
        for item in payload.items:
            for ds_id in payload.dataset_ids:
                try:
                    content = await eam.download(item.id, "file", item.name or item.id)
                    docs = await get_ragflow_client().upload_document(ds_id, item.name, content)
                    doc_ids = [d.get("id") for d in docs if d.get("id")]
                    if doc_ids:
                        await get_ragflow_client().parse_documents(ds_id, doc_ids)
                    results.append({"name": item.name, "dataset_id": ds_id, "ok": True})
                    ok_count += 1
                except Exception as e:   # 单项失败不中断批次
                    log.warning("EAM 同步单项失败 %s -> %s: %s", item.name, ds_id, e)
                    results.append({"name": item.name, "dataset_id": ds_id,
                                    "ok": False, "error": str(e)[:200]})
        return {"results": results, "ok_count": ok_count,
                "fail_count": len(results) - ok_count}

    return router
