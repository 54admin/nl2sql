"""LLM 服务：ChatOpenAI 封装 + Qwen 流式工具调用兼容。
配置全走数据库 llm_config 表（无 yml 兜底）：PUT /api/admin/llm-config 配置，
未配置时 _resolve_config 抛错提示先 PUT。"""
import json

from src.config import LLMConfig
from src.logging import get_logger
from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)


def collect_stream_result(chunks: list) -> dict | None:
    """合并流式工具调用分片。

    Qwen3-235B 经 OpenAI 兼容网关流式时：
    - tool_call_chunks[0].args 首块常为 None
    - 实际参数在后续块的 tool_call_chunks 增量到达
    - 兜底：流式仍空则用任意块的 tool_calls[i].args

    返回 {id, name, arguments(json 字符串)} 或 None（无工具调用）。
    """
    merged_args = {}
    name = None
    call_id = None

    for chunk in chunks:
        tcc = getattr(chunk, "tool_call_chunks", None) or []
        for tc in tcc:
            if tc.get("name"):
                name = tc["name"]
            if tc.get("id"):
                call_id = tc["id"]
            # 兼容 args / arguments 两种键
            arg = tc.get("args")
            if arg is None:
                arg = tc.get("arguments")
            if isinstance(arg, str) and arg:
                idx = tc.get("index", 0)
                merged_args[idx] = merged_args.get(idx, "") + arg

    if not name and not merged_args:
        return None

    arguments = merged_args.get(0, "")
    # 兜底：流式仍为空，从 tool_calls 取
    if not arguments:
        for chunk in chunks:
            tcs = getattr(chunk, "tool_calls", None) or []
            for tc in tcs:
                args = tc.get("args") or tc.get("arguments")
                if args:
                    arguments = json.dumps(args, ensure_ascii=False)
                    break
            if arguments:
                break

    return {"id": call_id, "name": name, "arguments": arguments}


class LLMService:
    """LLM 服务：配置全在数据库 llm_config 表，无 yml 兜底。
    未配置（表空 / enabled=False / PG 异常）时 _resolve_config 抛 RuntimeError，
    提示先 PUT /api/admin/llm-config。admin PUT 后调 reset_dynamic() 热更新。"""

    def __init__(self):
        self._client = None
        self._dynamic: dict | None = None  # 内存缓存（None=未加载）

    async def _load_dynamic(self) -> dict | None:
        """从 PG 读 LlmConfigRow（enabled=True）。无/未启用/PG异常 均返回 None。"""
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
        """admin PUT 后调用：清动态缓存 + 置空 client。下次调用按最新配置重建。"""
        self._dynamic = None
        self._client = None

    async def _resolve_config(self) -> LLMConfig:
        """配置全在数据库，无兜底。未配置抛错。"""
        dyn = await self._load_dynamic()
        if not dyn:
            raise RuntimeError(
                "LLM 未配置：请先 PUT /api/admin/llm-config 存 model/base_url/api_key")
        return LLMConfig(**dyn)

    def _ensure_client(self, cfg: LLMConfig):
        if self._client is None:
            from langchain_openai import ChatOpenAI
            self._client = ChatOpenAI(
                api_key=cfg.api_key, base_url=cfg.api_base, model=cfg.model,
                temperature=cfg.temperature, timeout=cfg.timeout, streaming=True,
            )
        return self._client

    async def chat(self, messages: list[dict], tools: list | None = None):
        """非流式一次调用（loop 主用）。每次 resolve 配置（动态可能被 admin 改）。
        ponytail: asyncio.to_thread 跑同步 invoke——langchain ainvoke 在 ASGI
        事件循环（uvicorn）下死锁，sync invoke 放线程池规避。"""
        import asyncio
        cfg = await self._resolve_config()
        client = self._ensure_client(cfg)
        if tools:
            client = client.bind_tools(tools)
        return await asyncio.to_thread(client.invoke, messages)

    async def chat_stream(self, messages: list[dict], tools: list | None = None):
        """流式生成，yield chunk。"""
        cfg = await self._resolve_config()
        client = self._ensure_client(cfg)
        if tools:
            client = client.bind_tools(tools)
        async for chunk in client.astream(messages):
            yield chunk
