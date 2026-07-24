from src.llm.service import (LLMService, _is_retryable, _call_with_retry,
                             _is_fatal_quota, describe_llm_error)


def test_llm_service_lazy_client():
    """LLMService 实例化不应触发 openai 导入（配置惰性，_get_client 首次调用才 import）。"""
    import sys
    before = set(sys.modules.keys())
    svc = LLMService()
    after = set(sys.modules.keys())
    assert "openai" not in after - before
    assert svc._clients == {}   # 按协议懒建，构造时不创建 client


def _make_err(name, status=None):
    """造一个类名为 name、可选 status_code 的异常实例。"""
    cls = type(name, (Exception,), {})
    e = cls()
    if status is not None:
        e.status_code = status
    return e


def test_is_retryable_transient_rate_limit():
    """普通限流（无 quota 字样）可恢复——重试。"""
    assert _is_retryable(_make_err("RateLimitError", status=429)) is True


def test_is_retryable_timeout():
    assert _is_retryable(_make_err("APITimeoutError")) is True
    assert _is_retryable(TimeoutError("超时")) is True


def test_is_retryable_5xx():
    assert _is_retryable(_make_err("APIError", status=503)) is True
    assert _is_retryable(_make_err("APIError", status=500)) is True

def test_not_retryable_auth_and_4xx():
    assert _is_retryable(_make_err("AuthenticationError")) is False
    assert _is_retryable(_make_err("BadRequestError", status=400)) is False
    assert _is_retryable(_make_err("NotFoundError", status=404)) is False


def test_quota_error_not_retryable_even_if_429():
    """insufficient_quota 虽返 429 但不可恢复——不重试，走人话文案。"""
    # openai SDK 的 RateLimitError 带错误体含 insufficient_quota
    class QuotaErr(Exception):
        pass
    QuotaErr.__name__ = "RateLimitError"
    e = QuotaErr("Error code: 429 - insufficient_quota: You exceeded your current quota")
    assert _is_fatal_quota(e) is True
    assert _is_retryable(e) is False


def test_transient_rate_limit_still_retryable():
    """普通限流（无 quota 字样）仍可恢复重试。"""
    class RateLimitErr(Exception):
        pass
    RateLimitErr.__name__ = "RateLimitError"
    e = RateLimitErr("Error code: 429 - Too many requests")
    assert _is_fatal_quota(e) is False
    assert _is_retryable(e) is True


def test_describe_quota_human_readable():
    class QuotaErr(Exception):
        pass
    QuotaErr.__name__ = "RateLimitError"
    e = QuotaErr("429 insufficient_quota exceeded quota")
    msg = describe_llm_error(e)
    assert "额度不足" in msg
    assert "insufficient_quota" not in msg   # 不透出技术词


def test_describe_auth_error():
    e = _make_err("AuthenticationError")
    assert "鉴权" in describe_llm_error(e)


def test_describe_timeout():
    assert "超时" in describe_llm_error(TimeoutError("read timeout"))


async def _run(coro):
    import asyncio
    return await coro


def test_retry_then_succeeds(monkeypatch):
    """可恢复错误重试后成功（sleep 被 monkeypatch 跳过，测试快）。"""
    import asyncio
    import src.llm.service as svc
    async def fast_sleep(d): return None
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _make_err("RateLimitError")
        return "ok"

    r = asyncio.run(_call_with_retry(flaky, base_delay=0))
    assert r == "ok"
    assert calls["n"] == 3


def test_not_retryable_raises_immediately(monkeypatch):
    """不可恢复错误（鉴权）立即抛，不重试。"""
    import asyncio
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise _make_err("AuthenticationError")

    import pytest
    with pytest.raises(Exception):
        asyncio.run(_call_with_retry(boom, base_delay=0))
    assert calls["n"] == 1


