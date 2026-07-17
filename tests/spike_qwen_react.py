"""Qwen3 自主 ReAct 稳定性 spike（spec 13 P0 末尾关键里程碑）。

手动跑：python -m tests.spike_qwen_react
验证三大能力：
  (1) 自主循环收敛——闲聊/取数 case 在 max_turns 内收到 done
  (2) ask_user 准确——缺参 case 触发 clarification_needed，注入回答后 resume 收敛
  (3) 错误自愈——execute_sql stub 首次返回坏表名错误，LLM 重试正确表后 finish

前置：config/application.yml llm 网关可达 + OPENAI_API_KEY 已设。
脚本不进 pytest 收集（spike_ 前缀非 test_）；test_spike_stats.py 只测 classify 纯函数。
spike 对 agent_loop 用 duck-typed 最小接口（run），接口未冻结时只改适配层。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SpikeCase:
    id: str
    text: str
    expect_finish: bool = True
    expect_ask_user: bool = False
    expect_heal: bool = False
    clarification_answer: str | None = None
    max_turns: int = 10


@dataclass
class CaseResult:
    case_id: str
    converged: bool
    asked: bool
    healed: bool
    turns: int
    final_text: str
    corrections: list[dict] = field(default_factory=list)
    error: str | None = None


def classify(events: list[dict]) -> tuple[bool, bool, bool]:
    """从事件流判定 (converged, asked, healed)。纯函数，无 IO，可单测。
    - converged: 收到 done
    - asked: 收到 clarification_needed
    - healed: error 后仍 done（出错后仍收敛 = 自愈）
    """
    types = [e.get("type") for e in events]
    converged = "done" in types
    asked = "clarification_needed" in types
    healed = "error" in types and "done" in types
    return converged, asked, healed


CASES: list[SpikeCase] = [
    SpikeCase("chat-1", "你好"),
    SpikeCase("chat-2", "你是谁？能做什么？"),
    SpikeCase("query-1", "查新疆分公司2026年6月发电量"),
    SpikeCase("query-2", "展示各分公司上月发电量对比"),
    SpikeCase("ask-1", "查发电量", expect_ask_user=True,
              clarification_answer="新疆分公司"),
    SpikeCase("ask-2", "对比发电量", expect_ask_user=True,
              clarification_answer="6月和5月对比"),
    SpikeCase("typo-1", "新疆省分公司发电量"),   # normalizer pass-through 不改
    SpikeCase("typo-2", "内蒙分公司风电量"),
    SpikeCase("heal-1", "查新疆分公司发电量", expect_heal=True),  # stub 首次坏表名
    SpikeCase("multi-1", "哪些分公司发电量最高？给出前3名"),
    SpikeCase("multi-2", "新疆分公司6月比5月多了多少？"),
    SpikeCase("chitchat-1", "今天天气怎么样？"),
]


def build_stub_registry():
    """构造 stub 工具注册表（query_metadata/execute_sql + P0b builtins）。
    execute_sql 在 heal-1 case 首次返回坏表名错误，模拟需自愈的场景。"""
    from src.core.types import CancelToken, ToolDefinition, ToolResult
    from src.tools.builtins import default_registry

    reg = default_registry()

    # stub query_metadata：返回固定表元数据
    async def _query_metadata(args, ctx, tk):
        return ToolResult(summary="表 power_output(分公司,月份,发电量MWh)；"
                                  "表 dim_branch(分公司id,分公司名,区域)")

    # stub execute_sql：heal-1 case 首次返回坏表名错误触发自愈
    _first_call = {"heal-1": True}

    async def _execute_sql(args, ctx, tk):
        if ctx.session_id == "spike-heal-1" and _first_call["heal-1"]:
            _first_call["heal-1"] = False
            return ToolResult(summary="错误：表 'power' 不存在。可用表：power_output, dim_branch")
        return ToolResult(summary="查询成功，返回 5 行（含新疆/华北/华东等分公司）",
                          result_id="r-spike")

    reg.register(ToolDefinition(
        name="query_metadata", description="查表/字段元数据，选表用",
        parameters={"type": "object",
                    "properties": {"keyword": {"type": "string"}},
                    "required": ["keyword"]},
        handler=_query_metadata))
    reg.register(ToolDefinition(
        name="execute_sql", description="执行 SQL 查询并返回摘要",
        parameters={"type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"]},
        handler=_execute_sql))
    return reg


async def run_one(case: SpikeCase, loop, normalizer, session_id: str) -> CaseResult:
    """跑单个 case，收集事件流，判定 converged/asked/healed。"""
    from src.core.types import CancelToken

    text, corrections = await normalizer.normalize(case.text)
    events: list[dict] = []
    turns = 0
    final_text = ""
    try:
        async for ev in loop.run(session_id, "spike-user", text,
                                 f"trace-{case.id}", CancelToken(), is_resume=False):
            events.append({"type": ev.type, "data": ev.data})
            if ev.type == "turn_start":
                turns = ev.data.get("turn", turns)
            if ev.type == "done":
                final_text = ev.data.get("answer", "")
            # ask_user 挂起后注入用户回答恢复
            if (ev.type == "clarification_needed" and case.clarification_answer):
                async for ev2 in loop.run(session_id, "spike-user",
                                          case.clarification_answer,
                                          f"trace-{case.id}", CancelToken(),
                                          is_resume=True):
                    events.append({"type": ev2.type, "data": ev2.data})
                    if ev2.type == "done":
                        final_text = ev2.data.get("answer", "")
    except Exception as e:
        return CaseResult(case.id, False, False, False, turns, final_text,
                          [c.__dict__ for c in corrections], str(e))
    converged, asked, healed = classify(events)
    return CaseResult(case.id, converged, asked, healed, turns, final_text,
                      [c.__dict__ for c in corrections])


def print_report(results: list[CaseResult]) -> None:
    """输出控制台表格 + 写 markdown 报告 + 落 jsonl 原始流。"""
    total = len(results)
    conv = sum(1 for r in results if r.converged)
    asked = sum(1 for r in results if r.asked)
    healed = sum(1 for r in results if r.healed)
    err_cases = [r for r in results if r.error]

    print(f"\n{'=' * 64}")
    print(f"Qwen3 自主 ReAct spike 报告（共 {total} case，基于 stub 工具）")
    print(f"{'=' * 64}")
    print(f"{'case':<12} {'收敛':>5} {'ask':>5} {'自愈':>5} {'轮数':>5} {'最终答案':<24}")
    for r in results:
        mark_c = "是" if r.converged else "否"
        mark_a = "是" if r.asked else "-"
        mark_h = "是" if r.healed else "-"
        print(f"{r.case_id:<12} {mark_c:>5} {mark_a:>5} {mark_h:>5} "
              f"{r.turns:>5} {r.final_text[:24]:<24}")
    print(f"\n收敛率: {conv}/{total}  ask_user 触发: {asked}  自愈: {healed}")
    if err_cases:
        print(f"异常 case: {[r.case_id for r in err_cases]}")

    # 写 markdown 报告
    out_dir = Path(__file__).parent / "spike_output"
    out_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    lines = [f"# Qwen3 spike 报告 {ts}", "",
             f"收敛 {conv}/{total}，ask_user {asked}，自愈 {healed}", ""]
    for r in results:
        lines.append(f"- **{r.case_id}**: conv={r.converged} asked={r.asked} "
                     f"healed={r.healed} turns={r.turns} "
                     f"final=`{r.final_text[:40]}` err={r.error}")
    (out_dir / f"report-{ts}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写：{out_dir / f'report-{ts}.md'}")


async def main():
    """spike 主入口：连真 Qwen3 网关跑全部 case。"""
    try:
        from src.config import load_config
        from src.core.agent_loop import AgentLoop
        from src.core.normalizer import Normalizer
        from src.core.session import SessionState
        from src.llm.service import LLMService
        from src.memory.session import SessionManager
        from src.storage.pg_client import init_db
        from src.storage.redis_client import RedisClient
    except ImportError as e:
        print(f"[spike] 依赖未就绪：{e}。请先完成 P0b 其他子系统。")
        return

    cfg = load_config("config")
    await init_db("sqlite+aiosqlite:///:memory:")
    redis = RedisClient(cfg.redis)
    await redis.connect()
    mgr = SessionManager(redis)
    state = SessionState(mgr)
    llm = LLMService()
    registry = build_stub_registry()
    loop = AgentLoop(llm, registry, state, max_turns=10)
    normalizer = Normalizer()  # P0b pass-through

    results = []
    # ponytail: spike 需要固定可预测的 session_id（execute_sql stub 按 sid 识别
    # heal-1 case），故绕过 create_session 的 uuid，直接 ORM 插入指定 id 的 Session 行。
    # 这保证 sid 入库，后续 transition(RUNNING/DONE/...) 不会因"会话不存在"抛错。
    from datetime import datetime, timedelta, timezone
    from src.storage.models import Session as SessionRow
    from src.storage.pg_client import AsyncSessionFactory

    for c in CASES:
        sid = f"spike-{c.id}"
        # 直接 ORM 插入指定 id 的 Session（绕过 create_session 的 uuid）
        async with AsyncSessionFactory() as s:
            existing = await s.get(SessionRow, sid)
            if existing is None:
                s.add(SessionRow(
                    id=sid, user_id="spike-user",
                    channel="web", status="idle",
                    ttl_at=datetime.now(timezone.utc) + timedelta(hours=1)))
                await s.commit()
        try:
            result = await asyncio.wait_for(
                run_one(c, loop, normalizer, sid), timeout=120)
        except asyncio.TimeoutError:
            result = CaseResult(c.id, False, False, False, 0, "", [],
                                "timeout 120s")
        results.append(result)
    print_report(results)


if __name__ == "__main__":
    asyncio.run(main())
