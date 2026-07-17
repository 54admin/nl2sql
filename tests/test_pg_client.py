import pytest

from src.storage.pg_client import init_db, AsyncSessionFactory
from src.storage.models import Session, Message


@pytest.mark.asyncio
async def test_init_db_creates_tables(tmp_path):
    # 用 file-based sqlite，验证 schema 能建、能写能读
    url = f"sqlite+aiosqlite:///{tmp_path}/t.db"
    await init_db(url)

    async with AsyncSessionFactory() as s:
        sess = Session(id="s1", user_id="u1", channel="web", status="idle")
        s.add(sess)
        await s.commit()
        msg = Message(id="m1", session_id="s1", role="user",
                      content="你好", trace_id="t1")
        s.add(msg)
        await s.commit()

        loaded = (await s.execute(
            Session.__table__.select().where(Session.id == "s1")
        )).first()
        assert loaded.user_id == "u1"
