"""Agent 编排核心：自主同步 ReAct 循环（spec 6.1）。
LLM→解析 tool_calls→逐工具执行→摘要回灌→重复，直到无 tool_calls / finish / 护栏 / 取消。
- ask_user 工具返回 suspended=True → 委托 SessionState.suspend 存 checkpoint + 发 clarification + return
- finish 工具返回 finished=True → 终止循环
- 护栏：max_turns / 重复调用检测 / ask_user 次数上限
- 取消令牌三处检查点：轮前 / 工具前 / 工具内（透传 cancel_token 给 registry.execute）"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from src.core.intent import is_doc_question
from src.core.session import SessionState, SessionStatus
from src.core.types import CancelToken, LoopContext, SSEEvent, ToolResult
from src.llm.service import LLMService
from src.logging import get_logger
from src.tools.registry import ToolRegistry

log = get_logger(__name__)

ASK_USER = "ask_user"


def _args_key(args: dict) -> str:
    """dict 参数稳定序列化，用于重复调用检测。"""
    return json.dumps(args, sort_keys=True, ensure_ascii=False)


def _sql_failed(summary: str) -> bool:
    """execute_sql 结果是否算失败（空结果或报错），供试错熔断计数。
    成功时 summary 是 JSON（{"result_id":..,"rows":N,...}）；失败时是纯文本错误兜底。
    成功看 rows==0；非 JSON 一律算失败（execute_sql 失败必走文本兜底）。"""
    if not summary:
        return True
    if summary.startswith("{"):
        try:
            return int(json.loads(summary).get("rows", -1)) == 0
        except (ValueError, TypeError):
            return False
    return True


def _sql_extract(sql: str) -> tuple[frozenset, frozenset, frozenset]:
    """从 SQL 抽 (tables, where_predicates, base_cols)。
    where_predicates: AND 连接的 WHERE 条件拆成集合（顺序无关，规范化空格），
    比对字符串更稳——同条件不同顺序/空格不漏判。
    base_cols: 引用的 业务表.列（排除 CROSS JOIN 指标字典列 ind.*）。解析失败返空集。"""
    import sqlglot
    from sqlglot import exp
    try:
        st = sqlglot.parse(sql)[0]
    except Exception:
        return frozenset(), frozenset(), frozenset()
    if not isinstance(st, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return frozenset(), frozenset(), frozenset()
    tables = frozenset(t.name for t in st.find_all(exp.Table))
    preds = set()
    wh = st.find(exp.Where)
    if wh and wh.this:
        def walk(e):
            if isinstance(e, exp.And):
                walk(e.left)
                walk(e.right)
            else:
                preds.add(" ".join(e.sql().split()))
        walk(wh.this)
    cols = set()
    for col in st.find_all(exp.Column):
        tbl = col.table or ""
        name = col.name or ""
        if tbl and tbl != "ind":
            cols.add(f"{tbl}.{name}")
    return tables, frozenset(preds), frozenset(cols)


def _sql_redundant(sql: str, prev: list[tuple[frozenset, frozenset, frozenset]]) -> bool:
    """检测 execute_sql 是否冗余：查的表 + WHERE 谓词集 完全等于已执行过的一条，
    且本次引用的业务列 ⊆ 那条已查回的列 → 覆盖，判定冗余。
    WHERE 用谓词集比对（顺序无关）；列用子集判定。解析失败/无列不判（宁漏不误杀）。"""
    tables, preds, cols = _sql_extract(sql)
    if not tables or not cols:
        return False
    for (pt, pp, pc) in prev:
        if tables == pt and preds == pp and cols <= pc:
            return True
    return False


def _normalize_args(raw) -> dict:
    """LLM 返回的 args 可能是 str，归一化成 dict。None/list/int 等兜底成 {}。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw else {}
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _to_openai_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """把 langchain 格式 [{name,args,id}] 转成 OpenAI 消息格式，供下一轮 langchain 识别。"""
    return [
        {"id": tc.get("id"), "type": "function",
         "function": {"name": tc.get("name"),
                      "arguments": json.dumps(tc.get("args", {}), ensure_ascii=False)}}
        for tc in tool_calls
    ]


