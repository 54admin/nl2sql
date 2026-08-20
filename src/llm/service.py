"""LLM 服务：openai 官方 AsyncOpenAI 直连（OpenAI 兼容协议，任意模型）。
配置全走数据库 llm_config 表：PUT /api/admin/llm-config 配置，
未配置时 _resolve_config 抛错提示先 PUT。"""
import asyncio
import json
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator

from src.config import LLMConfig
from src.logging import get_logger
from src.storage.models import LlmConfigRow
from src.storage.db_client import AsyncSessionFactory

log = get_logger(__name__)

# 孤立代理字符（\ud800-\udfff）出口兜底：流式分块把多字节字符（数学粗体 𝗽 等）切成两半
# 会产生半截代理；Python str 能存它，但一进 LLM 请求体 utf-8 编码就当场炸
# （'surrogates not allowed'，全 run 报错）。入口清洗（agent_loop._clean）只管新流入的
# 分块，管不了【中毒的会话历史回灌】（修复前中毒的 assistant 消息存在 Redis/PG 里，
# 同会话追问会原样带回请求体）——故在请求出口递归清洗全部消息，毒从哪来都拦得住。
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _sanitize_messages(messages: list[dict]) -> list[dict]:
    def _s(v):
        if isinstance(v, str):
            return _SURROGATE_RE.sub("\ufffd", v) if v else v
        if isinstance(v, list):
            return [_s(x) for x in v]
        if isinstance(v, dict):
            return {k: _s(x) for k, x in v.items()}
        return v
    return [_s(m) for m in messages]


@dataclass
class _Resp:
    """openai ChatCompletion 的兼容包装，供 AgentLoop 取 content/tool_calls。"""
    content: str
    tool_calls: list  # [{id, name, args(dict)}]


@dataclass
class _Chunk:
    """流式分片：content 文本片段 + tool_call 增量（openai delta 原生对象列表）。
    reasoning：模型思考链（reasoning_content/thinking），工具决策轮 content 为空时它非空——
    原来被丢弃→工具阶段全程静默；现在透传给 agent_loop 发 reasoning_delta 打字机。"""
    content: str = ""
    tool_call_delta: list = field(default_factory=list)
    reasoning: str = ""


