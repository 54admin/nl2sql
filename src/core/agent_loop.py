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
                 *, max_turns: int = 10, max_ask_user: int = 2):
        self._llm = llm
        self._registry = registry
        self._state = state
        self._max_turns = max_turns
        self._max_ask_user = max_ask_user

    async def run(self, session_id: str, user_id: str, user_msg: str,
                  trace_id: str, cancel_token: CancelToken,
                  is_resume: bool = False,
                  system_prompt: str | None = None) -> AsyncIterator[SSEEvent]:
        ctx = LoopContext(session_id, user_id, trace_id)
        msgs = await self._prepare_messages(session_id, user_msg, is_resume,
                                            system_prompt)

        last_answer = ""
        ask_count = 0
        prev_keys: set[tuple[str, str]] = set()
        turn = 0
        try:
            for turn in range(self._max_turns):
                cancel_token.check()
                yield SSEEvent("turn_start", {"turn": turn}, trace_id)
                resp = await self._llm.chat(msgs, self._registry.openai_tools())
                tool_calls = getattr(resp, "tool_calls", None) or []
                content = getattr(resp, "content", "") or ""
                if content:
                    last_answer = content
                msgs.append({"role": "assistant", "content": content,
                             "tool_calls": _to_openai_tool_calls(tool_calls)
                             if tool_calls else []})
                yield SSEEvent("assistant", {"content": content, "turn": turn}, trace_id)

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
                    yield SSEEvent("tool_call",
                                   {"name": name, "args": args, "id": cid}, trace_id)

                    if (name, _args_key(args)) in dup_keys:
                        tip = "已调用过相同工具和参数，请基于已有结果直接作答。"
                        msgs.append({"role": "tool", "tool_call_id": cid,
                                     "content": tip})
                        yield SSEEvent("tool_result",
                                       {"name": name, "summary": tip,
                                        "converged": True}, trace_id)
                        continue

                    if name == ASK_USER:
                        ask_count += 1
                        if ask_count > self._max_ask_user:
                            tip = "已达询问次数上限，请基于已有信息直接给出答案。"
                            msgs.append({"role": "tool", "tool_call_id": cid,
                                         "content": tip})
                            yield SSEEvent("tool_result",
                                           {"name": name, "summary": tip}, trace_id)
                            continue

                    result = await self._registry.execute(name, args, ctx, cancel_token)

                    if result.suspended:
                        # ask_user 挂起：本轮不 append tool 消息——
                        # SessionState.resume 唯一负责注入用户回答，避免重复 tool_call_id
                        await self._state.suspend(session_id, msgs,
                                                  pending_tool=cid)
                        yield SSEEvent("clarification_needed",
                                       {"question": result.summary, "turn": turn},
                                       trace_id)
                        return

                    msgs.append({"role": "tool", "tool_call_id": cid,
                                 "content": result.summary})
                    yield SSEEvent("tool_result",
                                   {"name": name, "summary": result.summary,
                                    "result_id": result.result_id}, trace_id)

                    if result.finished:
                        # finish 的最终答案以 LLM 给的 args.answer 为准（spec 6.2），
                        # summary 仅作兜底——内置 _finish 实际就把 args.answer 填进 summary
                        last_answer = args.get("answer") or result.summary
                        finished = True
                        break

                if finished:
                    break

                self._maybe_compress(msgs)
            else:
                yield SSEEvent("warning",
                               {"reason": "max_turns", "max": self._max_turns},
                               trace_id)

            await self._state.transition(session_id, SessionStatus.DONE)
            yield SSEEvent("done", {"answer": last_answer}, trace_id)
        except asyncio.CancelledError:
            log.info("agent loop 被取消 sid=%s turn=%s", session_id, turn)
            await self._state.transition(session_id, SessionStatus.IDLE)
            yield SSEEvent("cancelled", {"turn": turn}, trace_id)
        except Exception as e:
            log.exception("agent loop 异常 sid=%s", session_id)
            try:
                await self._state.transition(session_id, SessionStatus.ERROR)
            except ValueError:
                log.warning("异常处理时状态转换失败，忽略: sid=%s", session_id)
            yield SSEEvent("error",
                           {"message": str(e), "answer": last_answer}, trace_id)

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
        msgs.append({"role": "user", "content": user_msg})
        return msgs

    def _maybe_compress(self, msgs: list[dict]) -> None:
        """ponytail: 占位。P1 接 token 计数按 80% 阈值压早期 tool 结果。"""
        return None
