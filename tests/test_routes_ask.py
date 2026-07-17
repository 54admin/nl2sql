import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.types import SSEEvent
from src.web.routes.ask import build_ask_router


class FakeOrchestrator:
    def __init__(self, events):
        self._events = events

    async def handle_message(self, user_id, session_id, text, mode, trace_id):
        for e in self._events:
            yield e


@pytest.fixture
def client():
    events = [
        SSEEvent("correction",
                 {"original": "x", "normalized": "y", "corrections": []}, "t1"),
        SSEEvent("sql_generated", {"sql": "select 1"}, "t1"),
        SSEEvent("answer_delta", {"text": "结果"}, "t1"),
        SSEEvent("done", {"answer": "结果"}, "t1"),
    ]
    app = FastAPI()
    app.include_router(build_ask_router(FakeOrchestrator(events)))
    return TestClient(app)


def test_ask_sse_returns_event_stream(client):
    resp = client.post("/api/ask/sse", json={
        "user_id": "u1", "session_id": "s1", "text": "你好", "mode": "user"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]


def test_ask_sse_user_mode_hides_sql(client):
    resp = client.post("/api/ask/sse", json={
        "user_id": "u1", "session_id": "s1", "text": "你好", "mode": "user"})
    body = resp.text
    assert "event: correction" in body
    assert "event: answer_delta" in body
    assert "event: done" in body
    assert "sql_generated" not in body
    assert "t1" in body


def test_ask_sse_admin_mode_shows_all(client):
    resp = client.post("/api/ask/sse", json={
        "user_id": "u1", "session_id": "s1", "text": "你好", "mode": "admin"})
    body = resp.text
    assert "sql_generated" in body


def test_ask_sse_default_mode_is_user(client):
    resp = client.post("/api/ask/sse", json={
        "user_id": "u1", "session_id": "s1", "text": "你好"})
    body = resp.text
    assert "sql_generated" not in body


def test_ask_sse_invalid_mode_returns_422(client):
    resp = client.post("/api/ask/sse", json={
        "user_id": "u1", "session_id": "s1", "text": "你好", "mode": "ghost"})
    assert resp.status_code == 422