class LLMService:
    """LLM 服务：双协议（openai / anthropic）+ 配置全数据库（无 yml 兜底）。
    按 llm_config.protocol 选 SDK：openai 走 /v1/chat/completions+Bearer，
    anthropic 走 /v1/messages+x-api-key。同一网关常按协议分额度桶——
    哪条路径有额度就配哪个 protocol。
    对外统一吐 _Chunk（流式）/ _Resp（非流式），AgentLoop 不感知协议差异。
    未配置时 _resolve_config 抛 RuntimeError。admin PUT 后调 reset_dynamic() 热更新。"""

    def __init__(self):
        self._clients: dict[str, object] = {}   # purpose|protocol → client，按用途/协议各自缓存
        self._dynamic: dict[str, dict] = {}      # 用途 → 配置缓存（analysis/attribution）
        # 限流（P2 主动节流，防撞网关限流）：全局闸，按 analysis 用途的 rpm/concurrency 配置
        self._sem: asyncio.Semaphore | None = None
        self._req_times: deque = deque()
        self._rpm_limit: int | None = None
        self._concurrency: int | None = None

    async def _load_dynamic(self, purpose: str = "analysis") -> dict | None:
        if purpose in self._dynamic:
            return self._dynamic[purpose]
        try:
            async with AsyncSessionFactory() as s:
                from sqlalchemy import select
                # model 级：取 enabled 且 purposes 含 purpose 的（取 version 最新）；python 过滤跨方言
                rows = (await s.execute(select(LlmConfigRow).where(
                    LlmConfigRow.enabled.is_(True)))).scalars().all()
                matched = sorted([r for r in rows if purpose in (r.purposes or [])],
                                 key=lambda r: r.version, reverse=True)
                row = matched[0] if matched else None
                if row is None:
                    return None
                self._dynamic[purpose] = {
                    "model": row.model, "api_base": row.base_url,
                    "api_key": row.api_key, "temperature": row.temperature,
                    "timeout": row.timeout, "max_context": row.max_context,
                    "protocol": (row.protocol or "openai"),
                    "rpm_limit": row.rpm_limit, "concurrency": row.concurrency,
                }
                # 限流器全局：按 analysis 用途的 rpm/concurrency 建（主 chat 流量）
                if purpose == "analysis":
                    self._apply_rate_limit(row.rpm_limit, row.concurrency)
        except Exception as e:
            log.warning("读 LLM 配置失败(%s): %s", purpose, e)
            return None
        return self._dynamic[purpose]

    def reset_dynamic(self) -> None:
        """admin PUT 后调用：清所有用途缓存 + client + 限流器，下次按最新配置重建。"""
        self._dynamic.clear()
        self._clients.clear()
        self._sem = None
        self._req_times.clear()
        self._rpm_limit = None
        self._concurrency = None

    async def _resolve_config(self, purpose: str = "analysis") -> LLMConfig:
        dyn = await self._load_dynamic(purpose)
        if not dyn:
            raise RuntimeError(
                f"LLM 未配置[{purpose}]：请在管理后台配 {purpose} 用途的 model/base_url/api_key")
        return LLMConfig(**dyn)

    def _get_client(self, cfg: LLMConfig):
        """按 protocol 懒建并缓存对应 SDK 客户端。"""
        proto = (cfg.protocol or "openai").lower()
        if proto in self._clients:
            return self._clients[proto]
        if proto == "anthropic":
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(
                api_key=cfg.api_key, base_url=cfg.api_base, timeout=cfg.timeout)
        else:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                api_key=cfg.api_key, base_url=cfg.api_base, timeout=cfg.timeout)
        self._clients[proto] = client
        return client

    async def chat_stream(self, messages: list[dict], tools: list | None = None,
                          purpose: str = "analysis") -> AsyncIterator[_Chunk]:
        """流式生成，yield _Chunk。purpose=用途（analysis 对话查询/attribution 归因），
        按用途取配置。入口主动限速（RPM 窗+并发闸）；建立 stream 阶段可恢复错误指数退避+jitter+retry-after；
        流中途断不重试（避免重复输出），交 loop 错误自愈。"""
        cfg = await self._resolve_config(purpose)
        proto = (cfg.protocol or "openai").lower()
        messages = _sanitize_messages(messages)   # 出口兜底：拦中毒历史/DB/知识库文本
        await self._throttle()              # RPM 时间窗：满了先 sleep（并发闸在下方 async with）
        # 并发闸：信号量在 async gen 里用 async with 包裹分发迭代；None 则不限
        if self._sem is not None:
            async with self._sem:
                async for ch in self._dispatch(proto, cfg, messages, tools):
                    yield ch
        else:
            async for ch in self._dispatch(proto, cfg, messages, tools):
                yield ch

    async def _dispatch(self, proto: str, cfg: LLMConfig, messages: list[dict],
                        tools: list | None) -> AsyncIterator[_Chunk]:
        """按协议分发流式（chat_stream 已做限速，这里只选 openai/anthropic）。"""
        if proto == "anthropic":
            async for ch in self._stream_anthropic(cfg, messages, tools):
                yield ch
        else:
            async for ch in self._stream_openai(cfg, messages, tools):
                yield ch

    def _apply_rate_limit(self, rpm_limit: int | None, concurrency: int | None) -> None:
        """按配置建/重建限流器。None=该维度不限。
        并发闸用 Semaphore（chat_stream async with）；RPM 用时间窗 deque（_throttle 里查）。"""
        self._rpm_limit = rpm_limit
        self._concurrency = concurrency
        self._sem = asyncio.Semaphore(concurrency) if concurrency else None
        self._req_times.clear()

    async def _throttle(self) -> None:
        """RPM 时间窗限速：最近 60s 内请求数 >= rpm_limit 时，sleep 到最早的记录过期。
        并发由 self._sem 信号量管（chat_stream 里 async with）。rpm_limit=None 不限。"""
        if not self._rpm_limit:
            return
        now = time.monotonic()
        # 清 60s 前的旧记录
        while self._req_times and now - self._req_times[0] >= 60:
            self._req_times.popleft()
        if len(self._req_times) >= self._rpm_limit:
            # 满了：等到最早那条记录满 60s 后过期才能放行
            wait = 60 - (now - self._req_times[0])
            if wait > 0:
                log.info("RPM 限速：%d/%d 满，等待 %.1fs", len(self._req_times), self._rpm_limit, wait)
                await asyncio.sleep(wait)
                now = time.monotonic()
                while self._req_times and now - self._req_times[0] >= 60:
                    self._req_times.popleft()
        self._req_times.append(time.monotonic())

    async def _stream_openai(self, cfg: LLMConfig, messages: list[dict],
                             tools: list | None) -> AsyncIterator[_Chunk]:
        """openai 协议流式：/v1/chat/completions + Authorization: Bearer。"""
        client = self._get_client(cfg)
        kwargs = {"model": cfg.model, "messages": messages,
                  "temperature": cfg.temperature, "stream": True}
        if tools:
            kwargs["tools"] = tools

        async def _create():
            return await client.chat.completions.create(**kwargs)

        stream = await _call_with_retry(_create)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # reasoning_content：思考链（deepseek-r1/glm/qwen 等推理模型才有）。
            # 工具决策轮 content 为空但 reasoning 非空——捕获后下游打字机，破除工具阶段静默。
            reasoning = getattr(delta, "reasoning_content", None) or ""
            yield _Chunk(content=delta.content or "",
                         tool_call_delta=list(delta.tool_calls or []),
                         reasoning=reasoning)

    async def _stream_anthropic(self, cfg: LLMConfig, messages: list[dict],
                                tools: list | None) -> AsyncIterator[_Chunk]:
        """anthropic 协议流式：/v1/messages + x-api-key。
        anthropic 的 messages 格式与 openai 不同：system 单列、user/assistant 轮替 content 为 list。
        这里做格式适配：把 openai 风格 messages 转成 anthropic 风格再发，对外仍吐 _Chunk。
        tool_calls 增量按 anthropic 的 content_block_delta/tool_use 事件转成 openai delta 形状，
        AgentLoop 的 _to_openai_tool_calls 不用改。"""
        client = self._get_client(cfg)
        sys_text, user_msgs = _split_anthropic_messages(messages)
        # 输出预算给足（16384）：思考模型的 reasoning 也计入 max_tokens——4096 时
        # 长思考直接烧穿上限，流戛然而止（无正文无工具调用，或 finish 参数 JSON 被拦腰截断），
        # 实测 glm-5.2 单轮思考就能烧 5-8K。网关侧还有模型窗口上限兜底，不会真放肆。
        kwargs: dict = {"model": cfg.model, "messages": user_msgs,
                        "temperature": cfg.temperature, "max_tokens": 16384}
        if sys_text:
            kwargs["system"] = sys_text
        if tools:
            # openai tools 格式 → anthropic tools 格式（去外层 type，input_schema 取 parameters）
            kwargs["tools"] = [
                {"name": t["function"]["name"], "description": t["function"].get("description", ""),
                 "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}})}
                for t in tools if t.get("type") == "function" and t.get("function")
            ]

        async def _create():
            # anthropic SDK：stream=True 已废弃走 .stream() 上下文管理器，
            # 这里返回 AsyncMessagesStream（async with 用），不带 stream 参数。
            return await client.messages.create(**kwargs, stream=True)

        # anthropic 的 stream 是事件流：message_start/content_block_start/content_block_delta/...
        # 用 async with 管理流生命周期（SDK 的 AsyncMessagesStream 支持），逐事件映射到 _Chunk。
        async def _gen():
            stream = await _call_with_retry(_create)
            # 每个 content_block 一个 id；block_start 带 tool_use 名/工具 id；
            # input_json_delta 是 args 的增量片段（字符串，需拼接，同 openai arguments）。
            block_id_map: dict[int, dict] = {}   # block index → {id, name, args_acc}
            async for event in stream:
                etype = getattr(event, "type", "")
                if etype == "content_block_start":
                    idx = getattr(event, "index", 0)
                    block = getattr(event, "content_block", None)
                    if block and getattr(block, "type", "") == "tool_use":
                        block_id_map[idx] = {"id": getattr(block, "id", ""),
                                             "name": getattr(block, "name", "")}
                elif etype == "content_block_delta":
                    idx = getattr(event, "index", 0)
                    delta = getattr(event, "delta", None)
                    if not delta:
                        continue
                    dtype = getattr(delta, "type", "")
                    if dtype == "text_delta":
                        yield _Chunk(content=getattr(delta, "text", "") or "")
                    elif dtype == "thinking_delta":
                        # anthropic 扩展思考：thinking 块的增量，映射到 reasoning（同 openai reasoning_content）
                        yield _Chunk(reasoning=getattr(delta, "thinking", "") or "")
                    elif dtype == "input_json_delta":
                        m = block_id_map.get(idx)
                        if m:
                            m["args_acc"] = m.get("args_acc", "") + (getattr(delta, "partial_json", "") or "")
                elif etype == "content_block_stop":
                    # block 结束：若是 tool_use，把累积的 args 一次性吐成 tool_call_delta
                    idx = getattr(event, "index", 0)
                    m = block_id_map.get(idx)
                    if m:
                        yield _Chunk(tool_call_delta=[_AnthropicTCDelta(
                            id=m["id"], name=m["name"], args=m.get("args_acc", ""))])
        async for ch in _gen():
            yield ch

    async def chat(self, messages: list[dict], tools: list | None = None,
                   purpose: str = "analysis"):
        """非流式一次调用（收集 chat_stream 成 _Resp）。备用/测试用。"""
        content = ""
        tc_acc: dict = {}
        async for chunk in self.chat_stream(messages, tools, purpose):
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
                # anthropic 路径：_AnthropicTCDelta 直接有 name/args 字段
                if not fn and getattr(tc, "name", None):
                    acc["name"] = tc.name
                if not fn and getattr(tc, "args", None):
                    acc["args"] += tc.args
        tool_calls = []
        for idx in sorted(tc_acc):
            v = tc_acc[idx]
            try:
                args = json.loads(v["args"]) if v["args"] else {}
            except Exception:
                args = {}
            tool_calls.append({"id": v["id"], "name": v["name"], "args": args})
        return _Resp(content=content, tool_calls=tool_calls)


@dataclass
class _AnthropicTCDelta:
    """anthropic 协议的 tool_call 增量适配对象：带 id/name/args，模拟 openai delta 形状。
    chat() 收集时按 name/args 字段取值（openai delta 走 .function.name/arguments）。"""
    id: str = ""
    name: str = ""
    args: str = ""    # JSON 字符串增量片段（同 openai arguments 拼接）
    index: int = 0
    function: object = field(default=None)   # openai delta 兼容占位（anthropic 路径不用）


def _split_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """把 openai 风格 messages 转成 anthropic 风格：system 抽出成纯文本，其余转 content 为 list。
    返回 (system_text, anthropic_messages)。
    ponytail: 只转本项目实际用到的 role：system/user/assistant/tool。
    tool 角色在 anthropic 里是 user 的 tool_result content block。"""
    sys_parts: list[str] = []
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                sys_parts.append(str(m["content"]))
            continue
        if role == "tool":
            # openai tool 消息 → anthropic user role 的 tool_result block
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                 "content": str(m.get("content", ""))}]})
            continue
        # user/assistant：content 可能是字符串也可能带 tool_calls（assistant）
        content = m.get("content", "")
        if role == "assistant":
            blocks = []
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for tc in m.get("tool_calls", []) or []:
                fn = tc.get("function", {})
                args_str = fn.get("arguments", "")
                try:
                    args_obj = json.loads(args_str) if args_str else {}
                except Exception:
                    args_obj = {}
                blocks.append({"type": "tool_use", "id": tc.get("id", ""),
                               "name": fn.get("name", ""), "input": args_obj})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        else:
            out.append({"role": role, "content": str(content) if content else ""})
    return "\n\n".join(sys_parts), out


