"""内核控制流工具：finish（结束本轮）/ ask_user（澄清挂起）。

二者是 ReAct loop 的终止 / 挂起出口，不属任何具体 skill——所有会话恒启用
（见 catalog.KERNEL_TOOL_NAMES）。工具本身只置标志位，由 AgentLoop 观察后决定终止 / 挂起；
不持久化，保持 tools 包零存储依赖。
"""
from __future__ import annotations

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult


async def _finish(args: dict, ctx: LoopContext, cancel_token: CancelToken) -> ToolResult:
    """给出最终答案并结束本轮对话。agent_loop 观察 finished=True 后终止循环。"""
    return ToolResult(summary=args.get("answer", ""), finished=True)


async def _ask_user(args: dict, ctx: LoopContext, cancel_token: CancelToken) -> ToolResult:
    """向用户提问澄清。有候选时给 options（前端渲染按钮），用户选项或自定义。"""
    return ToolResult(summary=args.get("question", ""), suspended=True, options=args.get("options"))


FINISH = ToolDefinition(
    name="finish",
    description="给出最终答案并结束本轮对话。当不再需要调用其他工具时使用。",
    parameters={"type": "object",
                "properties": {"answer": {"type": "string", "description": "给用户的最终答案"}},
                "required": ["answer"]},
    handler=_finish,
)

ASK_USER = ToolDefinition(
    name="ask_user",
    description="向用户提问澄清。有明确候选时给 options（2-4 个，第一个推荐，用户选项或自定义）；没候选就只问 question。",
    parameters={"type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要问用户的问题"},
                    "options": {"type": "array", "description": "候选选项（2-4 个，第一个为推荐）；用户可选项或自定义",
                                "items": {"type": "object",
                                          "properties": {"label": {"type": "string"}, "description": {"type": "string"}},
                                          "required": ["label"]}}
                },
                "required": ["question"]},
    handler=_ask_user,
)
