"""取查询全量结果（前端按 result_id 渲染表格用）。P1b。"""
from fastapi import APIRouter, HTTPException

from src.storage.query_results import get_result


def build_result_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/result/{result_id}")
    async def fetch_result(result_id: str) -> dict:
        r = await get_result(result_id)
        if r is None:
            raise HTTPException(404, "结果不存在或已过期")
        return r  # {columns, rows, total}

    return router
