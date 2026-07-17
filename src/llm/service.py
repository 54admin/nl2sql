"""LLM 服务：ChatOpenAI 封装 + Qwen 流式工具调用兼容。"""
import json

from src.config import LLMConfig
from src.logging import get_logger

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
    def __init__(self, config: LLMConfig):
        self._config = config
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from langchain_openai import ChatOpenAI
            self._client = ChatOpenAI(
                api_key=config_api_key(self._config),
                base_url=self._config.api_base,
                model=self._config.model,
                temperature=self._config.temperature,
                timeout=self._config.timeout,
                streaming=True,
            )
        return self._client

    async def chat(self, messages: list[dict], tools: list | None = None):
        """非流式一次调用（loop 主用），返回完整响应。"""
        client = self._ensure_client()
        if tools:
            client = client.bind_tools(tools)
        return await client.ainvoke(messages)

    async def chat_stream(self, messages: list[dict], tools: list | None = None):
        """流式生成，yield chunk。调用方自行 collect。"""
        client = self._ensure_client()
        if tools:
            client = client.bind_tools(tools)
        async for chunk in client.astream(messages):
            yield chunk


def config_api_key(cfg: LLMConfig) -> str:
    key = cfg.api_key or ""
    if not key:
        log.warning("LLM api_key 为空，确认环境变量 OPENAI_API_KEY 已设置")
    return key
