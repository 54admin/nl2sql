import pytest

from src.config import RedisConfig
from src.core.normalizer import Correction
from src.core.orchestrator import Orchestrator
from src.core.types import SSEEvent
from src.memory.session import SessionManager
from src.storage.pg_client import init_db
from src.storage.redis_client import RedisClient
from src.web.sse import ViewerMode


class FakeNormalizer:
    def __init__(self, text=None, corrections=None):
        self._text = text
        self._corrections = corrections or []

    async def normalize(self, text):
        if self._text is not None:
            return self._text, self._corrections
        return text, []


class FakeLoop:
    def __init__(self, events):
        self._events = list(events)
        self.calls = []

    async def run(self, session_id, user_id, user_msg, trace_id,
                  cancel_token, is_resume=False, system_prompt=None, **kwargs):
        self.calls.append({"session_id": session_id, "user_msg": user_msg,
                           "trace_id": trace_id, "is_resume": is_resume,
                           "system_prompt": system_prompt})
        for e in self._events:
            yield e


class BoomLoop:
    async def run(self, **kw):
        yield SSEEvent("turn_start", {}, "t1")
        raise RuntimeError("炸了")


class FakePromptStore:
    def __init__(self, prompts=None):
        self._prompts = prompts or {}
        self.get_calls = []

    async def get(self, scene="default"):
        self.get_calls.append(scene)
        return self._prompts.get(scene)


class FakeRuleStore:
    def __init__(self, text=""):
        self._text = text

    async def all_text(self):
        return self._text


@pytest.fixture
async def session_mgr():
    await init_db("sqlite+aiosqlite:///:memory:")
    redis = RedisClient(RedisConfig(host="unreachable.invalid", port=1))
    await redis.connect()
    return SessionManager(redis)


async def _collect(gen):
    return [e async for e in gen]


@pytest.mark.asyncio
async def test_new_session_emits_correction(session_mgr):
    sid = await session_mgr.create_session("u1", "web")
    norm = FakeNormalizer(text="新疆分公司", corrections=[
        Correction(raw="新疆省", standard="新疆", confidence=0.99, source="typo")])
    loop = FakeLoop([SSEEvent("answer_delta", {"text": "结果"}, "t1"),
                     SSEEvent("done", {"answer": "结果"}, "t1")])
    orch = Orchestrator(norm, loop, session_mgr)
    events = await _collect(orch.handle_message("u1", sid, "新疆省发电量",
                                                ViewerMode.USER, "t1"))
    assert events[0].type == "correction"
    assert events[0].data["original"] == "新疆省发电量"
    assert events[0].data["normalized"] == "新疆分公司"
    assert len(events[0].data["corrections"]) == 1
    assert loop.calls[0]["user_msg"] == "新疆分公司"
    assert loop.calls[0]["is_resume"] is False


@pytest.mark.asyncio
async def test_new_session_no_correction_when_clean(session_mgr):
    sid = await session_mgr.create_session("u1", "web")
    norm = FakeNormalizer()
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "t1")])
    orch = Orchestrator(norm, loop, session_mgr)
    events = await _collect(orch.handle_message("u1", sid, "你好",
                                                ViewerMode.USER, "t1"))
    assert all(e.type != "correction" for e in events)


@pytest.mark.asyncio
async def test_resume_skips_normalizer(session_mgr):
    sid = await session_mgr.create_session("u1", "web")
    await session_mgr.set_status(sid, "awaiting_clarification")
    norm = FakeNormalizer(text="不应使用")
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "t1")])
    orch = Orchestrator(norm, loop, session_mgr)
    events = await _collect(orch.handle_message("u1", sid, "6月",
                                                ViewerMode.USER, "t1"))
    assert all(e.type != "correction" for e in events)
    assert loop.calls[0]["user_msg"] == "6月"
    assert loop.calls[0]["is_resume"] is True


@pytest.mark.asyncio
async def test_passthrough_events_in_order(session_mgr):
    sid = await session_mgr.create_session("u1", "web")
    norm = FakeNormalizer()
    expected = [SSEEvent("query_progress", {"p": 1}, "t1"),
                SSEEvent("answer_delta", {"text": "x"}, "t1"),
                SSEEvent("done", {"answer": "x"}, "t1")]
    loop = FakeLoop(expected)
    orch = Orchestrator(norm, loop, session_mgr)
    events = await _collect(orch.handle_message("u1", sid, "你好",
                                                ViewerMode.ADMIN, "t1"))
    assert [e.type for e in events] == ["query_progress", "answer_delta", "done"]


