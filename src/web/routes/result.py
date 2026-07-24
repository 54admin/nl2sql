"""取查询全量结果（前端按 result_id 渲染表格用）+ Excel 导出（P4 需求 8.4）。P1b。"""
from io import BytesIO

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook

from src.storage.query_results import get_result


def build_result_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/result/{result_id}")
    async def fetch_result(result_id: str) -> dict:
        r = await get_result(result_id)
        if r is None:
            raise HTTPException(404, "结果不存在或已过期")
        return r  # {columns, rows, total}

    @router.get("/api/result/{result_id}/export")
    async def export_result(result_id: str):
        """导出全量结果为 Excel（openpyxl）。前端表格「导出」按钮调它。"""
        r = await get_result(result_id)
        if r is None:
            raise HTTPException(404, "结果不存在或已过期")
        wb = Workbook()
        ws = wb.active
        cols = list(r["columns"])
        ws.append(cols)
        for row in r["rows"]:
            ws.append([row.get(c) for c in cols])
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="result_{result_id}.xlsx"'})

    return router