# 可恢复错误类型名（openai SDK 异常类名前缀 + 内置超时/连接类），命中则退避重试。
# ponytail: 按类名字符串判，避免硬 import openai 异常类（FakeLLM 测试不依赖 openai）。
_RETRYABLE_HINTS = ("APITimeoutError", "APIConnectionError",
                    "InternalServerError", "OverloadedError",
                    "TimeoutError", "ConnectionError", "ConnectionResetError",
                    "ClientOSError")

# 不可恢复错误特征：即使 HTTP 429/403，只要错误体含这些关键词就是"额度/欠费/封禁"，
# 重试只会白等——额度不会自己恢复。拎出来走人话文案，不重试。
# （RateLimitError 不再无脑重试：先看错误体是不是 quota/billing 类）
_FATAL_QUOTA_HINTS = ("insufficient_quota", "quota", "billing", "exceeded",
                      "payment", "credit", "limit_reached", "account_deactivated")


def _err_body(err: Exception) -> str:
    """尽量从 openai 异常里抠出错误体文本（message/code/body 拼一起），用于关键词判配额类。"""
    parts = [str(err), type(err).__name__]
    for attr in ("message", "code", "body"):
        v = getattr(err, attr, None)
        if v:
            parts.append(str(v))
    # openai SDK 的 err.response.text 也有完整错误体
    resp = getattr(err, "response", None)
    if resp is not None:
        txt = getattr(resp, "text", None)
        if txt:
            parts.append(str(txt))
    return " ".join(parts)


