"""会话状态机 + ask_user 挂起/恢复（spec 6.4）。
状态存储委托 P0a SessionManager（Redis 热 + PG 持久），checkpoint 存 PG LoopCheckpoint。
状态转换严格按状态机表校验，非法转换抛 ValueError。"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from src.logging import get_logger
from src.memory.session import SessionManager
from src.storage.models import LoopCheckpoint
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)


class SessionStatus(str, Enum):
    """会话状态（spec 6.4 状态机）。"""
    IDLE = "idle"
    RUNNING = "running"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    DONE = "done"
    ERROR = "error"


# 合法状态转换（spec 6.4 状态机 + RUNNING→IDLE 取消场景）
ALLOWED_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.IDLE: {SessionStatus.RUNNING},
    SessionStatus.RUNNING: {SessionStatus.RUNNING, SessionStatus.AWAITING_CLARIFICATION,
                            SessionStatus.DONE, SessionStatus.ERROR, SessionStatus.IDLE},
    SessionStatus.AWAITING_CLARIFICATION: {SessionStatus.RUNNING, SessionStatus.IDLE,
                                           SessionStatus.ERROR},
    SessionStatus.DONE: {SessionStatus.IDLE, SessionStatus.RUNNING},  # done 后能继续问（同会话第二轮）
    SessionStatus.ERROR: {SessionStatus.IDLE, SessionStatus.RUNNING},
}


@dataclass
class ResumedContext:
    """恢复后的 loop 上下文，交给 AgentLoop 续跑。
    messages 已把用户回答作为 ask_user tool result append 进去。"""
    messages: list[dict]
    checkpoint_id: str
    pending_tool: str | None


class SessionState:
    """会话状态机 + ask_user 挂起/恢复。状态存储全部委托 SessionManager。"""

    def __init__(self, session_manager: SessionManager):
        self._sm = session_manager

    async def current_status(self, sid: str) -> SessionStatus | None:
        sess = await self._sm.get_session(sid)
        return SessionStatus(sess["status"]) if sess else None

    async def transition(self, sid: str, target: SessionStatus) -> None:
        """校验状态转换合法性，非法抛 ValueError。合法则委托 SessionManager 双写。"""
        cur = await self.current_status(sid)
        if cur is None:
            raise ValueError(f"会话不存在: {sid}")
        if target not in ALLOWED_TRANSITIONS.get(cur, set()):
            raise ValueError(f"非法状态转换 {cur.value} -> {target.value} (sid={sid})")
        await self._sm.set_status(sid, target.value)

    async def is_suspended(self, sid: str) -> bool:
        return await self.current_status(sid) == SessionStatus.AWAITING_CLARIFICATION

    async def suspend(self, sid: str, messages: list[dict],
                      pending_tool: str | None = None) -> str:
        """挂起：存 LoopCheckpoint + 转 awaiting。受状态机约束，仅 running 可挂起。"""
        cp_id = uuid.uuid4().hex
        # 显式带 created_at：sqlite 下 server_default=func.now() 返字符串，
        # 与 Python datetime 比较会类型错乱（sweep_stale 的 < cutoff 永真/假不稳）。
        async with AsyncSessionFactory() as s:
            s.add(LoopCheckpoint(
                id=cp_id, session_id=sid,
                messages_json=json.dumps(messages, ensure_ascii=False),
                pending_tool=pending_tool, created_at=datetime.now()))
            await s.commit()
        await self.transition(sid, SessionStatus.AWAITING_CLARIFICATION)
        log.info("挂起 sid=%s checkpoint=%s pending_tool=%s", sid, cp_id, pending_tool)
        return cp_id

    async def resume(self, sid: str, user_reply: str) -> ResumedContext | None:
        """恢复：把用户回答作为 ask_user 工具结果注入 messages，删 checkpoint，转 running。
        非挂起态返回 None（orchestrator 走正常路径）。"""
        if not await self.is_suspended(sid):
            return None
        cp = await self._latest_checkpoint(sid)
        if cp is None:
            log.warning("挂起态但无 checkpoint，回退 idle: sid=%s", sid)
            await self.transition(sid, SessionStatus.IDLE)
            return None
        messages = json.loads(cp.messages_json)
        messages.append({"role": "tool",
                         "tool_call_id": cp.pending_tool,
                         "content": user_reply})
        await self._delete_checkpoint(cp.id)
        await self.transition(sid, SessionStatus.RUNNING)
        return ResumedContext(messages=messages, checkpoint_id=cp.id,
                              pending_tool=cp.pending_tool)

    async def expire_suspended(self, sid: str) -> bool:
        """挂起超时放弃：awaiting -> idle 并清 checkpoint。"""
        if not await self.is_suspended(sid):
            return False
        cp = await self._latest_checkpoint(sid)
        if cp:
            await self._delete_checkpoint(cp.id)
        await self.transition(sid, SessionStatus.IDLE)
        return True

    async def sweep_stale_suspended(self, max_age_minutes: int = 30) -> int:
        """扫所有挂起超 max_age_minutes 的会话，判过期清 checkpoint。
        用于：服务启动时补清上次挂掉遗留的孤儿 + 运行期周期清扫。
        返回清理的会话数。

        ponytail: 按 checkpoint.created_at 筛超时的，逐个调 expire_suspended。
        不用一条 bulk UPDATE 是为了复用状态机校验（awaiting->idle 合法）。
        清理本身幂等：checkpoint 已不在就跳过。"""
        cutoff = datetime.now() - timedelta(minutes=max_age_minutes)
        async with AsyncSessionFactory() as s:
            stale = (await s.execute(
                LoopCheckpoint.__table__.select()
                .where(LoopCheckpoint.created_at < cutoff)
            )).all()
        if not stale:
            return 0
        cleared = 0
        for cp in stale:
            sid = cp.session_id
            # expire 内部会校验状态：仍是 awaiting 才清 checkpoint+转 idle。
            # 会话若已被 resume/done 走掉但 checkpoint 残留（异常残留），直接删孤儿。
            if await self.is_suspended(sid):
                if await self.expire_suspended(sid):
                    cleared += 1
            else:
                await self._delete_checkpoint(cp.id)
                cleared += 1
                log.info("清孤儿 checkpoint sid=%s cp=%s（会话已非挂起态）",
                         sid, cp.id)
        if cleared:
            log.info("挂起超时清扫完成：清理 %d 个（阈值 %d 分钟）", cleared, max_age_minutes)
        return cleared

    async def _latest_checkpoint(self, sid: str):
        """取该会话最新一条 checkpoint（按 created_at desc）。"""
        async with AsyncSessionFactory() as s:
            return (await s.execute(
                LoopCheckpoint.__table__.select()
                .where(LoopCheckpoint.session_id == sid)
                .order_by(LoopCheckpoint.created_at.desc()).limit(1)
            )).first()

    async def _delete_checkpoint(self, cp_id: str) -> None:
        async with AsyncSessionFactory() as s:
            row = await s.get(LoopCheckpoint, cp_id)
            if row:
                await s.delete(row)
                await s.commit()
