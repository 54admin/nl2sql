"""admin 查询上限配置路由：单配置 GET/PUT。配置走 agent_limits 表，
lifespan 启动读、构造 AgentLoop 传入；改完重启生效（无热重连）。
照 admin_feishu 单配置范式，去掉 adapter 热重连。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.storage.models import AgentLimitsRow
from src.storage.pg_client import AsyncSessionFactory

DEFAULT_ID = "default"


class AgentLimitsPayload(BaseModel):
    max_turns: int = 10
    max_ask_user: int = 2
    max_sql: int = 4
    max_sql_fail_streak: int = 2
    max_meta_per_run: int = 1


def _defaults() -> dict:
    """默认上限——AgentLimitsPayload 为唯一真源，别处默认值都从这里取。"""
    return AgentLimitsPayload().model_dump()


async def load_agent_limits() -> dict:
    """读 agent_limits(default)，无行/表未建都返默认（不阻塞启动）。lifespan + GET 共用。"""
    try:
        async with AsyncSessionFactory() as s:
            row = await s.get(AgentLimitsRow, DEFAULT_ID)
    except Exception:
        return {"id": DEFAULT_ID, "version": 0, **_defaults()}   # 表未建 → 默认（lifespan 不崩）
    if row is None:
        return {"id": DEFAULT_ID, "version": 0, **_defaults()}
    return {"id": row.id, "version": row.version,
            "max_turns": row.max_turns, "max_ask_user": row.max_ask_user,
            "max_sql": row.max_sql, "max_sql_fail_streak": row.max_sql_fail_streak,
            "max_meta_per_run": row.max_meta_per_run}


def build_agent_limits_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/agent-limits")
    async def get_limits() -> dict:
        return await load_agent_limits()

    @router.put("/api/admin/agent-limits")
    async def save_limits(payload: AgentLimitsPayload) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(AgentLimitsRow, DEFAULT_ID)
            if row is None:
                row = AgentLimitsRow(id=DEFAULT_ID, version=0)
                s.add(row)
            row.max_turns = payload.max_turns
            row.max_ask_user = payload.max_ask_user
            row.max_sql = payload.max_sql
            row.max_sql_fail_streak = payload.max_sql_fail_streak
            row.max_meta_per_run = payload.max_meta_per_run
            row.version += 1
            await s.commit()
            version = row.version
        return {"ok": True, "version": version}   # 改完重启应用生效（lifespan 读）

    return router