def _is_fatal_quota(err: Exception) -> bool:
    """判是否额度/欠费类不可恢复错误（即便返回 429/403 也不重试）。"""
    body = _err_body(err).lower()
    return any(h in body for h in _FATAL_QUOTA_HINTS)


def _is_retryable(err: Exception) -> bool:
    """判错误是否可恢复（限流/超时/网关/连接）。
    顺序：先排额度类（不可恢复）→ 网关包装 400 → 超时/连接/5xx 类名 → 纯 429 限流（非额度）。
    RateLimitError 单独拎出来：先看错误体，是 quota/billing 则不重试，否则（瞬时限流）重试。
    网关包装 400：'bad response status code' 是代理转述上游瞬时 400（实测网关抖动时间歇出现，
    过一会就好）——与真 schema 错（max_tokens/invalid_request_error）区分，前者重试后者不重试。"""
    if _is_fatal_quota(err):
        return False   # 额度不足：不重试，直接抛让前端展示人话
    if "bad response status code" in str(err):
        return True    # 网关转述上游瞬时错误：可重试
    name = type(err).__name__
    if any(h in name for h in _RETRYABLE_HINTS):
        return True
    # RateLimitError 本身：排除了 quota 后就是瞬时限流，可重试
    if "RateLimit" in name:
        return True
    # 状态码判：5xx 网关错可重试；429 但非 quota 也可重试（上面已排 quota）
    status = (getattr(err, "status_code", None)
              or getattr(getattr(err, "response", None), "status_code", None))
    if isinstance(status, int) and (500 <= status < 600 or status == 429):
        return True
    return False