def test_retry_exhausted_raises(monkeypatch):
    """一直可恢复错误到 max_retries 用尽，最终抛出。"""
    import asyncio
    async def fast_sleep(d): return None
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    async def always_fail():
        raise _make_err("InternalServerError")

    import pytest
    with pytest.raises(Exception):
        asyncio.run(_call_with_retry(always_fail, max_retries=2, base_delay=0))


# ===== P2 限流：jitter / retry-after / RPM 窗 / 并发闸 =====

def test_retry_after_seconds_numeric_and_none():
    """retry-after 数字秒能抠出来；无响应/无头返回 None。"""
    from src.llm.service import _retry_after_seconds

    class Err(Exception):
        pass
    e = Err()
    e.response = type("R", (), {"headers": {"retry-after": "30"}})()
    assert _retry_after_seconds(e) == 30.0

    e2 = Err()  # 无 response
    assert _retry_after_seconds(e2) is None
    e3 = Err()
    e3.response = type("R", (), {"headers": {}})()
    assert _retry_after_seconds(e3) is None


def test_retry_respects_retry_after_header(monkeypatch):
    """retry-after(10s) 大于退避(0.5s) 时，sleep 取 max → 10s。"""
    import asyncio
    import src.llm.service as svc

    slept = []
    async def rec(d): slept.append(d)
    monkeypatch.setattr(asyncio, "sleep", rec)
    monkeypatch.setattr(svc.random, "random", lambda: 0.0)   # jitter 固定 → delay*0.5

    class Err(Exception):
        pass
    Err.__name__ = "RateLimitError"
    e = Err()
    e.response = type("R", (), {"headers": {"retry-after": "10"}})()

    calls = {"n": 0}
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise e
        return "ok"

    asyncio.run(svc._call_with_retry(flaky, base_delay=1.0))
    # 退避 1*2^0=1，jitter*0.5=0.5，retry-after=10 → max=10
    assert slept == [10.0]


def test_retry_jitter_applied(monkeypatch):
    """退避乘 (0.5+random)；random=1.0 时 delay=退避*1.5。"""
    import asyncio
    import src.llm.service as svc

    slept = []
    async def rec(d): slept.append(d)
    monkeypatch.setattr(asyncio, "sleep", rec)
    monkeypatch.setattr(svc.random, "random", lambda: 1.0)   # delay*1.5

    class Err(Exception):
        pass
    Err.__name__ = "RateLimitError"
    calls = {"n": 0}
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise Err()
        return "ok"

    asyncio.run(svc._call_with_retry(flaky, base_delay=2.0))
    # 退避 2*1=2，jitter*1.5=3.0，无 retry-after
    assert slept == [3.0]


def test_throttle_rpm_limit_sleeps_when_full(monkeypatch):
    """rpm_limit=2：前两次不 sleep，第三次窗口满应 sleep。"""
    import asyncio
    import src.llm.service as svc

    svc_inst = svc.LLMService()
    svc_inst._apply_rate_limit(rpm_limit=2, concurrency=None)

    slept = []
    async def rec(d): slept.append(d)
    monkeypatch.setattr(asyncio, "sleep", rec)

    asyncio.run(svc_inst._throttle())   # 1/2
    asyncio.run(svc_inst._throttle())   # 2/2
    assert slept == []
    asyncio.run(svc_inst._throttle())   # 满 → sleep
    assert len(slept) == 1 and slept[0] > 0


def test_apply_rate_limit_builds_semaphore():
    """concurrency=N 建 Semaphore；None 不建；rpm_limit 记下供 _throttle 用。"""
    import asyncio
    from src.llm.service import LLMService

    s = LLMService()
    s._apply_rate_limit(rpm_limit=None, concurrency=3)
    assert isinstance(s._sem, asyncio.Semaphore)

    s2 = LLMService()
    s2._apply_rate_limit(rpm_limit=10, concurrency=None)
    assert s2._sem is None
    assert s2._rpm_limit == 10
