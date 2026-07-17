"""工具注册表：动态 schema 重建 + coerce 强转 + 可用性过滤（spec 6.2）。
- openai_tools: 每次按可用集合现算，防 模型 幻觉调隐藏工具（spec 6.2.2）
- coerce_tool_args: 按 JSON Schema 强转 LLM 字符串参数（spec 6.2.3）
- execute: 错误兜底回灌 LLM 触发错误自愈（spec 6.1）
ponytail: 工具数 ≤10，openai_tools 不缓存；规模上来按版本号失效。"""
from __future__ import annotations

import importlib
import json
from typing import Any, Callable

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.logging import get_logger

log = get_logger(__name__)


def _first_non_null_type(t: Any) -> str:
    """JSON Schema type 可能是 'integer' 或 ['integer','null']，统一取首个非 null。"""
    if isinstance(t, list):
        return next((x for x in t if x != "null"), "string")
    return t


def coerce_tool_args(parameters: dict, args: dict) -> dict:
    """按 JSON Schema 强转 LLM 返回的字符串参数（spec 6.2.3）。
    模型 偶尔把 integer/number/boolean/array/object 返回成字符串，统一兜底。
    union type 如 ["string","null"] 取首个非 null。强转失败保留原值，不抛异常。"""
    props = parameters.get("properties", {})
    out = dict(args)
    for key, val in list(out.items()):
        if not isinstance(val, str):
            continue  # 仅强转字符串
        schema = props.get(key) or {}
        t = _first_non_null_type(schema.get("type"))
        try:
            if t == "integer":
                out[key] = int(val)
            elif t == "number":
                out[key] = float(val)
            elif t == "boolean":
                out[key] = val.strip().lower() in ("true", "1", "yes")
            elif t in ("array", "object"):
                out[key] = json.loads(val)
        except (ValueError, json.JSONDecodeError):
            log.warning("参数 %s 强转 %s 失败，保留原值 %r", key, t, val)
    return out


def require_module(module_name: str) -> Callable[[], bool]:
    """闭包：模块可导入则工具可见（自动隐藏缺依赖工具，spec 6.2.1）。"""
    def _check() -> bool:
        try:
            importlib.import_module(module_name)
            return True
        except ImportError:
            return False
    return _check


class ToolRegistry:
    """工具注册表。available_defs 运行时过滤，openai_tools 动态重建，execute 错误兜底。"""

    def __init__(self) -> None:
        self._defs: dict[str, ToolDefinition] = {}

    def register(self, td: ToolDefinition) -> None:
        self._defs[td.name] = td
        log.info("注册工具 %s", td.name)

    def get(self, name: str) -> ToolDefinition | None:
        return self._defs.get(name)

    def available_defs(self) -> list[ToolDefinition]:
        """运行时可用性过滤后的 ToolDefinition 列表。"""
        return [td for td in self._defs.values() if td.availability()]

    def openai_tools(self) -> list[dict]:
        """LLM 侧 schema。动态重建防 模型 幻觉调隐藏工具。"""
        return [
            {"type": "function",
             "function": {"name": td.name, "description": td.description,
                          "parameters": td.parameters}}
            for td in self.available_defs()
        ]

    async def execute(self, name: str, args: dict,
                      ctx: LoopContext, cancel_token: CancelToken) -> ToolResult:
        """按名取工具 → coerce 参数 → 调 handler。
        工具不存在/不可用/抛异常均返回带错误摘要的 ToolResult，回灌 LLM 触发错误自愈。"""
        td = self._defs.get(name)
        if td is None:
            return ToolResult(summary=f"错误：工具 '{name}' 不存在")
        if not td.availability():
            return ToolResult(summary=f"错误：工具 '{name}' 当前不可用")
        coerced = coerce_tool_args(td.parameters, args)
        try:
            return await td.handler(coerced, ctx, cancel_token)
        except Exception as e:
            log.exception("工具 %s 执行异常", name)
            return ToolResult(summary=f"工具 '{name}' 执行出错: {e}")
