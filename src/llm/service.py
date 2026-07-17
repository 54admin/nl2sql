"""LLM 服务：openai 官方 AsyncOpenAI 直连（OpenAI 兼容协议，任意模型）。
配置全走数据库 llm_config 表：PUT /api/admin/llm-config 配置，
未配置时 _resolve_config 抛错提示先 PUT。"""
import json
from dataclasses import dataclass
from typing import AsyncIterator

from src.config import LLMConfig
from src.logging import get_logger
from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)


@dataclass
class _Resp:
    """openai ChatCompletion 的兼容包装，供 AgentLoop 用 getattr 取 content/tool_calls。"""
    content: str
    tool_calls: list  # [{id, name, args(dict)}]


@dataclass
class _Chunk:
    """流式分片兼容包装（demo 用 getattr(chunk, 'content')）。"""
    content: str


class LLMService:
    """LLM 服务：openai AsyncOpenAI 直连 + 配置全数据库（无 yml 兜底）。
    未配置（表空/enabled=False/PG异常）时 _resolve_config 抛 RuntimeError。
    admin PUT 后调 reset_dynamic() 热更新（清缓存 + 重建 client）。"""

    def __init__(self):
        self._client = None          # AsyncOpenAI
        self._cfg_sig = None         # 已建 client 的配置签名（变了重建）
        self._dynamic: dict | None = None

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
        self._dynamic = None
        self._client = None
        self._cfg_sig = None

    async def _resolve_config(self) -> LLMConfig:
        dyn = await self._load_dynamic()
        if not dyn:
            raise RuntimeError(
                "LLM 未配置：请先 PUT /api/admin/llm-config 存 model/base_url/api_key")
        return LLMConfig(**dyn)

    def _get_client(self, cfg: LLMConfig):
        from openai import AsyncOpenAI
        sig = (cfg.api_key, cfg.api_base, cfg.timeout)
        if self._client is None or self._cfg_sig != sig:
            self._client = AsyncOpenAI(
                api_key=cfg.api_key, base_url=cfg.api_base, timeout=cfg.timeout)
            self._cfg_sig = sig
        return self._client

    async def chat(self, messages: list[dict], tools: list | None = None):
        """非流式一次调用（loop 主用）。返回 _Resp（content + tool_calls）。"""
        cfg = await self._resolve_config()
        client = self._get_client(cfg)
        kwargs = {"model": cfg.model, "messages": messages,
                  "temperature": cfg.temperature}
        if tools:
            kwargs["tools"] = tools
        resp = await client.chat.completions.create(**kwargs)
        return self._wrap(resp)

    @staticmethod
    def _wrap(resp) -> _Resp:
        """openai ChatCompletion → _Resp（agent_loop 用 getattr content/tool_calls）。"""
        msg = resp.choices[0].message
        tcs = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except Exception:
                args = {}
            tcs.append({"id": tc.id, "name": tc.function.name, "args": args})
        return _Resp(content=msg.content or "", tool_calls=tcs)

    async def chat_stream(self, messages: list[dict], tools: list | None = None) -> AsyncIterator[_Chunk]:
        """流式生成，yield _Chunk（content 文本片段）。"""
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
            content = getattr(delta, "content", None)
            if content:
                yield _Chunk(content=content)