def describe_llm_error(err: Exception) -> str:
    """把 LLM 异常翻成人话给前端展示。配额类→额度不足，鉴权→key 错，超时→稍重试。
    兜底返回原始 message，不吞信息。"""
    if _is_fatal_quota(err):
        return "LLM 额度不足或配额超限，请联系管理员充值/检查配额后再试。"
    name = type(err).__name__
    if "Auth" in name or "Permission" in name or "Unauthorized" in name:
        return "LLM 鉴权失败：API Key 无效或无权限，请检查 llm_config 的 api_key。"
    status = (getattr(err, "status_code", None)
              or getattr(getattr(err, "response", None), "status_code", None))
    if isinstance(status, int) and status == 404:
        return "LLM 模型不存在：请检查 llm_config 的 model 名称是否正确。"
    if "Timeout" in name or "timeout" in str(err).lower():
        return "LLM 调用超时，请稍后重试。"
    return str(err)[:200]


async def _call_with_retry(coro_fn, *, max_retries: int = 3, base_delay: float = 1.0):
    """对协程工厂做指数退避 + jitter 重试。可恢复错误重试，不可恢复直接抛。
    coro_fn: 无参返回 coroutine 的可调用。
    退避 = base_delay * 2^(attempt-1)，封顶 30s；±50% jitter 防高并发惊群；
    若响应带 retry-after 头则取 max(退避, retry_after)——尊重网关给的精确等待。"""
    last_err = None
    for attempt in range(1, max_retries + 2):   # 1..max_retries+1，最后一次不重试直接抛
        try:
            return await coro_fn()
        except Exception as e:
            last_err = e
            if not _is_retryable(e) or attempt > max_retries:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), 30.0)
            delay *= 0.5 + random.random()      # ±50% jitter，防惊群（并发重试不同步撞限流）
            ra = _retry_after_seconds(e)        # 尊重 retry-after 头
            if ra is not None:
                delay = max(delay, min(ra, 60.0))   # retry-after 也封顶 60s，避免等太久
            log.warning("LLM 调用可恢复错误，%.1fs 后重试(%d/%d): %s",
                        delay, attempt, max_retries, e)
            await asyncio.sleep(delay)
    raise last_err   # 兜底，逻辑走不到


def _retry_after_seconds(err: Exception) -> float | None:
    """从异常的 HTTP 响应里抠 retry-after 头（秒）。没有则 None。
    retry-after 可能是秒数（"30"）或 HTTP 日期（RFC1123）——前者常见，后者兜底解析。
    ponytail: openai/anthropic SDK 异常都带 .response（httpx Response），headers 大小写都试。"""
    resp = getattr(err, "response", None)
    if resp is None:
        return None
    headers = getattr(resp, "headers", None) or {}
    ra = headers.get("retry-after") or headers.get("Retry-After")
    if not ra:
        return None
    try:
        return float(ra)     # 数字秒数（最常见）
    except (ValueError, TypeError):
        # HTTP 日期格式，兜底用 email.utils 解析成到现在的剩余秒
        try:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone
            dt = parsedate_to_datetime(ra)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            return None
