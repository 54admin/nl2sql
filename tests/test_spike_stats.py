"""spike 统计逻辑单测（不连真网关，只测 classify 纯函数）。"""
from tests.spike_react import classify


def test_classify_converged():
    """收到 done = 收敛。"""
    evs = [{"type": "intermediate"}, {"type": "done", "data": {"answer": "x"}}]
    assert classify(evs) == (True, False, False)


def test_classify_ask_user_then_done():
    """clarification_needed 后 resume 收敛 = asked。"""
    evs = [{"type": "clarification_needed"},
           {"type": "done", "data": {"answer": "x"}}]
    assert classify(evs) == (True, True, False)


def test_classify_heal_after_error():
    """error 后仍 done = 自愈。"""
    evs = [{"type": "error", "data": {"stage": "execute_sql"}},
           {"type": "intermediate"},
           {"type": "done", "data": {"answer": "x"}}]
    assert classify(evs) == (True, False, True)


def test_classify_not_converged():
    """max_turns 耗尽无 done = 未收敛。"""
    evs = [{"type": "intermediate"}, {"type": "warning"}]
    assert classify(evs) == (False, False, False)


def test_classify_empty_events():
    assert classify([]) == (False, False, False)


def test_classify_ask_without_done_not_converged():
    """clarification_needed 但无 resume/done = 未收敛（挂起未补答）。"""
    evs = [{"type": "clarification_needed"}]
    assert classify(evs) == (False, True, False)