class AgentLoop:
    """自主同步 ReAct 循环。run 为 async generator，末事件必为
    done/cancelled/error/clarification_needed 之一。"""

    def __init__(self, llm: LLMService, registry: ToolRegistry, state: SessionState,
                 *, max_turns: int = 30, max_ask_user: int = 2,
                 max_context: int | None = None,
                 max_sql: int = 10, max_sql_fail_streak: int = 3,
                 max_meta_per_run: int = 1,
                 session_manager=None, audit=None):
        self._llm = llm
        self._registry = registry
        self._state = state
        # 护栏集中存 _limits：admin 改 agent_limits 表后 reload_limits 热更新，无需重启。
        # 试错熔断（P0）：审计实证 LLM 单对话能跑 15 条 SQL 撞爆网关额度，必须拦。
        # max_context 不在此列——它走 LLM 配置，运行时自取（见 _context_limit）。
        self._limits: dict[str, int] = {
            "max_turns": max_turns,            # 最大推理轮数
            "max_ask_user": max_ask_user,      # 单轮最多追问几次
            "max_sql": max_sql,                # 单次对话 execute_sql 硬上限
            "max_sql_fail_streak": max_sql_fail_streak,  # 连续空/错几次提示 LLM 收手
            "max_meta_per_run": max_meta_per_run,        # query_metadata 每轮最多查几次
        }
        # 压缩阈值（token，逼近即压）。对齐 Claude Code：window 即阈值，绝对值非占比。
        # None=运行时读 LLM 配置/环境变量 max_context，拿不到用 32000 兜底。
        self._max_context = max_context
        # 会话历史读写：注入则用，不注入则降级无记忆（兼容旧测试 FakeLLM 不传）
        self._sessions = session_manager
        # 审计落库器：注入则每事件落 audit_events，不注入则跳过（测试可关）
        self._audit = audit

    def reload_limits(self, limits: dict) -> None:
        """admin 改 agent_limits 表后调：只更新 max_ 开头的护栏，立即生效（不重启）。"""
        for k, v in limits.items():
            if k.startswith("max_") and v is not None:
                self._limits[k] = v

    def reload_registry(self, registry: "ToolRegistry") -> None:
        """admin 改 skill（content/enabled/tools）后调：热替换工具集，立即生效（不重启）。
        与 reload_limits 同构——DB 改完重建 registry，替换 self._registry，下一轮 openai_tools 即新。"""
        self._registry = registry
        names = [td.name for td in registry.available_defs()]
        log.info("工具集热刷新完成 active=%s", names)

    async def run(self, session_id: str, user_id: str, user_msg: str,
                  trace_id: str, cancel_token: CancelToken,
                  is_resume: bool = False,
                  system_prompt: str | None = None) -> AsyncIterator[SSEEvent]:
        ctx = LoopContext(session_id, user_id, trace_id)
        msgs = await self._prepare_messages(session_id, user_msg, is_resume,
                                            system_prompt)
        # 审计 trace 开始（注入了才落）：记原始输入+启动计时
        if self._audit is not None:
            try:
                self._audit.begin(trace_id, session_id, user_id, user_msg,
                                  getattr(msgs[-1], "content", None) if msgs else None)
            except Exception as ex:
                log.warning("审计 begin 失败（忽略）: %s", ex)

        last_answer = ""
        ask_count = 0
        prev_keys: set[tuple[str, str]] = set()
        prev_sql: list[tuple[frozenset, frozenset, frozenset]] = []  # 已执行 SQL 的 (表,WHERE谓词,列)，查重用
        # 试错熔断计数器（P0）：单 run 内 query_metadata/execute_sql 调用数与连续失败统计
        meta_calls = 0
        sql_calls = 0
        sql_fail_streak = 0
        kb_searched = False   # 本轮是否已调过 knowledge_search（文档类护栏用）
        _doc_intent = is_doc_question(user_msg)  # 用户问题是否文档类（只判一次）
        turn = 0
        try:
            for turn in range(self._limits["max_turns"]):
                cancel_token.check()
                yield SSEEvent("turn_start", {"turn": turn}, trace_id)
                # 流式收 content（发 answer_delta 打字机）+ collect tool_calls 增量
                content = ""
                tc_acc: dict = {}
                async for chunk in self._llm.chat_stream(msgs, self._registry.openai_tools()):
                    cancel_token.check()
                    if chunk.content:
                        content += chunk.content
                        yield SSEEvent("answer_delta", {"text": chunk.content}, trace_id)
                        self._audit_event("answer_delta", {"text": chunk.content}, trace_id, turn)
                    # 思考链：推理模型在工具决策轮吐 reasoning_content（content 为空）。
                    # 单独发 reasoning_delta——飞书侧流式打字进专属"思考"元素，破除工具阶段静默。
                    if chunk.reasoning:
                        yield SSEEvent("reasoning_delta", {"text": chunk.reasoning}, trace_id)
                        self._audit_event("reasoning_delta", {"text": chunk.reasoning}, trace_id, turn)
                    for tc in chunk.tool_call_delta:
                        idx = getattr(tc, "index", None)
                        idx = idx if idx is not None else 0
                        acc = tc_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if getattr(tc, "id", None):
                            acc["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        # anthropic 路径的 _AnthropicTCDelta 没有 .function，回退到顶层 name/args
                        if fn:
                            if fn.name:
                                acc["name"] = fn.name
                            if fn.arguments:
                                acc["args"] += fn.arguments
                        else:
                            if getattr(tc, "name", None):
                                acc["name"] = tc.name
                            if getattr(tc, "args", None):
                                acc["args"] += tc.args
                tool_calls = []
                for idx in sorted(tc_acc):
                    v = tc_acc[idx]
                    try:
                        args = json.loads(v["args"]) if v["args"] else {}
                    except Exception:
                        args = {}
                    tool_calls.append({"id": v["id"], "name": v["name"], "args": args})
                if content.strip():  # 纯空白（如 "\n\n"）不算有效答案，避免覆盖前面已查到的结论
                    last_answer = content
                msgs.append({"role": "assistant", "content": content,
                             "tool_calls": _to_openai_tool_calls(tool_calls)
                             if tool_calls else []})

                if not tool_calls:
                    break

                cur_keys = {(tc.get("name"),
                             _args_key(_normalize_args(tc.get("args"))))
                            for tc in tool_calls}
                dup_keys = cur_keys & prev_keys
                prev_keys = cur_keys

                finished = False
                for tc in tool_calls:
                    cancel_token.check()
                    name = tc.get("name")
                    args = _normalize_args(tc.get("args"))
                    cid = tc.get("id")
                    sql_text = args.get("sql", "") if name == "execute_sql" else ""
                    # 重复调用（同工具+参数）：不显示也不执行，直接回 tip 让 LLM 用已有结果——
                    # 否则前端/飞书会看到重复的"执行查询"步骤
                    if (name, _args_key(args)) in dup_keys:
                        tip = "已调用过相同工具和参数，请基于已有结果直接作答。"
                        msgs.append({"role": "tool", "tool_call_id": cid,
                                     "content": tip})
                        self._audit_event("tool_result", {"name": name, "summary": tip,
                                                          "converged": True}, trace_id, turn)
                        continue
                    yield SSEEvent("tool_call",
                                   {"name": name, "args": args, "id": cid}, trace_id)
                    self._audit_event("tool_call", {"name": name, "args": args, "id": cid}, trace_id, turn)

                    if name == ASK_USER:
                        ask_count += 1
                        if ask_count > self._limits["max_ask_user"]:
                            tip = "已达询问次数上限，请基于已有信息直接给出答案。"
                            msgs.append({"role": "tool", "tool_call_id": cid,
                                         "content": tip})
                            yield SSEEvent("tool_result",
                                           {"name": name, "summary": tip}, trace_id)
                            self._audit_event("tool_result", {"name": name, "summary": tip}, trace_id, turn)
                            continue

                    # 试错熔断（P0）：query_metadata 每轮只查1次；execute_sql 硬上限。
                    # 防 LLM 反复试探烧光网关额度（审计实证：单对话 15 条 SQL 撞爆配额）。
                    if name == "query_metadata":
                        meta_calls += 1
                        if meta_calls > self._limits["max_meta_per_run"]:
                            tip = "元数据本轮已查过，直接用上面返回的表清单，不要重复调 query_metadata。"
                            msgs.append({"role": "tool", "tool_call_id": cid, "content": tip})
                            yield SSEEvent("tool_result",
                                           {"name": name, "summary": tip, "converged": True}, trace_id)
                            self._audit_event("tool_result", {"name": name, "summary": tip}, trace_id, turn)
                            continue
                    elif name == "execute_sql":
                        # 文档类护栏（代码层硬保障，不赌 LLM 判断）：
                        # 用户问的是文档/制度/资料/移交缺陷等（is_doc_question），但 LLM 却去查数据表，
                        # 且本轮还没调过 knowledge_search → 拦下 execute_sql，强制先查知识库。
                        # 审计实证：trace 2ef130f57334，"禾枫移交生产的缺陷"被当项目名模糊匹配 11 次。
                        # 拦一次即可（LLM 看到提示会改调 knowledge_search）；若 LLM 仍执意查数据，
                        # 下面的 max_sql 护栏兜底，不会无限跑。
                        if _doc_intent and not kb_searched:
                            tip = ("这是文档/资料类问题，不该查业务数据表。"
                                   "请先调 knowledge_search 查相关文档（如移交资料/验收文档/缺陷清单），"
                                   "基于文档片段回答；不要用 execute_sql 模糊匹配实体名。")
                            msgs.append({"role": "tool", "tool_call_id": cid, "content": tip})
                            yield SSEEvent("tool_result",
                                           {"name": name, "summary": tip, "converged": True}, trace_id)
                            self._audit_event("tool_result", {"name": name, "summary": tip,
                                                              "converged": True, "guard": "doc_intent"}, trace_id, turn)
                            continue
                        sql_limit = self._limits["max_sql"]
                        if sql_calls >= sql_limit:
                            tip = f"已达查询上限（{sql_limit} 次），停止继续查，基于已有结果直接回答用户。"
                            msgs.append({"role": "tool", "tool_call_id": cid, "content": tip})
                            yield SSEEvent("tool_result",
                                           {"name": name, "summary": tip, "converged": True}, trace_id)
                            self._audit_event("tool_result", {"name": name, "summary": tip}, trace_id, turn)
                            continue
                        sql_calls += 1
                        # 冗余查询检测：同表+同WHERE+列已被覆盖 → 不执行，回灌复用提示。
                        # 防 LLM 查完得分又 SELECT 同行同批列、或把 unpivot 原样重跑一遍。
                        if sql_text and _sql_redundant(sql_text, prev_sql):
                            tip = ("这条 SQL 查的表和筛选条件与上面已执行过的完全相同，所需列也已在上方结果里。"
                                   "不要重查——直接读上方已有的工具结果作答。")
                            msgs.append({"role": "tool", "tool_call_id": cid, "content": tip})
                            yield SSEEvent("tool_result",
                                           {"name": name, "summary": tip, "converged": True}, trace_id)
                            self._audit_event("tool_result", {"name": name, "summary": tip}, trace_id, turn)
                            continue

                    result = await self._registry.execute(name, args, ctx, cancel_token)
                    # 文档类护栏：标记本轮已查过知识库（解上面的 execute_sql 拦截）
                    if name == "knowledge_search":
                        kb_searched = True
                    # 记录本次 SQL 的 (表,WHERE谓词,列) 供后续冗余检测（只记成功的）
                    if name == "execute_sql" and not _sql_failed(result.summary):
                        _t, _p, _c = _sql_extract(sql_text)
                        if _t and _c:
                            prev_sql.append((_t, _p, _c))

                    # execute_sql 连续空/错熔断（P0）：提示 LLM 收手，别闷头试到口径都乱。
                    fail_hint = ""
                    if name == "execute_sql":
                        sql_fail_streak = sql_fail_streak + 1 if _sql_failed(result.summary) else 0
                        if sql_fail_streak >= self._limits["max_sql_fail_streak"]:
                            fail_hint = ("\n\n【系统提示】连续多次查询无结果，很可能是筛选口径不对"
                                         "（时间范围/取值/字段名）。停止重试，基于已有信息作答"
                                         "或直接说明'未查到符合条件的数据'。")

                    if result.suspended:
                        # ask_user 挂起：本轮不 append tool 消息——
                        # SessionState.resume 唯一负责注入用户回答，避免重复 tool_call_id
                        await self._state.suspend(session_id, msgs,
                                                  pending_tool=cid)
                        yield SSEEvent("clarification_needed",
                                       {"question": result.summary, "turn": turn,
                                        "options": result.options}, trace_id)
                        self._audit_event("clarification_needed",
                                          {"question": result.summary, "turn": turn,
                                           "options": result.options}, trace_id, turn)
                        return

                    final_summary = result.summary + fail_hint
                    msgs.append({"role": "tool", "tool_call_id": cid,
                                 "content": final_summary})
                    yield SSEEvent("tool_result",
                                   {"name": name, "summary": final_summary,
                                    "result_id": result.result_id}, trace_id)
                    self._audit_event("tool_result", {"name": name, "summary": final_summary,
                                                      "result_id": result.result_id}, trace_id, turn)

                    if result.finished:
                        # finish 的最终答案以 LLM 给的 args.answer 为准（spec 6.2），
                        # summary 仅作兜底——内置 _finish 实际就把 args.answer 填进 summary
                        last_answer = args.get("answer") or result.summary
                        finished = True
                        break

                if finished:
                    break

                await self._maybe_compress(msgs)
            else:
                # max_turns 用尽仍未 finish：别返回空答案——保住前面已得到的内容；全空则明确提示
                turns_limit = self._limits["max_turns"]
                if not last_answer.strip():
                    last_answer = f"已达最大推理轮数（{turns_limit} 轮），未能生成完整结论，请缩小问题范围或重试。"
                yield SSEEvent("warning",
                               {"reason": "max_turns", "max": turns_limit},
                               trace_id)
                self._audit_event("warning", {"reason": "max_turns", "max": self._limits["max_turns"]}, trace_id, turn)

            # 正常结束（done）：把本轮 user + 最终答案回写会话历史，供下轮多轮记忆
            await self._persist_history(session_id, user_msg, last_answer, trace_id)
            await self._audit_finalize(True, last_answer, trace_id)

            await self._state.transition(session_id, SessionStatus.DONE)
            yield SSEEvent("done", {"answer": last_answer}, trace_id)
        except asyncio.CancelledError:
            log.info("agent loop 被取消 sid=%s turn=%s", session_id, turn)
            await self._state.transition(session_id, SessionStatus.IDLE)
            # 取消也回写：至少存上用户问了啥，切会话能看到
            await self._persist_history(session_id, user_msg, last_answer or "（已取消）", trace_id)
            await self._audit_finalize(False, last_answer or "（已取消）", trace_id)
            yield SSEEvent("cancelled", {"turn": turn}, trace_id)
        except Exception as e:
            log.exception("agent loop 异常 sid=%s", session_id)
            # 异常也回写：存上用户问题 + 错误文案，切会话能看到"问过啥"
            from src.llm.service import describe_llm_error
            err_text = describe_llm_error(e)
            await self._persist_history(session_id, user_msg, f"⚠ {err_text}", trace_id)
            await self._audit_finalize(False, f"⚠ {err_text}", trace_id)
            try:
                await self._state.transition(session_id, SessionStatus.ERROR)
            except ValueError:
                log.warning("异常处理时状态转换失败，忽略: sid=%s", session_id)
            yield SSEEvent("error",
                           {"message": err_text, "answer": last_answer},
                           trace_id)

    def _audit_event(self, evt_type: str, data: dict, trace_id: str,
                     turn: int | None = None) -> None:
        """把单个事件喂给审计落库器（注入了才落）。answer_delta 合并、失败不抛。"""
        if self._audit is None:
            return
        try:
            self._audit.event(trace_id, evt_type, data, turn)
        except Exception as ex:
            log.warning("审计记事件失败（忽略）: %s", ex)

    async def _audit_finalize(self, success: bool, final_answer: str,
                              trace_id: str) -> None:
        """trace 结束落汇总行+事件流。失败不抛。"""
        if self._audit is None:
            return
        try:
            await self._audit.finalize(success, final_answer, trace_id)
        except Exception as ex:
            log.warning("审计落库失败（忽略）: %s", ex)

    async def _persist_history(self, session_id: str, user_msg: str,
                               assistant_msg: str, trace_id: str) -> None:
        """把本轮 user + assistant 回写会话历史，供多轮记忆 + 切会话回填。
        done/error/cancelled 三种结束都调，保证切会话至少能看到"问过啥+答/错"。
        失败不抛（历史写挂了不该毁掉对话主链路）。"""
        if self._sessions is None:
            return
        try:
            await self._sessions.append_message(
                session_id, "user", user_msg, trace_id)
            await self._sessions.append_message(
                session_id, "assistant", assistant_msg, trace_id)
        except Exception as e:
            log.warning("回写会话历史失败，忽略: sid=%s %s", session_id, e)

    async def _prepare_messages(self, session_id: str, user_msg: str,
                                is_resume: bool,
                                system_prompt: str | None = None) -> list[dict]:
        if is_resume:
            rc = await self._state.resume(session_id, user_msg)
            if rc is not None:
                return rc.messages
            log.warning("resume 无 checkpoint，降级新会话: sid=%s", session_id)
        await self._state.transition(session_id, SessionStatus.RUNNING)
        msgs: list[dict] = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        # 注入会话历史（不含本轮）：上一轮及更早的 user/assistant 文本回合。
        # resume 分支已返回带 checkpoint 的 messages（含历史+工具上下文），不走这里。
        if self._sessions is not None:
            hist = await self._sessions.get_messages(session_id)
            for m in hist:
                msgs.append({"role": m["role"], "content": m["content"]})
        msgs.append({"role": "user", "content": user_msg})
        return msgs

    async def _maybe_compress(self, msgs: list[dict]) -> None:
        """上下文逼近窗口阈值时压缩：中段按 group（user→assistant→tool）切分摘要。
        对齐 Claude Code auto-compact：绝对 token 阈值触发（非占比），按 group 切，
        LLM 摘要非截断，保留最近 group 不压。
        ponytail: 字符数粗估 token（1 token≈4 字符，中文偏 2）；拿不到阈值用 32000 默认。
        拿不到 LLM 摘要能力（FakeLLM 无 summarize/chat）时跳过，不毁对话。"""
        threshold_tokens = await self._resolve_max_context()
        threshold_chars = threshold_tokens * 4   # 粗估 4 字符/token
        total_chars = sum(len(m.get("content", "")) for m in msgs)
        if total_chars < threshold_chars:
            return
        # 按 group 切分：[user, assistant, (tool...)] 为一组。
        # 保留最近 2 个 group 不压（当前轮上下文），其余中段 group 喂摘要。
        groups = _split_into_groups(msgs)
        if len(groups) <= 3:   # 太少没东西可压（too_few_groups）
            return
        keep_groups = 2
        old_groups = groups[:-keep_groups]
        recent_groups = groups[-keep_groups:]
        # system 消息（最前的）原样保留；中段非 system group 喂摘要
        head = [m for m in old_groups[0] if m.get("role") == "system"] if old_groups else []
        # query_metadata 的结果不压：表结构/字段/表级规则是写 SQL 的依据，压成摘要会丢细节
        # → LLM 凭记忆把 years 猜成 LIKE '202%-%'。按 tool_call_id 关联出 query_metadata 的
        # tool result，所在 group 原样保留，只压其余中段。
        meta_call_ids = {tc.get("id") for g in old_groups for m in g if m.get("role") == "assistant"
                         for tc in (m.get("tool_calls") or [])
                         if (tc.get("function") or {}).get("name") == "query_metadata"}

        def _has_meta(g: list[dict]) -> bool:
            return any(m.get("tool_call_id") in meta_call_ids for m in g)
        body = [m for g in old_groups if not _has_meta(g) for m in g if m.get("role") != "system"]
        if not body:
            return   # 无可压内容（中段全是 query_metadata），原样保留即跳过
        keep_meta_flat = [m for g in old_groups if _has_meta(g) for m in g if m.get("role") != "system"]
        try:
            digest = await self._summarize_segment(body)
        except Exception as e:
            log.warning("会话压缩失败，跳过: %s", e)
            return
        summary_msg = {
            "role": "system",
            "content": (f"[对话摘要-压缩] 以下是早期对话的压缩摘要，供参考上下文：\n{digest}")
        }
        # 重组：head + 摘要 + query_metadata 结果（原样保留）+ 最近 group（展平）
        recent_flat = [m for g in recent_groups for m in g]
        msgs.clear()
        msgs.extend(head + [summary_msg] + keep_meta_flat + recent_flat)

    async def _resolve_max_context(self) -> int:
        """拿压缩触发阈值（token）：优先构造参数 → 环境变量 → LLM 配置 max_context → 32000。
        对齐 Claude Code：环境变量最高优先级（CLAUDE_CODE_MAX_CONTEXT_TOKENS 同构），
        内置表覆盖在用模型（DeepSeek/Qwen），查不到走配置，绝不瞎猜。"""
        import os
        env_ctx = os.environ.get("NL2SQL_MAX_CONTEXT")
        if env_ctx and env_ctx.isdigit():
            return int(env_ctx)
        if self._max_context is not None:
            return self._max_context
        try:
            cfg = await self._llm._resolve_config()   # 生产 LLMService 有此方法
            from_cfg = getattr(cfg, "max_context", 0) or 0
            # 优先按模型名查内置表（在用模型窗口），查不到用配置值，再查不到 32000
            from_table = _model_window(getattr(cfg, "model", ""))
            return from_table or from_cfg or 32000
        except Exception:
            return 32000

    async def _summarize_segment(self, segment: list[dict]) -> str:
        """把中段历史消息整体喂 LLM 摘要。对齐 Claude Code 摘要模板：
        聚焦①用户问了什么 ②做了什么 ③关键数据/表名/结论 ④未决事项。"""
        text = "\n".join(
            f"[{m.get('role')}] {m.get('content', '')}" for m in segment)
        if hasattr(self._llm, "summarize"):
            return await self._llm.summarize(text)
        prompt_msgs = [
            {"role": "system", "content": COMPACT_SUMMARY_PROMPT},
            {"role": "user", "content": text},
        ]
        resp = await self._llm.chat(prompt_msgs)
        return resp.content


# Claude Code 同款摘要模板（中文版）：聚焦四要素，去冗余保关键。
COMPACT_SUMMARY_PROMPT = (
    "你是对话摘要助手。把给定的多轮对话压缩成关键信息摘要，按以下四点组织：\n"
    "1) 用户问了什么（用户意图/追问）\n"
    "2) 做了什么（调了哪些工具、查了哪些表/字段、执行了什么SQL）\n"
    "3) 关键数据/结果（表名、字段名、数值、结论）\n"
    "4) 未决事项（还没回答的、待用户确认的）\n"
    "去掉冗余细节和重复行，只保留对话继续所需的关键信息。"
)


# 内置模型→窗口表（只覆盖在用模型，对齐 Claude Code：表小，查不到走配置，绝不瞎猜）。
# ponytail: 新模型按实际窗口补这两行即可，无需穷举。
_MODEL_WINDOWS: dict[str, int] = {
    "deepseek-v3": 64000, "deepseek-v4": 64000,
    "qwen3-32b": 32000, "qwen3-72b": 32000, "qwen2.5-72b": 131072,
}


def _model_window(model: str) -> int:
    """模型名→窗口。小写前缀匹配（覆盖版本后缀），查不到返回 0。"""
    if not model:
        return 0
    key = model.lower()
    for prefix, win in _MODEL_WINDOWS.items():
        if key.startswith(prefix):
            return win
    return 0


def _split_into_groups(msgs: list[dict]) -> list[list[dict]]:
    """把消息列表按对话轮切分：每个 group 以 user 开头，含其后 assistant+tool 序列。
    开头连续的非 user 消息（如 system）并入首个 group。对齐 Claude Code 的 group 切分。"""
    if not msgs:
        return []
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for m in msgs:
        if m.get("role") == "user" and cur:
            groups.append(cur)
            cur = [m]
        else:
            cur.append(m)
    if cur:
        groups.append(cur)
    return groups
