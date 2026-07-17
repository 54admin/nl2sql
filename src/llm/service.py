"""LLM 服务：openai 官方 AsyncOpenAI 直连（OpenAI 兼容协议，任意模型）。
配置全走数据库 llm_config 表：PUT /api/admin/llm-config 配置，
未配置时 _resolve_config 抛错提示先 PUT。"""
import json
from dataclasses import dataclass, field
from typing import AsyncIterator

from src.config import LLMConfig
from src.logging import get_logger
from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)


@dataclass
class _Resp:
    """openai ChatCompletion 的兼容包装，供 AgentLoop 取 content/tool_calls。"""
    content: str
    tool_calls: list  # [{id, name, args(dict)}]


@dataclass
class _Chunk:
    """流式分片：content 文本片段 + tool_call 增量（openai delta 原生对象列表）。"""
    content: str = ""
    tool_call_delta: list = field(default_factory=list)


class LLMService:
    """LLM 服务：openai AsyncOpenAI 直连 + 配置全数据库（无 yml 兜底）。
    未配置时 _resolve_config 抛 RuntimeError。admin PUT 后调 reset_dynamic() 热更新。"""

    def __init__(self):
        self._client = None          # AsyncOpenAI
        self._dynamic: dict | None = None  # 内存缓存（None=未加载）

    async def _load_dynamic(self) -> dict | None:
        if self._dynamic is not None:
            return self._dynamic
        try:
            async with AsyncSessionFactory() as s:
                row = await s.get(LlmConfigRow, "default")
                if row is None or not row.enabled:
                    return None
                self._dynamic = {
                    "model": row.model, "api_base": row.base_url,
                    "api_key": row.api_key, "temperature": row.temperature,
                    "timeout": row.timeout,
                }
        except Exception as e:
            log.warning("读 LLM 配置失败: %s", e)
            return None
        return self._dynamic

    def reset_dynamic(self) -> None:
        """admin PUT 后调用：清缓存 + 置空 client，下次按最新配置重建。"""
        self._dynamic = None
        self._client = None

    async def _resolve_config(self) -> LLMConfig:
        dyn = await self._load_dynamic()
        if not dyn:
            raise RuntimeError(
                "LLM 未配置：请先 PUT /api/admin/llm-config 存 model/base_url/api_key")
        return LLMConfig(**dyn)

    def _get_client(self, cfg: LLMConfig):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=cfg.api_key, base_url=cfg.api_base, timeout=cfg.timeout)
        return self._client

    async def chat_stream(self, messages: list[dict], tools: list | None = None) -> AsyncIterator[_Chunk]:
        """流式生成，yield _Chunk（content 文本片段 + tool_call 增量）。
        loop 边收 content 发 answer_delta（打字机），流末 collect tool_calls。"""
        cfg = await self._resolve_config()
        client = self._get_client(cfg)
        kwargs = {"model": cfg.model, "messages": messages,
                  "temperature": cfg.temperature, "stream": True}
        if tools:
            kwargs["tools"] = tools
        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            yield _Chunk(content=delta.content or "",
                         tool_call_delta=list(delta.tool_calls or []))

    async def chat(self, messages: list[dict], tools: list | None = None):
        """非流式一次调用（收集 chat_stream 成 _Resp）。备用/测试用。"""
        content = ""
        tc_acc: dict = {}
        async for chunk in self.chat_stream(messages, tools):
            if chunk.content:
                content += chunk.content
            for tc in chunk.tool_call_delta:
                idx = getattr(tc, "index", None) or 0
                acc = tc_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                if getattr(tc, "id", None):
                    acc["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn:
                    if fn.name:
                        acc["name"] = fn.name
                    if fn.arguments:
                        acc["args"] += fn.arguments
        tool_calls = []
        for idx in sorted(tc_acc):
            v = tc_acc[idx]
            try:
                args = json.loads(v["args"]) if v["args"] else {}
            except Exception:
                args = {}
            tool_calls.append({"id": v["id"], "name": v["name"], "args": args})
        return _Resp(content=content, tool_calls=tool_calls)
