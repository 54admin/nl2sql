"""审计查询路由：trace 列表（分页+筛选）/ trace 详情 / 全局统计。

GET /api/admin/audit/traces        trace 分页列表（支持 session_id/user_id/success 筛选）
GET /api/admin/audit/trace/{tid}   单次 trace 全链路（汇总行 + 事件流按序）
GET /api/admin/audit/stats         全局统计（成功率/平均耗时/调用次数）

ponytail: 鉴权层 P5 再补，内网单租户先暴露供调试统计。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from src.storage.models import AuditEvent, AuditTrace, Session
from src.storage.pg_client import AsyncSessionFactory


def build_audit_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/audit/traces")
    async def list_traces(session_id: str | None = None,
                          user_id: str | None = None,
                          success: bool | None = None,
                          page: int = 1, page_size: int = 20) -> dict:
        """trace 分页列表：支持按 session_id/user_id/success 筛选，按时间倒序。
        返回 traces + total（前端分页用）。page_size 兜底 [1,100]。"""
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)
        stmt = select(AuditTrace, Session.title, Session.channel).outerjoin(
            Session, AuditTrace.session_id == Session.id)
        if session_id:
            stmt = stmt.where(AuditTrace.session_id == session_id)
        if user_id:
            stmt = stmt.where(AuditTrace.user_id == user_id)
        if success is not None:
            stmt = stmt.where(AuditTrace.success.is_(success))
        # 先查总数（不带 order/limit），再查当前页
        count_stmt = select(func.count()).select_from(stmt.subquery())
        async with AsyncSessionFactory() as s:
            total = (await s.execute(count_stmt)).scalar() or 0
            rows = (await s.execute(
                stmt.order_by(AuditTrace.created_at.desc())
                .offset((page - 1) * page_size).limit(page_size))).all()
        return {
            "total": total, "page": page, "page_size": page_size,
            "traces": [{
                "trace_id": r.trace_id, "session_id": r.session_id,
                "session_title": title or "", "channel": channel or "",
                "user_id": r.user_id, "raw_input": r.raw_input,
                "success": r.success,
                "final_answer": (r.final_answer or "")[:120],
                "sql_text": (r.sql_text or "")[:200],
                "result_id": r.result_id, "elapsed_ms": r.elapsed_ms,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            } for r, title, channel in rows],
        }

    @router.get("/api/admin/audit/trace/{trace_id}")
    async def get_trace(trace_id: str) -> dict:
        """单次 trace 全链路：汇总行 + 事件流（按 seq 排序）。"""
        async with AsyncSessionFactory() as s:
            t = await s.get(AuditTrace, trace_id)
            if t is None:
                raise HTTPException(404, "trace 不存在")
            evs = (await s.execute(
                select(AuditEvent).where(AuditEvent.trace_id == trace_id)
                .order_by(AuditEvent.seq))).scalars().all()
        return {
            "trace": {
                "trace_id": t.trace_id, "session_id": t.session_id,
                "user_id": t.user_id, "raw_input": t.raw_input,
                "normalized_input": t.normalized_input,
                "success": t.success, "final_answer": t.final_answer,
                "sql_text": t.sql_text, "result_id": t.result_id,
                "elapsed_ms": t.elapsed_ms, "cost_tokens": t.cost_tokens,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            },
            "events": [{
                "seq": e.seq, "event_type": e.event_type, "turn": e.turn,
                "content": json.loads(e.content_json) if e.content_json else None,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            } for e in evs],
        }

    @router.get("/api/admin/audit/stats")
    async def audit_stats() -> dict:
        """全局统计：总 trace 数/成功率/平均耗时/按事件类型计数。
        复盘+巡检用——一眼看问数整体质量。"""
        async with AsyncSessionFactory() as s:
            total = (await s.execute(select(func.count()).select_from(AuditTrace))).scalar() or 0
            ok = (await s.execute(select(func.count()).select_from(AuditTrace)
                                  .where(AuditTrace.success.is_(True)))).scalar() or 0
            avg_ms = (await s.execute(select(func.avg(AuditTrace.elapsed_ms))
                                     .where(AuditTrace.elapsed_ms.isnot(None)))).scalar()
            # 按事件类型计数
            type_rows = (await s.execute(
                select(AuditEvent.event_type, func.count()).group_by(AuditEvent.event_type))).all()
        return {
            "total_traces": total,
            "success_count": ok,
            "success_rate": round(ok / total, 4) if total else 0.0,
            "avg_elapsed_ms": round(float(avg_ms), 1) if avg_ms else 0.0,
            "events_by_type": {r[0]: r[1] for r in type_rows},
        }

    @router.get("/api/admin/audit/filters")
    async def audit_filters() -> dict:
        """筛选下拉选项：去重的人 + 全部未删除会话（按最近更新倒序）。
        供统计页两个 <select> 填充。"""
        async with AsyncSessionFactory() as s:
            users = (await s.execute(
                select(AuditTrace.user_id)
                .where(AuditTrace.user_id.isnot(None), AuditTrace.user_id != "")
                .group_by(AuditTrace.user_id))).scalars().all()
            sess_rows = (await s.execute(
                select(Session.id, Session.title, Session.channel)
                .where(Session.deleted_at.is_(None))
                .order_by(Session.updated_at.desc()).limit(500))).all()   # 下拉装不下几千项，取最近 500
        return {
            "users": users,
            "sessions": [{"id": r.id, "title": r.title or "", "channel": r.channel or ""}
                         for r in sess_rows],
        }

    return router