@pytest.mark.asyncio
async def test_loop_exception_becomes_error_event(session_mgr):
    sid = await session_mgr.create_session("u1", "web")
    norm = FakeNormalizer()
    orch = Orchestrator(norm, BoomLoop(), session_mgr)
    events = await _collect(orch.handle_message("u1", sid, "你好",
                                                ViewerMode.USER, "t1"))
    types = [e.type for e in events]
    assert "error" in types
    err = [e for e in events if e.type == "error"][0]
    assert "炸了" in err.data["message"]


@pytest.mark.asyncio
async def test_trace_id_propagated_to_correction(session_mgr):
    sid = await session_mgr.create_session("u1", "web")
    norm = FakeNormalizer(text="x", corrections=[
        Correction(raw="y", standard="x", confidence=0.9, source="typo")])
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "mytrace")])
    orch = Orchestrator(norm, loop, session_mgr)
    events = await _collect(orch.handle_message("u1", sid, "y",
                                                ViewerMode.USER, "mytrace"))
    corr = [e for e in events if e.type == "correction"]
    assert all(e.trace_id == "mytrace" for e in corr)


@pytest.mark.asyncio
async def test_nonexistent_session_treated_as_new(session_mgr):
    norm = FakeNormalizer()
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "t1")])
    orch = Orchestrator(norm, loop, session_mgr)
    events = await _collect(orch.handle_message("u1", "ghost-sid", "你好",
                                                ViewerMode.USER, "t1"))
    assert loop.calls[0]["is_resume"] is False
    assert events[-1].type == "done"


@pytest.mark.asyncio
async def test_prompt_store_injects_system_prompt(session_mgr):
    norm = FakeNormalizer()
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "t1")])
    prompts = FakePromptStore(prompts={"default": "你是问数助手"})
    orch = Orchestrator(norm, loop, session_mgr, prompt_store=prompts)
    sid = await session_mgr.create_session("u1", "web")
    await _collect(orch.handle_message("u1", sid, "你好",
                                       ViewerMode.USER, "t1"))
    assert loop.calls[0]["system_prompt"] == "你是问数助手"
    assert prompts.get_calls == ["default"]


@pytest.mark.asyncio
async def test_no_prompt_store_passes_none(session_mgr):
    norm = FakeNormalizer()
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "t1")])
    orch = Orchestrator(norm, loop, session_mgr)
    sid = await session_mgr.create_session("u1", "web")
    await _collect(orch.handle_message("u1", sid, "你好",
                                       ViewerMode.USER, "t1"))
    assert loop.calls[0]["system_prompt"] is None


@pytest.mark.asyncio
async def test_rule_store_appends_to_system_prompt(session_mgr):
    """P2 业务规则段追加到 system_prompt 末尾。"""
    norm = FakeNormalizer()
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "t1")])
    prompts = FakePromptStore(prompts={"default": "你是问数助手"})
    rules = FakeRuleStore("- 发电量单位: 万kWh")
    orch = Orchestrator(norm, loop, session_mgr, prompt_store=prompts,
                        rule_store=rules)
    sid = await session_mgr.create_session("u1", "web")
    await _collect(orch.handle_message("u1", sid, "你好",
                                       ViewerMode.USER, "t1"))
    sp = loop.calls[0]["system_prompt"]
    assert sp.startswith("你是问数助手")
    assert "【业务规则】" in sp
    assert "发电量单位" in sp


@pytest.mark.asyncio
async def test_no_rule_store_keeps_prompt_untouched(session_mgr):
    """rule_store=None 时不追加（system_prompt 原样）。"""
    norm = FakeNormalizer()
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "t1")])
    prompts = FakePromptStore(prompts={"default": "你是问数助手"})
    orch = Orchestrator(norm, loop, session_mgr, prompt_store=prompts)
    sid = await session_mgr.create_session("u1", "web")
    await _collect(orch.handle_message("u1", sid, "你好",
                                       ViewerMode.USER, "t1"))
    assert loop.calls[0]["system_prompt"] == "你是问数助手"
