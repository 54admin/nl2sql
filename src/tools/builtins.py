"""内置工具：echo(stub)/finish/ask_user（spec 6.2）。
finish/ask_user 只置标志位，由 AgentLoop 观察后决定终止/挂起。
工具本身不持久化，保持 tools 包零 P0a 依赖。"""
from __future__ import annotations

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.tools.attribution import ATTRIBUTION
from src.tools.knowledge_tool import KNOWLEDGE_SEARCH
from src.tools.metadata import QUERY_METADATA
from src.tools.registry import ToolRegistry
from src.tools.sql_engine import EXECUTE_SQL
from src.tools.sql_template import make_get_sql_template


async def _echo(args: dict, ctx: LoopContext, cancel_token: CancelToken) -> ToolResult:
    """回显输入文本（测试用 stub，演示工具调用链路）。"""
    return ToolResult(summary=f"echo: {args.get('text', '')}")


async def _finish(args: dict, ctx: LoopContext, cancel_token: CancelToken) -> ToolResult:
    """给出最终答案并结束本轮对话。agent_loop 观察 finished=True 后终止循环。"""
    return ToolResult(summary=args.get("answer", ""), finished=True)


async def _ask_user(args: dict, ctx: LoopContext, cancel_token: CancelToken) -> ToolResult:
    """向用户提问澄清。有候选时给 options（前端渲染按钮），用户选项或自定义。"""
    return ToolResult(summary=args.get("question", ""), suspended=True, options=args.get("options"))


ECHO = ToolDefinition(
    name="echo",
    description="回显输入文本（测试用 stub，演示工具调用链路）",
    parameters={"type": "object",
                "properties": {"text": {"type": "string", "description": "要回显的文本"}},
                "required": ["text"]},
    handler=_echo,
)

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


def default_registry(sql_template_desc: str | None = None) -> ToolRegistry:
    """注册 echo / finish / ask_user + query_metadata + execute_sql + knowledge_search + do_attribution + get_sql_template。
    sql_template_desc：启动时拼好的模板清单，注入 get_sql_template 的 description（LLM 据此预知模板）。"""
    reg = ToolRegistry()
    for td in (ECHO, FINISH, ASK_USER, QUERY_METADATA, EXECUTE_SQL, KNOWLEDGE_SEARCH, ATTRIBUTION):
        reg.register(td)
    reg.register(make_get_sql_template(sql_template_desc))
    return reg
