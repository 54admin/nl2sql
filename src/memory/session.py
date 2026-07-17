"""会话管理：Redis 热态 + PG 持久。"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from src.logging import get_logger
from src.storage.models import Session as SessionRow, Message
from src.storage.pg_client import AsyncSessionFactory
from src.storage.redis_client import RedisClient

log = get_logger(__name__)

SESSION_TTL = 3600  # 秒，需求 1.6 长时间无操作清空
SESSION_KEY = "session:{sid}"
MSGS_KEY = "session:{sid}:messages"


class SessionManager:
    def __init__(self, redis: RedisClient):
        self._redis = redis

    async def create_session(self, user_id: str, channel: str) -> str:
        sid = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        ttl_at = now + timedelta(seconds=SESSION_TTL)
        # PG 持久
        async with AsyncSessionFactory() as s:
            s.add(SessionRow(id=sid, user_id=user_id, channel=channel,
                             status="idle", ttl_at=ttl_at))
            await s.commit()
        # Redis 热态
        await self._redis.set(SESSION_KEY.format(sid=sid),
                              json.dumps({"user_id": user_id, "channel": channel,
                                          "status": "idle"}),
                              ttl=SESSION_TTL)
        await self._redis.set(MSGS_KEY.format(sid=sid), json.dumps([]),
                              ttl=SESSION_TTL)
        log.info("创建会话 %s (user=%s)", sid, user_id)
        return sid

    async def get_session(self, sid: str) -> dict | None:
        raw = await self._redis.get(SESSION_KEY.format(sid=sid))
        if raw:
            return json.loads(raw)
        # Redis 未命中，回查 PG 并回填
        async with AsyncSessionFactory() as s:
            row = (await s.execute(
                SessionRow.__table__.select().where(SessionRow.id == sid)
            )).first()
            if not row:
                return None
            data = {"user_id": row.user_id, "channel": row.channel,
                    "status": row.status}
        await self._redis.set(SESSION_KEY.format(sid=sid),
                              json.dumps(data), ttl=SESSION_TTL)
        return data

    async def set_status(self, sid: str, status: str):
        sess = await self.get_session(sid)
        if not sess:
            return
        sess["status"] = status
        await self._redis.set(SESSION_KEY.format(sid=sid),
                              json.dumps(sess), ttl=SESSION_TTL)
        async with AsyncSessionFactory() as s:
            row = await s.get(SessionRow, sid)
            if row:
                row.status = status
                await s.commit()

    async def append_message(self, sid: str, role: str, content: str,
                             trace_id: str):
        mid = uuid.uuid4().hex
        # PG
        async with AsyncSessionFactory() as s:
            s.add(Message(id=mid, session_id=sid, role=role,
                          content=content, trace_id=trace_id))
            await s.commit()
        # Redis 最近消息（热态）
        msgs = json.loads(await self._redis.get(MSGS_KEY.format(sid=sid)) or "[]")
        msgs.append({"role": role, "content": content})
        await self._redis.set(MSGS_KEY.format(sid=sid), json.dumps(msgs),
                              ttl=SESSION_TTL)

    async def get_messages(self, sid: str) -> list[dict]:
        raw = await self._redis.get(MSGS_KEY.format(sid=sid))
        if raw:
            return json.loads(raw)
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(
                Message.__table__.select().where(Message.session_id == sid)
                .order_by(Message.created_at)
            )).all()
            msgs = [{"role": r.role, "content": r.content} for r in rows]
        await self._redis.set(MSGS_KEY.format(sid=sid), json.dumps(msgs),
                              ttl=SESSION_TTL)
        return msgs

    async def delete_session(self, sid: str):
        await self._redis.delete(SESSION_KEY.format(sid=sid))
        await self._redis.delete(MSGS_KEY.format(sid=sid))
        async with AsyncSessionFactory() as s:
            row = await s.get(SessionRow, sid)
            if row:
                await s.delete(row)
                await s.commit()
