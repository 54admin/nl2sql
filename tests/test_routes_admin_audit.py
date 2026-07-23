"""审计查询路由测试。"""
import pytest
import httpx
from fastapi import FastAPI

from src.storage.pg_client import init_db
from src.storage.models import AuditTrace, AuditEvent
from src.storage.pg_client import AsyncSessionFactory
from src.web.routes.admin_audit import build_audit_router


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(build_audit_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # 预置 3 条 trace + 事件流：s1 有 2 条（成功+失败），s2 有 1 条（成功）
        async with AsyncSessionFactory() as s:
            s.add(AuditTrace(trace_id="t1", session_id="s1", user_id="u1",
                             raw_input="问1", success=True, final_answer="答1",
                             sql_text="SELECT 1", result_id="r1", elapsed_ms=120,
                             tool_calls_json="[]"))
            s.add(AuditTrace(trace_id="t2", session_id="s1", user_id="u1",
                             raw_input="问2", success=False, final_answer="⚠ 额度不足",
                             elapsed_ms=50))
            s.add(AuditTrace(trace_id="t3", session_id="s2", user_id="u2",
                             raw_input="问3", success=True, final_answer="答3",
                             elapsed_ms=80))
            evs = [("user_input", None, '{"raw":"问1"}'),
                   ("turn_start", 0, '{}'),
                   ("tool_call", 0, '{"name":"execute_sql","args":{"sql":"SELECT 1"}}'),
                   ("tool_result", 0, '{"result_id":"r1"}'),
                   ("done", None, '{"success":true}')]
            for seq, (et, turn, cj) in enumerate(evs, start=1):
                s.add(AuditEvent(trace_id="t1", seq=seq, event_type=et,
                                 turn=turn, content_json=cj))
            await s.commit()
        yield ac


@pytest.mark.asyncio
async def test_list_traces_no_filter_returns_all(client):
    r = await client.get("/api/admin/audit/traces")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["traces"]) == 3
    assert body["page"] == 1


@pytest.mark.asyncio
async def test_list_traces_filter_by_session(client):
    r = await client.get("/api/admin/audit/traces", params={"session_id": "s1"})
    assert r.json()["total"] == 2
    assert {t["trace_id"] for t in r.json()["traces"]} == {"t1", "t2"}


@pytest.mark.asyncio
async def test_list_traces_filter_by_user(client):
    r = await client.get("/api/admin/audit/traces", params={"user_id": "u2"})
    body = r.json()
    assert body["total"] == 1
    assert body["traces"][0]["trace_id"] == "t3"


@pytest.mark.asyncio
async def test_list_traces_filter_by_success(client):
    # 只看成败
    r = await client.get("/api/admin/audit/traces", params={"success": "false"})
    body = r.json()
    assert body["total"] == 1
    assert body["traces"][0]["trace_id"] == "t2"
    assert body["traces"][0]["success"] is False


@pytest.mark.asyncio
async def test_list_traces_pagination(client):
    # page_size=2 第一页 2 条，total=3
    r = await client.get("/api/admin/audit/traces", params={"page": 1, "page_size": 2})
    body = r.json()
    assert body["total"] == 3
    assert len(body["traces"]) == 2
    # 第二页 1 条
    r2 = await client.get("/api/admin/audit/traces", params={"page": 2, "page_size": 2})
    assert len(r2.json()["traces"]) == 1


@pytest.mark.asyncio
async def test_list_traces_combined_filter(client):
    # session=s1 且 success=true → 只 t1
    r = await client.get("/api/admin/audit/traces",
                         params={"session_id": "s1", "success": "true"})
    body = r.json()
    assert body["total"] == 1
    assert body["traces"][0]["trace_id"] == "t1"


@pytest.mark.asyncio
async def test_get_trace_returns_summary_and_events(client):
    r = await client.get("/api/admin/audit/trace/t1")
    assert r.status_code == 200
    body = r.json()
    assert body["trace"]["trace_id"] == "t1"
    assert body["trace"]["success"] is True
    assert body["trace"]["sql_text"] == "SELECT 1"
    assert len(body["events"]) == 5
    assert body["events"][0]["event_type"] == "user_input"
    assert body["events"][2]["content"]["name"] == "execute_sql"


@pytest.mark.asyncio
async def test_get_trace_404_when_missing(client):
    r = await client.get("/api/admin/audit/trace/ghost")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_audit_stats(client):
    r = await client.get("/api/admin/audit/stats")
    body = r.json()
    assert body["total_traces"] == 3
    assert body["success_count"] == 2
    assert body["success_rate"] == round(2 / 3, 4)
    # 事件类型计数（t1 有 5 个事件）
    assert body["events_by_type"]["tool_call"] == 1
