"""SSE 事件类型常量 + 双模式过滤 + 文本格式化（spec 6.8）。
SSEEvent 复用 core.types 的定义（统一类型，避免重复）。
type 用 str 保持序列化简单；SSEEventType Enum 仅作常量参考。"""
from __future__ import annotations

import json
from enum import Enum

from src.core.types import SSEEvent


class SSEEventType(str, Enum):
    """SSE 事件类型常量（spec 6.8 表 + AgentLoop 内部技术事件）。"""
    # 用户友好事件（user 模式可见）
    CORRECTION = "correction"
    CLARIFICATION_NEEDED = "clarification_needed"
    PLAN = "plan"
    TODO_UPDATE = "todo_update"
    INTERMEDIATE = "intermediate"
    ANSWER_DELTA = "answer_delta"
    DONE = "done"
    ERROR = "error"
    # 技术事件
    QUERY_PROGRESS = "query_progress"        # user 可见的进度感事件
    METADATA_LOOKUP = "metadata_lookup"      # 4 类技术细节之一
    SQL_GENERATED = "sql_generated"
    KNOWLEDGE_HIT = "knowledge_hit"
    ATTRIBUTION_STEP = "attribution_step"
    # AgentLoop 内部技术事件（admin 可见，user 隐藏）
    TURN_START = "turn_start"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    WARNING = "warning"
    CANCELLED = "cancelled"


class ViewerMode(str, Enum):
    ADMIN = "admin"
    USER = "user"


# user 模式隐藏的事件：4 类查询细节 + 6 类 loop 内部技术事件
# query_progress 不隐藏——给 user 进度感
_USER_HIDDEN = frozenset({
    SSEEventType.METADATA_LOOKUP.value,
    SSEEventType.SQL_GENERATED.value,
    SSEEventType.KNOWLEDGE_HIT.value,
    SSEEventType.ATTRIBUTION_STEP.value,
    SSEEventType.TURN_START.value,
    SSEEventType.ASSISTANT.value,
    SSEEventType.TOOL_CALL.value,
    SSEEventType.TOOL_RESULT.value,
    SSEEventType.WARNING.value,
    SSEEventType.CANCELLED.value,
})


def should_emit(event: SSEEvent, mode: ViewerMode) -> bool:
    """admin 全发；user 隐藏技术细节事件。"""
    if mode == ViewerMode.ADMIN:
        return True
    return event.type not in _USER_HIDDEN


def filter_event(event: SSEEvent, mode: ViewerMode) -> SSEEvent | None:
    """按模式过滤。hidden 返回 None；可见事件原样透传。
    ponytail: intermediate 用户模式精简规则 P2 再做。"""
    if not should_emit(event, mode):
        return None
    return event


def format_sse(event: SSEEvent) -> str:
    """格式化为 SSE 文本协议：event: <type>\ndata: <json>\n\n。
    ensure_ascii=False 防中文 Unicode 转义。"""
    payload = {"data": event.data, "trace_id": event.trace_id}
    return f"event: {event.type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
