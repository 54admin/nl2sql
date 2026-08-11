"""核心共享类型。所有 P0b 子系统反向依赖此处，避免循环导入。
本模块零内部依赖（仅 stdlib），保证 tools/core/web 任意方向 import 不成环。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class CancelToken:
    """取消令牌：外部 cancel() 置位，loop/工具在检查点 check()。
    ponytail: bool 标志位足够，GIL 下单线程读写原子；跨进程取消 P1 再换 Redis 标志。"""
    _cancelled: bool = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def check(self) -> None:
        """检查点：被取消则抛 CancelledError，由 loop 外层捕获转 cancelled 事件。"""
        if self._cancelled:
            raise asyncio.CancelledError("agent loop 已取消")


@dataclass
class LoopContext:
    """传给工具的上下文。agent_loop 构造后透传给 registry.execute。"""
    session_id: str
    user_id: str
    trace_id: str
    channel: str = "web"


@dataclass
class ToolResult:
    """工具执行结果。
    - summary: 回灌给 LLM 的摘要文本（结果旁路：全量不在内）
    - result_id: 大结果旁路引用（P1 execute_sql 用，P0b 留接口）
    - finished: finish 工具置 True → loop 终止并把 summary 作为最终答案
    - suspended: ask_user 工具置 True → loop 挂起，由 SessionState 持久化 checkpoint
    """
    summary: str
    result_id: str | None = None
    finished: bool = False
    suspended: bool = False
    options: list | None = None   # ask_user 候选（[{label, description}]），前端渲染成按钮让用户选
    references: list = field(default_factory=list)   # 知识库引用来源：[{document, similarity, document_id, dataset_id, url}]，done 时聚合成 citations 供两端渲染


@dataclass
class SSEEvent:
    """loop 产出的事件。SSE 层按双模式过滤。type 用 str 保持序列化简单。"""
    type: str
    data: dict = field(default_factory=dict)
    trace_id: str = ""


# 工具处理器类型别名（供 handlers 标注，统一三参签名）
ToolHandler = Callable[[dict, LoopContext, CancelToken], Awaitable[ToolResult]]


@dataclass
class ToolDefinition:
    """工具统一接口。handler 接收 (args, ctx, cancel_token)。"""
    name: str
    description: str
    parameters: dict                       # JSON Schema
    handler: ToolHandler
    availability: Callable[[], bool] = field(default=lambda: True)
