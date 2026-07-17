# P0b Agent 内核 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Note（2026-07-17 review 修正版）：** 本版在原 10 任务基础上：
> 1. **修正 8 项 review 问题**——Task 7（原 Task 5）suspended 分支不再 append tool 消息（避免 resume 时与 SessionState.resume 产生重复 tool_call_id）；Task 12（原 Task 9）`AskRequest.mode` 字段类型直接用 `ViewerMode` 枚举（非法值由 pydantic 自动 422，不再依赖路由内 `ViewerMode(req.mode)` 抛 ValueError）；Task 12 测试补 `import pytest`；Task 13（原 Task 10）spike `main()` 改用 ORM 直接插指定 id 的 Session 行（保证 session_id 入库供 transition 使用）；Task 12 session 测试改用 `httpx.AsyncClient + ASGITransport`（避免同步 TestClient 在 async 测试中卡 event loop）；Task 7 异常分支 `transition(ERROR)` 包 try/except 防 DONE→ERROR 二次抛错覆盖原始异常；Task 10（原 Task 7）`SSEEventType` 补 `turn_start/assistant/tool_call/tool_result/warning/cancelled` 并加入 `_USER_HIDDEN`（user 模式只透传用户友好事件）；Task 7 `_normalize_args` 加 dict 兜底防 list 类型崩。
> 2. **追加 3 个新任务**——Task 2 `config_store`（动态配置基础：通用 KV 表 + 内存缓存 + 版本号）、Task 5 `llm_config`（动态 LLM 配置 + LLMService 读动态 + admin API）、Task 9 `prompts`（场景化 prompt 管理 + admin API + orchestrator 集成）。三者依赖链：types → config_store → (llm_config / prompts)。
>
> 任务总数 13（原 10 + 新 3），按依赖顺序重排：types → config_store → registry → builtins → llm_config → session → agent_loop → normalizer → prompts → sse → orchestrator → routes → spike。

**Goal:** 搭建 nl2sql 的 Agent 编排内核——自主同步 ReAct 循环、工具注册表（动态 schema + coerce + 可用性过滤）、ask_user 跨消息挂起恢复、结果旁路 result_id、双模式 SSE、取消令牌、护栏、名称纠错前置框架——并在 P0b 末尾用 Qwen3 spike 手动验证自主循环稳定性。

**Architecture:** Python 3.12 + FastAPI。核心是 `AgentLoop`（同步 ReAct：LLM→解析 tool_calls→逐工具执行→摘要回灌→重复）。工具统一接口 `ToolDefinition`，`ToolRegistry` 动态重建 OpenAI schema 防 Qwen 幻觉。`ask_user` 是工具，调用即挂起（存 LoopCheckpoint 到 PG + 状态转 awaiting_clarification），用户下条消息作为 tool result 注入恢复 loop（不重走纠错）。结果旁路：大结果存 PG/Redis，只回灌摘要给 LLM。双模式 SSE 在路由层过滤。取消令牌（bool 标志）贯穿 loop + 工具。状态机复用 P0a SessionManager 双写。

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x(async), langchain-openai, pydantic, pytest, pytest-asyncio, httpx（测试 TestClient）

**对应设计文档：** `docs/superpowers/specs/2026-07-17-nl2sql-ai-wenshu-design.md`（重点第 6 章 Agent 编排核心、第 13 章 P0 范围、第 12 章数据模型）

**对应 P0a 基础设施：** `docs/superpowers/plans/2026-07-17-p0a-infrastructure.md`（已完成，19 测试绿）

---

## 接口统一决策（综合 5 子系统设计）

5 个子系统设计存在命名/签名冲突，本计划统一如下（理由见末尾 Self-Review）：

| 冲突点 | 统一决策 |
|---|---|
| 上下文 dataclass 命名（ToolContext vs LoopContext） | **统一 `LoopContext(session_id, user_id, trace_id, channel="web")`**，废弃 ToolContext |
| handler 签名（是否含 cancel_token） | **统一 `(args, ctx, cancel_token) -> ToolResult`**（spec 6.6 要求取消贯穿工具） |
| ToolResult 字段（是否含 finished/suspended） | **统一含 `summary/result_id/finished/suspended`**（工具标志位驱动 loop） |
| 事件类型（LoopEvent vs SSEEvent） | **统一 `SSEEvent(type:str, data:dict, trace_id:str)`**，废弃 LoopEvent；type 用 str 不用 Enum |
| AgentLoop.run 签名 | **`run(session_id, user_id, user_msg, trace_id, cancel_token, is_resume=False)`**，loop 内部组装 messages |
| ask_user 挂起机制（异常 vs 标志 vs 硬编码） | **工具返回 `suspended=True`，loop 观察后调 `SessionState.suspend` + yield clarification + return**；废弃 SuspendLoop 异常（异常做控制流是反模式） |
| checkpoint 操作归属 | **收敛到 SessionState**（suspend/resume/expire），废弃 agent_loop 的模块级 save/load/delete |
| ToolRegistry 方法 | **保留 `available_defs()->list[ToolDefinition]` + `openai_tools()->list[dict]` + `execute(name,args,ctx,cancel_token)`** |
| Normalizer 返回类型 | **`normalize(text) -> tuple[str, list[Correction]]`**，orchestrator 解包 |
| 共享类型归属 | **集中 `src/core/types.py`**（CancelToken/LoopContext/ToolResult/SSEEvent/ToolDefinition/ToolHandler），tools 反向依赖 core |
| ToolRegistryProtocol | **不要**（ponytail：单实现接口是过度抽象），agent_loop 直接 import ToolRegistry |
| 状态机 RUNNING→IDLE | **新增合法转换**（取消场景 cancelled→IDLE） |

---

## File Structure

```
nl2sql/
├── src/
│   ├── core/                          # 新建：Agent 编排核心包
│   │   ├── __init__.py                # 新建（空）
│   │   ├── types.py                   # 新建：共享类型（CancelToken/LoopContext/ToolResult/SSEEvent/ToolDefinition）
│   │   ├── session.py                 # 新建：SessionState 状态机 + ask_user 挂起恢复
│   │   ├── agent_loop.py              # 新建：AgentLoop 自主 ReAct 循环（run 支持 system_prompt）
│   │   ├── normalizer.py              # 新建：名称纠错前置（P0b pass-through）
│   │   ├── prompt_store.py            # 新建：场景化 prompt 存储（Task 9）
│   │   └── orchestrator.py            # 新建：编排入口（纠错前置→分流→读 prompt→透传）
│   ├── config_store/                  # 新建：动态配置基础包（Task 2）
│   │   ├── __init__.py                # 新建（空）
│   │   └── store.py                   # 新建：ConfigStore KV + 内存缓存 + 版本号
│   ├── tools/                         # 新建：工具包
│   │   ├── __init__.py                # 新建（空）
│   │   ├── registry.py                # 新建：ToolRegistry + coerce_tool_args + require_module
│   │   └── builtins.py                # 新建：echo/finish/ask_user + default_registry
│   ├── web/                           # 新建：Web 层
│   │   ├── __init__.py                # 新建（空）
│   │   ├── sse.py                     # 新建：SSEEventType/ViewerMode + should_emit/filter_event/format_sse
│   │   └── routes/
│   │       ├── __init__.py            # 新建（空）
│   │       ├── ask.py                 # 新建：POST /api/ask/sse 流式
│   │       ├── session.py             # 新建：GET/DELETE /api/session
│   │       ├── admin_llm.py           # 新建：GET/PUT /api/admin/llm-config（Task 5）
│   │       └── admin_prompts.py       # 新建：CRUD /api/admin/prompts（Task 9）
│   ├── storage/models.py              # 修改：追加 AppConfigRow / LlmConfigRow / Prompt 三张表
│   ├── llm/service.py                 # 修改：LLMService 加 _load_dynamic/_resolve_config/reset_dynamic
│   └── memory/session.py              # 修改：追加 list_sessions(user_id)
├── tests/
│   ├── test_cancel_token.py           # 新建：CancelToken + 共享类型
│   ├── test_config_store.py           # 新建：ConfigStore KV + 缓存 + 版本（Task 2）
│   ├── test_tool_registry.py          # 新建：coerce/registry/builtins
│   ├── test_llm_config.py             # 新建：LLMService 动态优先 fallback yml（Task 5）
│   ├── test_routes_admin_llm.py       # 新建：admin LLM 配置路由（Task 5）
│   ├── test_session_state.py          # 新建：状态机 + 挂起恢复
│   ├── test_agent_loop.py             # 新建：ReAct 循环 + 护栏 + 取消
│   ├── test_normalizer.py             # 新建：pass-through
│   ├── test_prompt_store.py           # 新建：场景化 prompt 存储（Task 9）
│   ├── test_routes_admin_prompts.py   # 新建：admin prompts CRUD 路由（Task 9）
│   ├── test_sse.py                    # 新建：双模式过滤
│   ├── test_orchestrator.py           # 新建：编排入口（含 prompt_store 集成）
│   ├── test_routes_ask.py             # 新建：SSE 流式路由
│   ├── test_routes_session.py         # 新建：会话列表/删除路由
│   ├── test_spike_stats.py            # 新建：spike 统计逻辑单测
│   └── spike_qwen_react.py            # 新建：Qwen3 spike 手动脚本（非单测）
└── requirements.txt                   # 修改：补 httpx>=0.27
```

**职责边界：**
- `core/types.py`：所有子系统的共享类型根基，零内部依赖（仅 stdlib）。
- `config_store/store.py`：通用动态配置 KV 基础设施，单进程内存缓存。
- `tools/registry.py`：工具注册与执行，从 core.types import 类型。
- `tools/builtins.py`：三个基础工具，零持久化（标志位交给 loop 解释）。
- `core/session.py`：状态机 + checkpoint，委托 P0a SessionManager 存状态、PG 存 checkpoint。
- `core/agent_loop.py`：编排内核，组合 LLM + registry + session_state，产 SSEEvent；`run` 接受可选 `system_prompt` 注入 `messages[0]`。
- `core/normalizer.py`：纯函数级纠错前置，P0b pass-through。
- `core/prompt_store.py`：场景化 prompt 存储（内存缓存 + PG 持久），orchestrator 按 `default` 场景读。
- `core/orchestrator.py`：入口分流（纠错→查状态→读 prompt→透传 loop 事件）。
- `web/sse.py`：纯函数双模式过滤。
- `web/routes/*`：FastAPI 路由薄层；admin_* 路由提供动态配置 CRUD。
- `llm/service.py`：LLMService 调用时动态优先（`LlmConfigRow`），fallback `ApplicationConfig.llm`；`reset_dynamic` 支持 PUT 热更新。

---

## Task 1: 共享类型 + CancelToken（core/types.py）

**Files:**
- Create: `src/core/__init__.py`（空包）, `src/core/types.py`
- Modify: `requirements.txt`（补 httpx>=0.27）
- Test: `tests/test_cancel_token.py`

**设计要点：** 所有 P0b 子系统反向依赖 types.py（避免循环导入）。CancelToken 用 bool 标志位（GIL 下单线程读写原子，spec 6.6 贯穿 loop 检查点）。

- [ ] **Step 1: 补 httpx 依赖 + 建包**

Run:
```bash
cd /Users/liuxiangwu/PycharmProjects/nl2sql
mkdir -p src/core src/tools src/web/routes
touch src/core/__init__.py src/tools/__init__.py src/web/__init__.py src/web/routes/__init__.py
```

`requirements.txt` 末尾追加：
```
httpx>=0.27
```

- [ ] **Step 2: 写失败测试 `tests/test_cancel_token.py`**

```python
import asyncio
import pytest

from src.core.types import (
    CancelToken, LoopContext, ToolResult, SSEEvent, ToolDefinition,
)


# ---- CancelToken ----
def test_cancel_token_default_not_cancelled():
    tk = CancelToken()
    assert tk.cancelled is False


def test_cancel_token_cancel_sets_flag():
    tk = CancelToken()
    tk.cancel()
    assert tk.cancelled is True


def test_cancel_token_check_silent_when_not_cancelled():
    tk = CancelToken()
    tk.check()  # 不抛异常即通过


def test_cancel_token_check_raises_when_cancelled():
    tk = CancelToken()
    tk.cancel()
    with pytest.raises(asyncio.CancelledError):
        tk.check()


# ---- LoopContext ----
def test_loop_context_default_channel():
    ctx = LoopContext(session_id="s", user_id="u", trace_id="t")
    assert ctx.channel == "web"


def test_loop_context_custom_channel():
    ctx = LoopContext(session_id="s", user_id="u", trace_id="t", channel="feishu")
    assert ctx.channel == "feishu"


# ---- ToolResult ----
def test_tool_result_defaults():
    r = ToolResult(summary="hi")
    assert r.result_id is None
    assert r.finished is False
    assert r.suspended is False


def test_tool_result_finished():
    r = ToolResult(summary="done", finished=True)
    assert r.finished is True


def test_tool_result_suspended():
    r = ToolResult(summary="q?", suspended=True)
    assert r.suspended is True


# ---- SSEEvent ----
def test_sse_event_defaults():
    ev = SSEEvent(type="done")
    assert ev.data == {}
    assert ev.trace_id == ""


def test_sse_event_full():
    ev = SSEEvent(type="answer_delta", data={"text": "x"}, trace_id="t1")
    assert ev.type == "answer_delta"
    assert ev.data == {"text": "x"}


# ---- ToolDefinition ----
def test_tool_definition_minimal():
    async def h(args, ctx, tk):
        return ToolResult(summary="ok")

    td = ToolDefinition(name="x", description="d", parameters={}, handler=h)
    assert td.name == "x"
    assert td.availability() is True  # 默认可用


def test_tool_definition_custom_availability():
    async def h(args, ctx, tk):
        return ToolResult(summary="ok")

    td = ToolDefinition(name="x", description="d", parameters={},
                        handler=h, availability=lambda: False)
    assert td.availability() is False
```

- [ ] **Step 3: 运行验证失败**

Run: `pytest tests/test_cancel_token.py -v`
Expected: FAIL（`No module named 'src.core.types'`）

- [ ] **Step 4: 实现 `src/core/types.py`**

```python
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
    - summary: 回灌给 LLM 的摘要文本（结果旁路：全量不在内，spec 6.5）
    - result_id: 大结果旁路引用（P1 execute_sql 用，P0b 留接口）
    - finished: finish 工具置 True → loop 终止并把 summary 作为最终答案
    - suspended: ask_user 工具置 True → loop 挂起，由 SessionState 持久化 checkpoint
    """
    summary: str
    result_id: str | None = None
    finished: bool = False
    suspended: bool = False


@dataclass
class SSEEvent:
    """loop 产出的事件。SSE 层按双模式过滤（spec 6.8）。type 用 str 保持序列化简单。"""
    type: str
    data: dict = field(default_factory=dict)
    trace_id: str = ""


# 工具处理器类型别名（供 handlers 标注，统一三参签名 spec 6.6）
ToolHandler = Callable[[dict, LoopContext, CancelToken], Awaitable[ToolResult]]


@dataclass
class ToolDefinition:
    """工具统一接口（spec 5.2）。handler 接收 (args, ctx, cancel_token)。"""
    name: str
    description: str
    parameters: dict                       # JSON Schema
    handler: ToolHandler
    availability: Callable[[], bool] = field(default=lambda: True)
```

- [ ] **Step 5: 运行验证通过**

Run: `pytest tests/test_cancel_token.py -v`
Expected: PASS（10 测试绿）

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "feat(p0b): 共享类型 + CancelToken（core/types.py）

定义 CancelToken/LoopContext/ToolResult/SSEEvent/ToolDefinition，
所有 P0b 子系统反向依赖此处避免循环。补 httpx 测试依赖。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: 动态配置基础 ConfigStore（src/config_store/）

**依赖：** Task 1（types，提供基础包结构）。**被依赖：** Task 5（llm_config）、Task 9（prompts）。

**Files:**
- Create: `src/config_store/__init__.py`（空包）, `src/config_store/store.py`
- Modify: `src/storage/models.py`（追加 `AppConfigRow` 通用 KV 表）
- Test: `tests/test_config_store.py`

**设计要点：** 通用动态配置 KV（`key PK / value_json Text / version BigInt / updated_at`），是页面配置模型（llm_config / prompts）的基础设施。`ConfigStore` 提供 `get(key, default=None)`（内存缓存优先，miss 读 PG 回填）、`set(key, value)`（写 PG + bump version + 刷新内存）、`refresh()`。P0b 单进程内存缓存即可，跨进程广播 P5 再上 Redis pub/sub（预留接口注释）。`llm_config`/`prompts` 本 plan 选独立结构化表（字段固定更清晰），但 `AppConfigRow` 作为通用 escape hatch 保留（未来任意 key/value 配置可走此表）。

- [ ] **Step 1: 建包**

Run:
```bash
cd /Users/liuxiangwu/PycharmProjects/nl2sql
mkdir -p src/config_store
touch src/config_store/__init__.py
```

- [ ] **Step 2: 写失败测试 `tests/test_config_store.py`**

```python
import json

import pytest

from src.config_store.store import ConfigStore
from src.storage.models import AppConfigRow
from src.storage.pg_client import AsyncSessionFactory, init_db


@pytest.fixture
async def store():
    await init_db("sqlite+aiosqlite:///:memory:")
    return ConfigStore()


@pytest.mark.asyncio
async def test_get_returns_default_when_absent(store):
    assert await store.get("nope", default="fallback") == "fallback"


@pytest.mark.asyncio
async def test_get_default_none_when_no_default(store):
    assert await store.get("nope") is None


@pytest.mark.asyncio
async def test_set_then_get(store):
    await store.set("k1", {"v": 1})
    assert await store.get("k1") == {"v": 1}


@pytest.mark.asyncio
async def test_set_bumps_version(store):
    v1 = await store.set("k", "a")
    assert v1 == 1
    v2 = await store.set("k", "b")
    assert v2 == 2
    v3 = await store.set("k", "c")
    assert v3 == 3


@pytest.mark.asyncio
async def test_get_uses_cache_after_set(store):
    """set 后立即写入内存缓存，后续 get 不查 PG。"""
    await store.set("k", "v")
    assert await store.get("k") == "v"
    assert await store.get("k") == "v"


@pytest.mark.asyncio
async def test_refresh_reloads_from_pg(store):
    """绕过 store 直接改 PG 模拟外部写入，refresh 后读到新值。"""
    await store.set("k", "v1")
    assert await store.get("k") == "v1"
    async with AsyncSessionFactory() as s:
        row = await s.get(AppConfigRow, "k")
        row.value_json = json.dumps("v2")
        await s.commit()
    await store.refresh()
    assert await store.get("k") == "v2"


@pytest.mark.asyncio
async def test_set_handles_complex_value(store):
    await store.set("complex", {"nested": [1, 2, {"x": "y"}]})
    assert await store.get("complex") == {"nested": [1, 2, {"x": "y"}]}


@pytest.mark.asyncio
async def test_new_key_first_version_is_one(store):
    """首次 set 某个 key 时版本从 1 起。"""
    v = await store.set("fresh", "v")
    assert v == 1
```

- [ ] **Step 3: 运行验证失败**

Run: `pytest tests/test_config_store.py -v`
Expected: FAIL（`No module named 'src.config_store.store'`）

- [ ] **Step 4: 追加 `AppConfigRow` 表到 `src/storage/models.py`**

在 `src/storage/models.py` 末尾（`QueryResult` 类之后）追加：

```python
class AppConfigRow(Base):
    """通用动态配置 KV（页面配置模型基础）。
    llm_config / prompts 本 plan 选独立结构化表，此表作为通用 escape hatch：
    未来任意 key/value 配置（feature flag、阈值、开关）可走此表。"""
    __tablename__ = "app_config"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())
```

- [ ] **Step 5: 实现 `src/config_store/store.py`**

```python
"""动态配置基础：通用 KV + 内存缓存 + 版本号（页面配置模型基础设施）。
- get 内存缓存优先，miss 读 PG 回填缓存
- set 写 PG + bump version + 刷新内存
ponytail: P0b 单进程内存缓存即足够；跨进程广播失效 P5 改 Redis pub/sub（预留 refresh 接口）。"""
from __future__ import annotations

import json
from typing import Any

from src.logging import get_logger
from src.storage.models import AppConfigRow
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)


class ConfigStore:
    """通用动态配置 KV。被 llm_config / prompts 等子系统复用模式，
    也可直接存任意 JSON 配置（feature flag、阈值等）。"""

    def __init__(self) -> None:
        # key -> (value, version)
        self._cache: dict[str, tuple[Any, int]] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        """读配置：内存缓存优先，miss 读 PG 回填缓存。未配置返回 default。"""
        cached = self._cache.get(key)
        if cached is not None:
            return cached[0]
        async with AsyncSessionFactory() as s:
            row = await s.get(AppConfigRow, key)
            if row is None:
                return default
            value = json.loads(row.value_json)
            version = row.version
        self._cache[key] = (value, version)
        return value

    async def set(self, key: str, value: Any) -> int:
        """写配置：upsert PG + bump version + 刷新内存。返回新版本号。"""
        async with AsyncSessionFactory() as s:
            row = await s.get(AppConfigRow, key)
            value_json = json.dumps(value, ensure_ascii=False)
            if row:
                row.value_json = value_json
                row.version += 1
                new_version = row.version
            else:
                s.add(AppConfigRow(key=key, value_json=value_json, version=1))
                new_version = 1
            await s.commit()
        self._cache[key] = (value, new_version)
        log.info("配置更新 key=%s version=%s", key, new_version)
        return new_version

    async def refresh(self) -> None:
        """清缓存重新加载已缓存的 key（admin 改完手动刷）。
        ponytail: P5 跨进程时改成订阅 Redis pub/sub 频道自动失效。"""
        keys = list(self._cache.keys())
        self._cache.clear()
        for k in keys:
            await self.get(k)
```

- [ ] **Step 6: 运行验证通过**

Run: `pytest tests/test_config_store.py -v`
Expected: PASS（8 测试绿）

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "feat(p0b): 动态配置基础 ConfigStore（config_store/store.py）

通用 KV 表 + 内存缓存 + 版本号，是 llm_config/prompts 的基础设施。
P0b 单进程内存缓存，P5 跨进程改 Redis pub/sub。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: ToolRegistry + coerce_tool_args（tools/registry.py）

**Files:**
- Create: `src/tools/registry.py`
- Test: `tests/test_tool_registry.py`（本任务写 registry 部分，Task 4 追加 builtins 部分）

**设计要点：** coerce_tool_args 按 JSON Schema 强转 LLM 字符串参数（spec 6.2.3，治 Qwen 偶发返回字符串）。execute 三层兜底（不存在/不可用/异常）返回错误 ToolResult，回灌 LLM 触发错误自愈（spec 6.1）。openai_tools 每次现算不缓存（ponytail：工具数 ≤10）。

- [ ] **Step 1: 写失败测试 `tests/test_tool_registry.py`（registry 部分）**

```python
import pytest

from src.core.types import (
    CancelToken, LoopContext, ToolDefinition, ToolResult,
)
from src.tools.registry import ToolRegistry, coerce_tool_args, require_module


@pytest.fixture
def ctx():
    return LoopContext(session_id="s", user_id="u", trace_id="t")


@pytest.fixture
def cancel_token():
    return CancelToken()


# ---- coerce_tool_args ----
def test_coerce_integer():
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    assert coerce_tool_args(schema, {"limit": "100"}) == {"limit": 100}


def test_coerce_number():
    schema = {"type": "object", "properties": {"pi": {"type": "number"}}}
    assert coerce_tool_args(schema, {"pi": "3.14"})["pi"] == 3.14


def test_coerce_boolean_truthy():
    schema = {"type": "object", "properties": {"flag": {"type": "boolean"}}}
    assert coerce_tool_args(schema, {"flag": "true"})["flag"] is True
    assert coerce_tool_args(schema, {"flag": "yes"})["flag"] is True
    assert coerce_tool_args(schema, {"flag": "1"})["flag"] is True


def test_coerce_boolean_falsy():
    schema = {"type": "object", "properties": {"flag": {"type": "boolean"}}}
    assert coerce_tool_args(schema, {"flag": "0"})["flag"] is False
    assert coerce_tool_args(schema, {"flag": "no"})["flag"] is False


def test_coerce_array():
    schema = {"type": "object", "properties": {"ids": {"type": "array"}}}
    assert coerce_tool_args(schema, {"ids": "[1,2]"})["ids"] == [1, 2]


def test_coerce_object():
    schema = {"type": "object", "properties": {"obj": {"type": "object"}}}
    assert coerce_tool_args(schema, {"obj": '{"k":1}'})["obj"] == {"k": 1}


def test_coerce_union_type_takes_first_non_null():
    schema = {"type": "object", "properties": {"x": {"type": ["integer", "null"]}}}
    assert coerce_tool_args(schema, {"x": "5"})["x"] == 5


def test_coerce_invalid_keeps_original():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    out = coerce_tool_args(schema, {"n": "abc"})
    assert out["n"] == "abc"  # 强转失败保留原值


def test_coerce_non_string_untouched():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    out = coerce_tool_args(schema, {"n": 5})
    assert out["n"] == 5


def test_coerce_missing_schema_field():
    schema = {"type": "object", "properties": {}}
    out = coerce_tool_args(schema, {"extra": "x"})
    assert out["extra"] == "x"


# ---- require_module ----
def test_require_module_present():
    assert require_module("json")() is True


def test_require_module_absent():
    assert require_module("__no_such_module_xyz__")() is False


# ---- ToolRegistry 注册/查询 ----
async def _ok_handler(args, ctx, tk):
    return ToolResult(summary=f"got {args}")


def test_register_available_defs_and_openai_tools():
    reg = ToolRegistry()
    on = ToolDefinition(name="on", description="d1", parameters={"type": "object"},
                        handler=_ok_handler)
    off = ToolDefinition(name="off", description="d2", parameters={"type": "object"},
                         handler=_ok_handler, availability=lambda: False)
    reg.register(on)
    reg.register(off)
    # available_defs 只含可用工具
    names = {td.name for td in reg.available_defs()}
    assert names == {"on"}
    # openai_tools 格式正确且只含可用
    tools = reg.openai_tools()
    assert len(tools) == 1
    assert tools[0] == {"type": "function",
                        "function": {"name": "on", "description": "d1",
                                     "parameters": {"type": "object"}}}


def test_get_hit_and_miss():
    reg = ToolRegistry()
    td = ToolDefinition(name="x", description="d", parameters={}, handler=_ok_handler)
    reg.register(td)
    assert reg.get("x") is td
    assert reg.get("nope") is None


# ---- ToolRegistry.execute ----
@pytest.mark.asyncio
async def test_execute_coerces_args(ctx, cancel_token):
    seen = {}

    async def h(args, c, t):
        seen.update(args)
        return ToolResult(summary="ok")

    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="t", description="d",
        parameters={"type": "object", "properties": {"n": {"type": "integer"}}},
        handler=h))
    await reg.execute("t", {"n": "5"}, ctx, cancel_token)
    assert seen == {"n": 5}


@pytest.mark.asyncio
async def test_execute_unknown_tool(ctx, cancel_token):
    reg = ToolRegistry()
    r = await reg.execute("ghost", {}, ctx, cancel_token)
    assert "不存在" in r.summary
    assert r.finished is False and r.suspended is False


@pytest.mark.asyncio
async def test_execute_unavailable(ctx, cancel_token):
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="x", description="d", parameters={},
                                handler=_ok_handler, availability=lambda: False))
    r = await reg.execute("x", {}, ctx, cancel_token)
    assert "不可用" in r.summary


@pytest.mark.asyncio
async def test_execute_handler_exception(ctx, cancel_token):
    async def boom(args, c, t):
        raise RuntimeError("炸了")

    reg = ToolRegistry()
    reg.register(ToolDefinition(name="x", description="d", parameters={}, handler=boom))
    r = await reg.execute("x", {}, ctx, cancel_token)
    assert "执行出错" in r.summary
    assert "炸了" in r.summary
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_tool_registry.py -v`
Expected: FAIL（`No module named 'src.tools.registry'`）

- [ ] **Step 3: 实现 `src/tools/registry.py`**

```python
"""工具注册表：动态 schema 重建 + coerce 强转 + 可用性过滤（spec 6.2）。
- openai_tools: 每次按可用集合现算，防 Qwen 幻觉调隐藏工具（spec 6.2.2）
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
    Qwen 偶尔把 integer/number/boolean/array/object 返回成字符串，统一兜底。
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
    """闭包：模块可导入则工具可见（自动隐藏缺依赖工具，spec 6.2.1）。
    query_metadata 等未来工具用此实现运行时可用性检查。"""
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
        """运行时可用性过滤后的 ToolDefinition 列表（管理端/execute 用）。"""
        return [td for td in self._defs.values() if td.availability()]

    def openai_tools(self) -> list[dict]:
        """LLM 侧 schema：[{"type":"function","function":{name,description,parameters}}]。
        动态重建防 Qwen 幻觉调隐藏工具（spec 6.2.2）。"""
        return [
            {"type": "function",
             "function": {"name": td.name, "description": td.description,
                          "parameters": td.parameters}}
            for td in self.available_defs()
        ]

    async def execute(self, name: str, args: dict,
                      ctx: LoopContext, cancel_token: CancelToken) -> ToolResult:
        """按名取工具 → coerce 参数 → 调 handler。
        工具不存在/不可用/抛异常均返回带错误摘要的 ToolResult，回灌 LLM 触发错误自愈（spec 6.1）。"""
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
            # ponytail: 异常文本回灌 LLM 触发自愈；生产前需脱敏 SQL/路径
            return ToolResult(summary=f"工具 '{name}' 执行出错: {e}")
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_tool_registry.py -v`
Expected: PASS（coerce + registry 共约 15 测试绿，builtins 部分待 Task 4）

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat(p0b): ToolRegistry + coerce_tool_args（tools/registry.py）

动态 schema 重建防 Qwen 幻觉、参数类型强转、运行时可用性过滤、
execute 错误兜底回灌 LLM 触发错误自愈。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: 内置工具 echo/finish/ask_user（tools/builtins.py）

**Files:**
- Create: `src/tools/builtins.py`
- Test: `tests/test_tool_registry.py`（追加 builtins 部分）

**设计要点：** 三个内置工具只置标志位（finished/suspended），不持久化——分工边界：工具零依赖，持久化由 agent_loop 观察 flags 后委托 SessionState。这保持 tool_registry 包不依赖 P0a 的 PG/Redis。

- [ ] **Step 1: 追加失败测试到 `tests/test_tool_registry.py` 末尾**

```python
# ==== builtins 测试（Task 4 追加）====
from src.tools.builtins import ECHO, FINISH, ASK_USER, default_registry


@pytest.mark.asyncio
async def test_echo_handler(ctx, cancel_token):
    r = await ECHO.handler({"text": "hi"}, ctx, cancel_token)
    assert r.summary == "echo: hi"
    assert r.finished is False and r.suspended is False


@pytest.mark.asyncio
async def test_finish_handler(ctx, cancel_token):
    r = await FINISH.handler({"answer": "done"}, ctx, cancel_token)
    assert r.summary == "done"
    assert r.finished is True


@pytest.mark.asyncio
async def test_ask_user_handler(ctx, cancel_token):
    r = await ASK_USER.handler({"question": "哪个月?"}, ctx, cancel_token)
    assert r.summary == "哪个月?"
    assert r.suspended is True


def test_default_registry_three_tools():
    reg = default_registry()
    names = {td.name for td in reg.available_defs()}
    assert names == {"echo", "finish", "ask_user"}
    tools = reg.openai_tools()
    assert len(tools) == 3
    # 格式正确
    for t in tools:
        assert t["type"] == "function"
        assert "name" in t["function"]
        assert "parameters" in t["function"]


def test_registry_hides_unavailable_tool():
    from src.tools.registry import ToolRegistry
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="hidden", description="d", parameters={"type": "object"},
        handler=ECHO.handler, availability=require_module("__no_such_module__")))
    assert reg.openai_tools() == []  # 缺依赖自动隐藏
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_tool_registry.py -v -k "echo or finish or ask_user or default_registry or hides"`
Expected: FAIL（`No module named 'src.tools.builtins'`）

- [ ] **Step 3: 实现 `src/tools/builtins.py`**

```python
"""内置工具：echo(stub)/finish/ask_user（spec 6.2）。
finish/ask_user 只置标志位，由 AgentLoop 观察后决定终止/挂起。
工具本身不持久化，保持 tools 包零 P0a 依赖（PG/Redis 由 loop 侧 SessionState 管）。"""
from __future__ import annotations

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.tools.registry import ToolRegistry


async def _echo(args: dict, ctx: LoopContext, cancel_token: CancelToken) -> ToolResult:
    """回显输入文本（测试用 stub，演示工具调用链路）。"""
    return ToolResult(summary=f"echo: {args.get('text', '')}")


async def _finish(args: dict, ctx: LoopContext, cancel_token: CancelToken) -> ToolResult:
    """给出最终答案并结束本轮对话。agent_loop 观察 finished=True 后终止循环。"""
    return ToolResult(summary=args.get("answer", ""), finished=True)


async def _ask_user(args: dict, ctx: LoopContext, cancel_token: CancelToken) -> ToolResult:
    """向用户提问以澄清需求。agent_loop 观察 suspended=True 后存 checkpoint 并挂起（spec 6.4）。"""
    return ToolResult(summary=args.get("question", ""), suspended=True)


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
    description="向用户提问以澄清需求（如缺参、歧义）。调用后本轮暂停等待用户回答。",
    parameters={"type": "object",
                "properties": {"question": {"type": "string", "description": "要问用户的问题"}},
                "required": ["question"]},
    handler=_ask_user,
)


def default_registry() -> ToolRegistry:
    """注册 echo / finish / ask_user 三个基础工具，返回新 ToolRegistry。"""
    reg = ToolRegistry()
    for td in (ECHO, FINISH, ASK_USER):
        reg.register(td)
    return reg
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_tool_registry.py -v`
Expected: PASS（全部约 20 测试绿）

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat(p0b): 内置工具 echo/finish/ask_user（tools/builtins.py）

工具只置标志位不持久化，分工边界清晰：loop 观察 flags 委托 SessionState。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: 动态 LLM 配置 + admin API（llm_config + LLMService 改造）

**依赖：** Task 1（types）、Task 2（ConfigStore 模式参考，但 LlmConfigRow 用独立结构化表）。**被依赖：** Task 7（AgentLoop 通过 LLMService 间接受益）、Task 13（spike 用 LLMService）。

**协议约定：** 固定 OpenAI 兼容协议（base_url 可换），本 plan 不做 Anthropic/Gemini 原生协议适配。页面 UI 留 P5，P0b 只做后端动态配置基础设施 + admin API。

**Files:**
- Create: `src/web/routes/admin_llm.py`
- Modify: `src/storage/models.py`（追加 `LlmConfigRow` 单行结构化表）
- Modify: `src/llm/service.py`（`LLMService` 启动/调用时优先读动态，fallback `ApplicationConfig.llm` yml）
- Test: `tests/test_llm_config.py`, `tests/test_routes_admin_llm.py`

**设计要点：** `LlmConfigRow` 单行表（`id='default'`，固定字段 model/base_url/api_key/temperature/timeout/enabled/version）。`LLMService` 加 `_dynamic` 内存缓存 + `_load_dynamic()` 读 PG + `_resolve_config()` 动态优先 fallback yml + `reset_dynamic()`（admin PUT 后调用，下次 `_ensure_client` 重建）。admin API：`GET /api/admin/llm-config`（返回动态配置或标记 source=yaml）、`PUT /api/admin/llm-config`（写入 + 调 `llm_service.reset_dynamic()` 热更新）。

- [ ] **Step 1: 写失败测试 `tests/test_llm_config.py`**

```python
import pytest

from src.config import LLMConfig
from src.llm.service import LLMService
from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory, init_db


@pytest.fixture
async def svc():
    await init_db("sqlite+aiosqlite:///:memory:")
    yaml_cfg = LLMConfig(api_key="yaml-key", api_base="yaml-url",
                         model="yaml-model", temperature=0.0, timeout=60)
    return LLMService(yaml_cfg)


@pytest.mark.asyncio
async def test_fallback_to_yaml_when_no_dynamic(svc):
    cfg = await svc._resolve_config()
    assert cfg.model == "yaml-model"
    assert cfg.api_base == "yaml-url"
    assert cfg.api_key == "yaml-key"


@pytest.mark.asyncio
async def test_dynamic_overrides_yaml(svc):
    async with AsyncSessionFactory() as s:
        s.add(LlmConfigRow(id="default", model="dyn-model", base_url="dyn-url",
                           api_key="dyn-key", temperature=0.7, timeout=120,
                           enabled=True, version=1))
        await s.commit()
    cfg = await svc._resolve_config()
    assert cfg.model == "dyn-model"
    assert cfg.api_base == "dyn-url"
    assert cfg.temperature == 0.7
    assert cfg.timeout == 120


@pytest.mark.asyncio
async def test_disabled_dynamic_falls_back_to_yaml(svc):
    async with AsyncSessionFactory() as s:
        s.add(LlmConfigRow(id="default", model="dyn", base_url="dyn",
                           api_key="x", temperature=0.0, timeout=60,
                           enabled=False, version=1))
        await s.commit()
    cfg = await svc._resolve_config()
    assert cfg.model == "yaml-model"  # enabled=False → fallback


@pytest.mark.asyncio
async def test_reset_dynamic_reloads_on_next_call(svc):
    """admin PUT 调 reset_dynamic 后，下次 _resolve_config 读最新 PG。"""
    async with AsyncSessionFactory() as s:
        s.add(LlmConfigRow(id="default", model="v1", base_url="u1",
                           api_key="k", temperature=0.0, timeout=60,
                           enabled=True, version=1))
        await s.commit()
    cfg = await svc._resolve_config()
    assert cfg.model == "v1"
    # 模拟外部 PUT 改 PG（绕过 service 缓存）
    async with AsyncSessionFactory() as s:
        row = await s.get(LlmConfigRow, "default")
        row.model = "v2"
        await s.commit()
    # 未 reset：仍读缓存 v1
    cfg = await svc._resolve_config()
    assert cfg.model == "v1"
    # reset 后：清缓存，下次读最新 v2
    svc.reset_dynamic()
    cfg = await svc._resolve_config()
    assert cfg.model == "v2"


@pytest.mark.asyncio
async def test_load_dynamic_failure_falls_back_silently(svc):
    """PG 异常时不抛、降级 yml。"""
    # 直接构造异常场景：monkeypatch AsyncSessionFactory 抛错
    import src.llm.service as svc_mod

    class BoomFactory:
        def __call__(self):
            raise RuntimeError("pg down")

    orig = svc_mod.AsyncSessionFactory
    svc_mod.AsyncSessionFactory = BoomFactory()
    try:
        cfg = await svc._resolve_config()
        assert cfg.model == "yaml-model"  # 降级 yml
    finally:
        svc_mod.AsyncSessionFactory = orig
```

- [ ] **Step 2: 写失败测试 `tests/test_routes_admin_llm.py`**

```python
import pytest
import httpx
from fastapi import FastAPI

from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory, init_db
from src.web.routes.admin_llm import build_admin_llm_router


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    app = FastAPI()
    app.include_router(build_admin_llm_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_get_returns_yaml_source_when_no_dynamic(client):
    resp = await client.get("/api/admin/llm-config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "yaml"
    assert body["dynamic"] is None


@pytest.mark.asyncio
async def test_put_creates_dynamic_config(client):
    resp = await client.put("/api/admin/llm-config", json={
        "model": "qwen3", "base_url": "http://gw", "api_key": "k",
        "temperature": 0.5, "timeout": 90, "enabled": True})
    assert resp.status_code == 200
    resp = await client.get("/api/admin/llm-config")
    body = resp.json()
    assert body["source"] == "dynamic"
    assert body["dynamic"]["model"] == "qwen3"
    assert body["dynamic"]["temperature"] == 0.5


@pytest.mark.asyncio
async def test_put_then_put_bumps_version(client):
    await client.put("/api/admin/llm-config", json={
        "model": "v1", "base_url": "u", "enabled": True})
    await client.put("/api/admin/llm-config", json={
        "model": "v2", "base_url": "u", "enabled": True})
    resp = await client.get("/api/admin/llm-config")
    assert resp.json()["dynamic"]["version"] == 2


@pytest.mark.asyncio
async def test_put_triggers_llm_service_reset(client):
    """传 llm_service 时 PUT 后调 reset_dynamic。"""
    reset_calls = {"n": 0}

    class FakeLLMService:
        def reset_dynamic(self):
            reset_calls["n"] += 1

    app = FastAPI()
    app.include_router(build_admin_llm_router(FakeLLMService()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.put("/api/admin/llm-config", json={
            "model": "x", "base_url": "u", "enabled": True})
    assert reset_calls["n"] == 1
```

- [ ] **Step 3: 运行验证失败**

Run: `pytest tests/test_llm_config.py tests/test_routes_admin_llm.py -v`
Expected: FAIL（`ImportError: cannot import name 'LlmConfigRow'` / `No module named 'src.web.routes.admin_llm'`）

- [ ] **Step 4: 追加 `LlmConfigRow` 表到 `src/storage/models.py`**

在 `src/storage/models.py` 末尾追加：

```python
class LlmConfigRow(Base):
    """动态 LLM 配置（admin 后台可改，热更新）。
    单行表：id 固定为 'default'。LLMService 调用时优先读此表（enabled=True），
    无则 fallback 到 application.yml 的 llm 段。"""
    __tablename__ = "llm_config"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    model: Mapped[str] = mapped_column(String(128))
    base_url: Mapped[str] = mapped_column(String(256))
    api_key: Mapped[str] = mapped_column(String(256))
    temperature: Mapped[float] = mapped_column(default=0.0)
    timeout: Mapped[int] = mapped_column(default=60)
    enabled: Mapped[bool] = mapped_column(default=True)
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())
```

- [ ] **Step 5: 改造 `src/llm/service.py` 的 `LLMService`**

替换 `LLMService` 类整体（保留模块级的 `collect_stream_result` 和 `config_api_key` 函数不动）：

```python
class LLMService:
    """LLM 服务：ChatOpenAI 封装 + 动态配置（页面可改）。
    调用时优先读 PG 里的 LlmConfigRow（enabled=True），fallback 到 yml 静态配置。
    admin PUT 后调 reset_dynamic() 触发热更新（清缓存 + 重建 client）。"""

    def __init__(self, config: LLMConfig):
        self._config = config          # yml 静态配置（fallback 兜底）
        self._client = None
        self._dynamic: dict | None = None  # 内存缓存的动态配置（None=未加载）

    async def _load_dynamic(self) -> dict | None:
        """从 PG 读 LlmConfigRow（enabled=True）。无则返回 None。
        ponytail: PG 异常时降级返回 None（fallback yml），不让配置查询中断对话。"""
        if self._dynamic is not None:
            return self._dynamic
        try:
            from src.storage.models import LlmConfigRow
            from src.storage.pg_client import AsyncSessionFactory
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
            log.warning("读动态 LLM 配置失败，fallback yml: %s", e)
            return None
        return self._dynamic

    def reset_dynamic(self) -> None:
        """admin PUT 后调用：清动态缓存 + 置空 client。下次调用按最新配置重建。"""
        self._dynamic = None
        self._client = None

    async def _resolve_config(self) -> LLMConfig:
        """动态优先，无则 fallback yml 静态。"""
        dyn = await self._load_dynamic()
        if dyn:
            return LLMConfig(**dyn)
        return self._config

    def _ensure_client(self, cfg: LLMConfig):
        if self._client is None:
            from langchain_openai import ChatOpenAI
            self._client = ChatOpenAI(
                api_key=config_api_key(cfg),
                base_url=cfg.api_base,
                model=cfg.model,
                temperature=cfg.temperature,
                timeout=cfg.timeout,
                streaming=True,
            )
        return self._client

    async def chat(self, messages: list[dict], tools: list | None = None):
        """非流式一次调用（loop 主用）。每次 resolve 配置（动态可能被 admin 改）。"""
        cfg = await self._resolve_config()
        client = self._ensure_client(cfg)
        kwargs = {"messages": messages}
        if tools:
            kwargs["tools"] = tools
        return await client.ainvoke(**kwargs)

    async def chat_stream(self, messages: list[dict], tools: list | None = None):
        """流式生成，yield chunk。"""
        cfg = await self._resolve_config()
        client = self._ensure_client(cfg)
        kwargs = {"messages": messages}
        if tools:
            kwargs["tools"] = tools
        async for chunk in client.astream(**kwargs):
            yield chunk
```

- [ ] **Step 6: 实现 `src/web/routes/admin_llm.py`**

```python
"""admin LLM 配置路由：GET/PUT /api/admin/llm-config。
ponytail: 鉴权层 P5 管理后台再补；P0b 暴露路由供页面调试。
协议固定 OpenAI 兼容（base_url 可换），不做 Anthropic/Gemini 原生协议适配。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.storage.models import LlmConfigRow
from src.storage.pg_client import AsyncSessionFactory

DEFAULT_ID = "default"


class LlmConfigPayload(BaseModel):
    model: str
    base_url: str
    api_key: str = ""
    temperature: float = 0.0
    timeout: int = 60
    enabled: bool = True


def build_admin_llm_router(llm_service=None) -> APIRouter:
    """构造 admin LLM 配置路由。
    llm_service: 可选 LLMService 实例，PUT 成功后调其 reset_dynamic 触发热更新。"""
    router = APIRouter()

    @router.get("/api/admin/llm-config")
    async def get_llm_config() -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(LlmConfigRow, DEFAULT_ID)
        if row is None:
            # 无动态配置：标记 source=yaml，由前端读 yml 兜底值（路由层不引 yml）
            return {"source": "yaml", "dynamic": None}
        return {
            "source": "dynamic",
            "dynamic": {
                "model": row.model, "base_url": row.base_url,
                "api_key": row.api_key, "temperature": row.temperature,
                "timeout": row.timeout, "enabled": row.enabled,
                "version": row.version,
            },
        }

    @router.put("/api/admin/llm-config")
    async def put_llm_config(payload: LlmConfigPayload) -> dict:
        async with AsyncSessionFactory() as s:
            row = await s.get(LlmConfigRow, DEFAULT_ID)
            if row is None:
                s.add(LlmConfigRow(
                    id=DEFAULT_ID, model=payload.model, base_url=payload.base_url,
                    api_key=payload.api_key, temperature=payload.temperature,
                    timeout=payload.timeout, enabled=payload.enabled, version=1))
                version = 1
            else:
                row.model = payload.model
                row.base_url = payload.base_url
                row.api_key = payload.api_key
                row.temperature = payload.temperature
                row.timeout = payload.timeout
                row.enabled = payload.enabled
                row.version += 1
                version = row.version
            await s.commit()
        # 热更新：清 LLMService 动态缓存，下次调用重建 client 用新配置
        if llm_service is not None:
            llm_service.reset_dynamic()
        return {"ok": True, "version": version}

    return router
```

- [ ] **Step 7: 运行验证通过**

Run: `pytest tests/test_llm_config.py tests/test_routes_admin_llm.py -v`
Expected: PASS（9 测试绿）

- [ ] **Step 8: 提交**

```bash
git add -A
git commit -m "feat(p0b): 动态 LLM 配置 + admin API（llm_config + LLMService 改造）

LlmConfigRow 单行表 + LLMService 动态优先 fallback yml + reset_dynamic 热更新。
admin GET/PUT /api/admin/llm-config，PUT 后下次调用用新配置。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: 会话状态机 SessionState（core/session.py）

**Files:**
- Create: `src/core/session.py`
- Test: `tests/test_session_state.py`

**设计要点：** 状态机用 enum + 转换表（ponytail：不引 FSM 框架）。挂起存 PG LoopCheckpoint（P0a 已建表），恢复时把用户回答作为 ask_user 的 tool result append 到 messages（spec 6.4 核心）。状态-数据不一致自愈回退（不抛异常中断会话）。

- [ ] **Step 1: 写失败测试 `tests/test_session_state.py`**

```python
import pytest

from src.config import RedisConfig
from src.core.session import SessionState, SessionStatus
from src.memory.session import SessionManager
from src.storage.pg_client import init_db
from src.storage.redis_client import RedisClient


@pytest.fixture
async def state():
    await init_db("sqlite+aiosqlite:///:memory:")
    redis = RedisClient(RedisConfig(host="unreachable.invalid", port=1))
    await redis.connect()
    return SessionState(SessionManager(redis))


# ---- 状态机转换 ----
@pytest.mark.asyncio
async def test_transition_idle_to_running(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    assert (await state.current_status(sid)) == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_transition_illegal_raises(state):
    sid = await state._sm.create_session("u1", "web")
    with pytest.raises(ValueError):  # idle 不能直接跳 done
        await state.transition(sid, SessionStatus.DONE)


@pytest.mark.asyncio
async def test_transition_nonexistent_raises(state):
    with pytest.raises(ValueError):
        await state.transition("nope", SessionStatus.RUNNING)


@pytest.mark.asyncio
async def test_current_status_nonexistent_returns_none(state):
    assert await state.current_status("nope") is None


@pytest.mark.asyncio
async def test_transition_running_to_idle_for_cancel(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    await state.transition(sid, SessionStatus.IDLE)  # 取消场景合法
    assert (await state.current_status(sid)) == SessionStatus.IDLE


# ---- suspend ----
@pytest.mark.asyncio
async def test_suspend_creates_checkpoint_and_marks_status(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    cp_id = await state.suspend(sid, [{"role": "u", "content": "hi"}],
                                pending_tool="call_42")
    assert len(cp_id) == 32  # uuid4().hex
    assert (await state.current_status(sid)) == SessionStatus.AWAITING_CLARIFICATION


@pytest.mark.asyncio
async def test_suspend_requires_running(state):
    sid = await state._sm.create_session("u1", "web")
    with pytest.raises(ValueError):  # idle 不能转 awaiting
        await state.suspend(sid, [], pending_tool="x")


# ---- resume ----
@pytest.mark.asyncio
async def test_resume_injects_tool_result(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    await state.suspend(sid, [{"role": "u", "content": "hi"}], pending_tool="call_42")
    rc = await state.resume(sid, "北京")
    assert rc is not None
    assert rc.pending_tool == "call_42"
    assert rc.messages[-1] == {"role": "tool", "tool_call_id": "call_42",
                               "content": "北京"}
    assert (await state.current_status(sid)) == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_resume_idempotent_after_delete(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    await state.suspend(sid, [], pending_tool="c1")
    await state.resume(sid, "x")
    assert await state.resume(sid, "again") is None  # checkpoint 已删


@pytest.mark.asyncio
async def test_resume_non_suspended_returns_none(state):
    sid = await state._sm.create_session("u1", "web")
    assert await state.resume(sid, "x") is None


@pytest.mark.asyncio
async def test_resume_inconsistent_self_heals(state):
    """状态 awaiting 但无 checkpoint（数据不一致）→ 自愈回退 idle，不崩。"""
    sid = await state._sm.create_session("u1", "web")
    # 直接 set_status 到 awaiting，跳过 suspend（不写 checkpoint）
    await state._sm.set_status(sid, SessionStatus.AWAITING_CLARIFICATION.value)
    rc = await state.resume(sid, "x")
    assert rc is None
    assert (await state.current_status(sid)) == SessionStatus.IDLE


# ---- expire_suspended ----
@pytest.mark.asyncio
async def test_expire_suspended(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    await state.suspend(sid, [], pending_tool="c1")
    assert await state.expire_suspended(sid) is True
    assert (await state.current_status(sid)) == SessionStatus.IDLE


@pytest.mark.asyncio
async def test_expire_non_suspended_returns_false(state):
    sid = await state._sm.create_session("u1", "web")
    assert await state.expire_suspended(sid) is False


# ---- 端到端 ----
@pytest.mark.asyncio
async def test_suspend_resume_e2e(state):
    sid = await state._sm.create_session("u1", "web")
    await state.transition(sid, SessionStatus.RUNNING)
    msgs = [{"role": "u", "content": "hi"},
            {"role": "assistant", "content": "x"}]
    await state.suspend(sid, msgs, pending_tool="call_42")
    assert await state.is_suspended(sid) is True
    rc = await state.resume(sid, "北京")
    assert rc is not None
    assert len(rc.messages) == 3  # 原 2 条 + 注入的 tool result
    assert rc.messages[-1]["content"] == "北京"
    assert rc.messages[-1]["tool_call_id"] == "call_42"
    await state.transition(sid, SessionStatus.DONE)
    assert (await state.current_status(sid)) == SessionStatus.DONE
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_session_state.py -v`
Expected: FAIL（`No module named 'src.core.session'`）

- [ ] **Step 3: 实现 `src/core/session.py`**

```python
"""会话状态机 + ask_user 挂起/恢复（spec 6.4）。
状态存储委托 P0a SessionManager（Redis 热 + PG 持久），checkpoint 存 PG LoopCheckpoint。
状态转换严格按状态机表校验，非法转换抛 ValueError。"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import Enum

from src.logging import get_logger
from src.memory.session import SessionManager
from src.storage.models import LoopCheckpoint
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)


class SessionStatus(str, Enum):
    """会话状态（spec 6.4 状态机）。"""
    IDLE = "idle"
    RUNNING = "running"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    DONE = "done"
    ERROR = "error"


# 合法状态转换（spec 6.4 状态机 + RUNNING→IDLE 取消场景）
ALLOWED_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.IDLE: {SessionStatus.RUNNING},
    SessionStatus.RUNNING: {SessionStatus.RUNNING, SessionStatus.AWAITING_CLARIFICATION,
                            SessionStatus.DONE, SessionStatus.ERROR, SessionStatus.IDLE},
    SessionStatus.AWAITING_CLARIFICATION: {SessionStatus.RUNNING, SessionStatus.IDLE,
                                           SessionStatus.ERROR},
    SessionStatus.DONE: {SessionStatus.IDLE},
    SessionStatus.ERROR: {SessionStatus.IDLE, SessionStatus.RUNNING},
}


@dataclass
class ResumedContext:
    """恢复后的 loop 上下文，交给 AgentLoop 续跑。
    messages 已把用户回答作为 ask_user tool result append 进去。"""
    messages: list[dict]
    checkpoint_id: str
    pending_tool: str | None


class SessionState:
    """会话状态机 + ask_user 挂起/恢复。状态存储全部委托 SessionManager。"""

    def __init__(self, session_manager: SessionManager):
        self._sm = session_manager

    async def current_status(self, sid: str) -> SessionStatus | None:
        sess = await self._sm.get_session(sid)
        return SessionStatus(sess["status"]) if sess else None

    async def transition(self, sid: str, target: SessionStatus) -> None:
        """校验状态转换合法性，非法抛 ValueError。合法则委托 SessionManager 双写。"""
        cur = await self.current_status(sid)
        if cur is None:
            raise ValueError(f"会话不存在: {sid}")
        # ponytail: 转换表查表 O(1)，比散落 if 链易扩展
        if target not in ALLOWED_TRANSITIONS.get(cur, set()):
            raise ValueError(f"非法状态转换 {cur.value} -> {target.value} (sid={sid})")
        await self._sm.set_status(sid, target.value)

    async def is_suspended(self, sid: str) -> bool:
        return await self.current_status(sid) == SessionStatus.AWAITING_CLARIFICATION

    async def suspend(self, sid: str, messages: list[dict],
                      pending_tool: str | None = None) -> str:
        """挂起：存 LoopCheckpoint + 转 awaiting。受状态机约束，仅 running 可挂起。"""
        cp_id = uuid.uuid4().hex
        async with AsyncSessionFactory() as s:
            s.add(LoopCheckpoint(
                id=cp_id, session_id=sid,
                messages_json=json.dumps(messages, ensure_ascii=False),
                pending_tool=pending_tool))
            await s.commit()
        # transition 受 ALLOWED_TRANSITIONS 约束：仅 running 可转 awaiting
        await self.transition(sid, SessionStatus.AWAITING_CLARIFICATION)
        log.info("挂起 sid=%s checkpoint=%s pending_tool=%s", sid, cp_id, pending_tool)
        return cp_id

    async def resume(self, sid: str, user_reply: str) -> ResumedContext | None:
        """恢复：把用户回答作为 ask_user 工具结果注入 messages，删 checkpoint，转 running。
        非挂起态返回 None（orchestrator 走正常路径）。"""
        if not await self.is_suspended(sid):
            return None
        cp = await self._latest_checkpoint(sid)
        if cp is None:
            # ponytail: 状态与数据不一致时自愈回退，不抛异常中断会话
            log.warning("挂起态但无 checkpoint，回退 idle: sid=%s", sid)
            await self.transition(sid, SessionStatus.IDLE)
            return None
        messages = json.loads(cp.messages_json)
        # spec 6.4：用户回答作为 ask_user 的工具结果回灌，loop 断点续跑
        messages.append({"role": "tool",
                         "tool_call_id": cp.pending_tool,
                         "content": user_reply})
        await self._delete_checkpoint(cp.id)
        await self.transition(sid, SessionStatus.RUNNING)
        return ResumedContext(messages=messages, checkpoint_id=cp.id,
                              pending_tool=cp.pending_tool)

    async def expire_suspended(self, sid: str) -> bool:
        """挂起超时放弃：awaiting -> idle 并清 checkpoint（spec 6.4「超时自动放弃」）。"""
        if not await self.is_suspended(sid):
            return False
        cp = await self._latest_checkpoint(sid)
        if cp:
            await self._delete_checkpoint(cp.id)
        await self.transition(sid, SessionStatus.IDLE)
        return True

    async def _latest_checkpoint(self, sid: str):
        """取该会话最新一条 checkpoint（按 created_at desc）。"""
        async with AsyncSessionFactory() as s:
            return (await s.execute(
                LoopCheckpoint.__table__.select()
                .where(LoopCheckpoint.session_id == sid)
                .order_by(LoopCheckpoint.created_at.desc()).limit(1)
            )).first()

    async def _delete_checkpoint(self, cp_id: str) -> None:
        async with AsyncSessionFactory() as s:
            row = await s.get(LoopCheckpoint, cp_id)
            if row:
                await s.delete(row)
                await s.commit()
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_session_state.py -v`
Expected: PASS（13 测试绿）

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat(p0b): 会话状态机 SessionState + ask_user 挂起恢复（core/session.py）

enum+转换表状态机、LoopCheckpoint 持久化、用户回答作 tool result 注入断点恢复、
状态数据不一致自愈。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: AgentLoop 自主 ReAct 循环（core/agent_loop.py）

**Files:**
- Create: `src/core/agent_loop.py`
- Test: `tests/test_agent_loop.py`

**设计要点：** 同步 ReAct（spec 6.1）：LLM→解析 tool_calls→逐工具执行→摘要回灌→重复。护栏三件套：max_turns / 重复调用检测（同工具同参）/ ask_user 次数上限。取消令牌三处检查点（轮前/工具前/工具内透传）。assistant 消息的 tool_calls 转 OpenAI 格式（`function/arguments` 字符串）供下一轮 langchain 识别。ask_user 观察 suspended 标志后委托 SessionState 挂起。

- [ ] **Step 1: 写失败测试 `tests/test_agent_loop.py`**

```python
import asyncio
import pytest

from src.config import RedisConfig
from src.core.agent_loop import AgentLoop
from src.core.session import SessionState, SessionStatus
from src.core.types import CancelToken, SSEEvent, ToolResult
from src.memory.session import SessionManager
from src.storage.pg_client import init_db
from src.storage.redis_client import RedisClient


# ---- Fake 组件 ----
class FakeResp:
    """模拟 langchain AIMessage：有 .content 和 .tool_calls 属性。"""
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.last_messages = None

    async def chat(self, messages, tools=None):
        self.calls += 1
        self.last_messages = messages
        return self._responses.pop(0)


class FakeRegistry:
    def __init__(self, results=None):
        self._results = results or {}
        self.executed = []

    def openai_tools(self):
        return []

    async def execute(self, name, args, ctx, cancel_token):
        self.executed.append((name, args))
        return self._results.get(name, ToolResult(summary="ok"))


@pytest.fixture
async def env():
    await init_db("sqlite+aiosqlite:///:memory:")
    redis = RedisClient(RedisConfig(host="unreachable.invalid", port=1))
    await redis.connect()
    mgr = SessionManager(redis)
    return mgr, SessionState(mgr)


async def _collect(gen):
    return [e async for e in gen]


# ---- happy path（无工具）----
@pytest.mark.asyncio
async def test_happy_path_no_tools(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([FakeResp(content="答案是42")])
    loop = AgentLoop(llm, FakeRegistry(), state)
    events = await _collect(loop.run(sid, "u1", "你好", "t1", CancelToken()))
    types = [e.type for e in events]
    assert "assistant" in types
    assert types[-1] == "done"
    assert events[-1].data["answer"] == "答案是42"
    assert llm.calls == 1
    assert (await state.current_status(sid)) == SessionStatus.DONE


# ---- 工具执行 + result_id 旁路 ----
@pytest.mark.asyncio
async def test_tool_execution_with_result_id(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {"q": "x"}, "id": "c1"}]),
        FakeResp(content="汇总5行"),
    ])
    reg = FakeRegistry({"stub": ToolResult(summary="命中5行", result_id="r1")})
    loop = AgentLoop(llm, reg, state)
    events = await _collect(loop.run(sid, "u1", "查", "t1", CancelToken()))
    assert len(reg.executed) == 1
    tr = [e for e in events if e.type == "tool_result"][0]
    assert tr.data["summary"] == "命中5行"
    assert tr.data["result_id"] == "r1"
    assert events[-1].type == "done"
    assert events[-1].data["answer"] == "汇总5行"


@pytest.mark.asyncio
async def test_tool_summary_only_no_full_result_in_messages(env):
    """结果旁路：messages 里 tool 消息只含摘要，不含全量。"""
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {}, "id": "c1"}]),
        FakeResp(content="done"),
    ])
    reg = FakeRegistry({"stub": ToolResult(summary="摘要", result_id="r1")})
    loop = AgentLoop(llm, reg, state)
    await _collect(loop.run(sid, "u1", "查", "t1", CancelToken()))
    # 第二轮 LLM 收到的 messages 里 tool 消息 content 是摘要
    tool_msgs = [m for m in llm.last_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "摘要"
    assert "r1" not in tool_msgs[0]["content"]  # result_id 不进 messages


# ---- finish 工具终止 ----
@pytest.mark.asyncio
async def test_finish_tool_terminates_loop(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "finish", "args": {"answer": "完成"}, "id": "c1"}]),
    ])
    reg = FakeRegistry({"finish": ToolResult(summary="完成", finished=True)})
    loop = AgentLoop(llm, reg, state)
    events = await _collect(loop.run(sid, "u1", "你好", "t1", CancelToken()))
    assert events[-1].type == "done"
    assert events[-1].data["answer"] == "完成"
    assert llm.calls == 1  # finish 后不再调 LLM


# ---- 护栏 max_turns ----
@pytest.mark.asyncio
async def test_guard_max_turns(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    # 永远返回工具调用不收敛
    llm = FakeLLM([FakeResp(content="", tool_calls=[
        {"name": "stub", "args": {"i": i}, "id": f"c{i}"}) for _ in [0]][:1]
        for i in range(20)])
    reg = FakeRegistry({"stub": ToolResult(summary="ok")})
    loop = AgentLoop(llm, reg, state, max_turns=3)
    events = await _collect(loop.run(sid, "u1", "查", "t1", CancelToken()))
    types = [e.type for e in events]
    assert "warning" in types
    assert types[-1] == "done"
    tool_call_count = sum(1 for e in events if e.type == "tool_call")
    assert tool_call_count == 3


# ---- 护栏重复调用检测 ----
@pytest.mark.asyncio
async def test_guard_duplicate_call(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {"q": "x"}, "id": "c1"}]),
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {"q": "x"}, "id": "c2"}]),  # 同工具同参
        FakeResp(content="最终"),
    ])
    reg = FakeRegistry({"stub": ToolResult(summary="ok")})
    loop = AgentLoop(llm, reg, state)
    events = await _collect(loop.run(sid, "u1", "查", "t1", CancelToken()))
    # 第二次 stub 因重复跳过 execute
    assert len(reg.executed) == 1
    converged = [e for e in events if e.type == "tool_result"
                 and e.data.get("converged")]
    assert len(converged) == 1


# ---- 护栏 ask_user 次数上限 ----
@pytest.mark.asyncio
async def test_guard_ask_user_limit(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "ask_user", "args": {"question": "q1"}, "id": "a1"}]),
        FakeResp(content="最终答案"),
    ])
    reg = FakeRegistry({"ask_user": ToolResult(summary="q1", suspended=True)})
    loop = AgentLoop(llm, reg, state, max_ask_user=0)  # 立即触发上限
    events = await _collect(loop.run(sid, "u1", "查", "t1", CancelToken()))
    assert not await state.is_suspended(sid)  # 不挂起
    types = [e.type for e in events]
    assert "clarification_needed" not in types
    assert types[-1] == "done"


# ---- 取消：轮间 ----
@pytest.mark.asyncio
async def test_cancel_between_turns(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    tk = CancelToken()
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {}, "id": "c1"}]),
        FakeResp(content="不应到达"),
    ])
    reg = FakeRegistry({"stub": ToolResult(summary="ok")})
    loop = AgentLoop(llm, reg, state)
    events = []
    async for e in loop.run(sid, "u1", "查", "t1", tk):
        events.append(e)
        if e.type == "tool_result":
            tk.cancel()  # 第一轮工具执行后取消
    assert events[-1].type == "cancelled"


# ---- 取消：工具内 ----
@pytest.mark.asyncio
async def test_cancel_in_tool(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    tk = CancelToken()
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "stub", "args": {}, "id": "c1"}]),
        FakeResp(content="不应到达"),
    ])

    class CancelInExecute:
        def openai_tools(self):
            return []

        async def execute(self, name, args, ctx, cancel_token):
            cancel_token.cancel()  # 工具内取消
            return ToolResult(summary="ok")

    loop = AgentLoop(llm, CancelInExecute(), state)
    events = await _collect(loop.run(sid, "u1", "查", "t1", tk))
    assert events[-1].type == "cancelled"


# ---- ask_user 挂起 + checkpoint ----
@pytest.mark.asyncio
async def test_ask_user_suspends_and_checkpoints(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    llm = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "ask_user", "args": {"question": "哪个时间范围?"}, "id": "a1"}]),
    ])
    reg = FakeRegistry({"ask_user": ToolResult(summary="哪个时间范围?", suspended=True)})
    loop = AgentLoop(llm, reg, state)
    events = await _collect(loop.run(sid, "u1", "查发电量", "t1", CancelToken()))
    types = [e.type for e in events]
    assert "clarification_needed" in types
    cn = [e for e in events if e.type == "clarification_needed"][0]
    assert cn.data["question"] == "哪个时间范围?"
    assert types[-1] == "clarification_needed"  # 挂起后不发 done
    # 挂起本轮不 append tool 消息、不发 tool_result 事件
    # （SessionState.resume 唯一负责 append 用户回答，避免重复 tool_call_id）
    assert "tool_result" not in types
    assert await state.is_suspended(sid)
    # checkpoint 可被 resume 加载
    rc = await state.resume(sid, "6月")
    assert rc is not None
    assert rc.pending_tool == "a1"
    assert rc.messages[-1] == {"role": "tool", "tool_call_id": "a1",
                               "content": "6月"}
    # resume 后 messages 中 tool 消息唯一（仅 resume 注入的那条）
    tool_msgs = [m for m in rc.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1


# ---- ask_user 恢复后续跑（断点恢复）----
@pytest.mark.asyncio
async def test_ask_user_resume_continues_loop(env):
    mgr, state = env
    sid = await mgr.create_session("u1", "web")
    # 第一次 run：触发 ask_user 挂起
    llm1 = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "ask_user", "args": {"question": "q?"}, "id": "a1"}]),
    ])
    reg = FakeRegistry({
        "ask_user": ToolResult(summary="q?", suspended=True),
        "finish": ToolResult(summary="最终", finished=True),
    })
    loop1 = AgentLoop(llm1, reg, state)
    await _collect(loop1.run(sid, "u1", "查", "t1", CancelToken(), is_resume=False))
    assert await state.is_suspended(sid)
    # 第二次 run：恢复，注入用户回答，LLM 调 finish
    llm2 = FakeLLM([
        FakeResp(content="", tool_calls=[
            {"name": "finish", "args": {"answer": "结果"}, "id": "f1"}]),
    ])
    loop2 = AgentLoop(llm2, reg, state)
    events = await _collect(loop2.run(sid, "u1", "6月", "t1", CancelToken(),
                                     is_resume=True))
    types = [e.type for e in events]
    assert types[-1] == "done"
    assert events[-1].data["answer"] == "结果"
    assert (await state.current_status(sid)) == SessionStatus.DONE
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_agent_loop.py -v`
Expected: FAIL（`No module named 'src.core.agent_loop'`）

- [ ] **Step 3: 实现 `src/core/agent_loop.py`**

```python
"""Agent 编排核心：自主同步 ReAct 循环（spec 6.1）。
LLM→解析 tool_calls→逐工具执行→摘要回灌→重复，直到无 tool_calls / finish / 护栏 / 取消。
- ask_user 工具返回 suspended=True → 委托 SessionState.suspend 存 checkpoint + 发 clarification + return
- finish 工具返回 finished=True → 终止循环
- 护栏：max_turns / 重复调用检测 / ask_user 次数上限
- 取消令牌三处检查点：轮前 / 工具前 / 工具内（透传 cancel_token 给 registry.execute）
不组装上下文（ContextAssembler 在 P1 接），不直接操作 LoopCheckpoint（委托 SessionState）。"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from src.core.session import SessionState, SessionStatus
from src.core.types import CancelToken, LoopContext, SSEEvent, ToolResult
from src.llm.service import LLMService
from src.logging import get_logger
from src.tools.registry import ToolRegistry

log = get_logger(__name__)

ASK_USER = "ask_user"


def _args_key(args: dict) -> str:
    """dict 参数稳定序列化，用于重复调用检测。dict 不可哈希，转 json 字符串。"""
    return json.dumps(args, sort_keys=True, ensure_ascii=False)


def _normalize_args(raw) -> dict:
    """LLM 返回的 args 可能是 str（Qwen 网关偶发），归一化成 dict。
    ponytail: None/list/int 等非 dict 类型一律兜底成 {}，防下游 args.get 崩。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw else {}
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}  # None / list / int / 其他类型统一兜底


def _to_openai_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """把 langchain 解析格式 [{name,args,id}] 转成 OpenAI 消息格式，供下一轮 langchain 识别。
    OpenAI 要求 tool_calls 是 [{id, type:'function', function:{name, arguments:str}}]。"""
    return [
        {"id": tc.get("id"), "type": "function",
         "function": {"name": tc.get("name"),
                      "arguments": json.dumps(tc.get("args", {}), ensure_ascii=False)}}
        for tc in tool_calls
    ]


class AgentLoop:
    """自主同步 ReAct 循环。
    run 为 async generator，每轮 yield SSEEvent，末事件必为
    done/cancelled/error/clarification_needed 之一。
    """

    def __init__(self, llm: LLMService, registry: ToolRegistry, state: SessionState,
                 *, max_turns: int = 10, max_ask_user: int = 2):
        self._llm = llm
        self._registry = registry
        self._state = state
        self._max_turns = max_turns
        self._max_ask_user = max_ask_user

    async def run(self, session_id: str, user_id: str, user_msg: str,
                  trace_id: str, cancel_token: CancelToken,
                  is_resume: bool = False,
                  system_prompt: str | None = None) -> AsyncIterator[SSEEvent]:
        ctx = LoopContext(session_id, user_id, trace_id)
        # 1. 组装初始 messages（resume 从 checkpoint 加载，否则新建）
        # system_prompt 由 orchestrator 读 PromptStore 后透传（Task 9 prompts 集成点）
        msgs = await self._prepare_messages(session_id, user_msg, is_resume,
                                            system_prompt)

        last_answer = ""
        ask_count = 0
        prev_keys: set[tuple[str, str]] = set()
        turn = 0
        try:
            for turn in range(self._max_turns):
                cancel_token.check()
                yield SSEEvent("turn_start", {"turn": turn}, trace_id)
                resp = await self._llm.chat(msgs, self._registry.openai_tools())
                tool_calls = getattr(resp, "tool_calls", None) or []
                content = getattr(resp, "content", "") or ""
                if content:
                    last_answer = content
                # assistant 消息：tool_calls 转 OpenAI 格式存盘，供下一轮 LLM 识别
                msgs.append({"role": "assistant", "content": content,
                             "tool_calls": _to_openai_tool_calls(tool_calls)
                                           if tool_calls else []})
                yield SSEEvent("assistant", {"content": content, "turn": turn}, trace_id)

                if not tool_calls:
                    break  # LLM 给最终答案

                # 护栏：重复调用检测（同工具同参连调强制收敛，spec 6.1）
                cur_keys = {(tc.get("name"),
                             _args_key(_normalize_args(tc.get("args"))))
                            for tc in tool_calls}
                dup_keys = cur_keys & prev_keys
                prev_keys = cur_keys

                finished = False
                for tc in tool_calls:
                    cancel_token.check()
                    name = tc.get("name")
                    args = _normalize_args(tc.get("args"))
                    cid = tc.get("id")
                    yield SSEEvent("tool_call",
                                   {"name": name, "args": args, "id": cid}, trace_id)

                    # 重复调用：跳过 execute，注入收敛提示
                    if (name, _args_key(args)) in dup_keys:
                        tip = "已调用过相同工具和参数，请基于已有结果直接作答。"
                        msgs.append({"role": "tool", "tool_call_id": cid,
                                     "content": tip})
                        yield SSEEvent("tool_result",
                                       {"name": name, "summary": tip,
                                        "converged": True}, trace_id)
                        continue

                    # ask_user 次数上限护栏（spec 6.1）
                    if name == ASK_USER:
                        ask_count += 1
                        if ask_count > self._max_ask_user:
                            tip = "已达询问次数上限，请基于已有信息直接给出答案。"
                            msgs.append({"role": "tool", "tool_call_id": cid,
                                         "content": tip})
                            yield SSEEvent("tool_result",
                                           {"name": name, "summary": tip}, trace_id)
                            continue

                    result = await self._registry.execute(name, args, ctx, cancel_token)

                    if result.suspended:
                        # ask_user 挂起：本轮不 append tool 消息——
                        # SessionState.resume 会唯一负责把用户回答作为 tool result
                        # 注入（spec 6.4），若此处也 append 会产生重复 tool_call_id
                        # 消息污染下轮 LLM 上下文。直接 suspend + 发 clarification + return。
                        await self._state.suspend(session_id, msgs,
                                                  pending_tool=cid)
                        yield SSEEvent("clarification_needed",
                                       {"question": result.summary, "turn": turn},
                                       trace_id)
                        return  # 不发 done，由 orchestrator 结束 SSE

                    # 结果旁路：只回灌摘要给 LLM，result_id 只进事件（spec 6.5）
                    msgs.append({"role": "tool", "tool_call_id": cid,
                                 "content": result.summary})
                    yield SSEEvent("tool_result",
                                   {"name": name, "summary": result.summary,
                                    "result_id": result.result_id}, trace_id)

                    if result.finished:
                        last_answer = result.summary
                        finished = True
                        break  # 跳出工具循环

                if finished:
                    break  # 跳出主循环

                self._maybe_compress(msgs)
            else:
                # for-else：max_turns 跑满未收敛
                yield SSEEvent("warning",
                               {"reason": "max_turns", "max": self._max_turns},
                               trace_id)

            await self._state.transition(session_id, SessionStatus.DONE)
            yield SSEEvent("done", {"answer": last_answer}, trace_id)
        except asyncio.CancelledError:
            log.info("agent loop 被取消 sid=%s turn=%s", session_id, turn)
            await self._state.transition(session_id, SessionStatus.IDLE)
            yield SSEEvent("cancelled", {"turn": turn}, trace_id)
        except Exception as e:
            log.exception("agent loop 异常 sid=%s", session_id)
            # ponytail: transition(ERROR) 自身可能因状态机约束失败
            # （例如已 DONE→ERROR 非法转换），包 try/except 避免二次抛 ValueError
            # 覆盖原始异常。状态停留不影响 error 事件送达前端。
            try:
                await self._state.transition(session_id, SessionStatus.ERROR)
            except ValueError:
                log.warning("异常处理时状态转换失败，忽略: sid=%s", session_id)
            yield SSEEvent("error",
                           {"message": str(e), "answer": last_answer}, trace_id)

    async def _prepare_messages(self, session_id: str, user_msg: str,
                                is_resume: bool,
                                system_prompt: str | None = None) -> list[dict]:
        """组装 loop 初始 messages。
        resume：从 SessionState.resume 加载（checkpoint 已含 system prompt，不重复注入）。
        新会话：transition(RUNNING)，messages = [{system?}, {user}]。"""
        if is_resume:
            rc = await self._state.resume(session_id, user_msg)
            if rc is not None:
                return rc.messages
            # 数据不一致自愈：降级为新会话
            log.warning("resume 无 checkpoint，降级新会话: sid=%s", session_id)
        await self._state.transition(session_id, SessionStatus.RUNNING)
        msgs: list[dict] = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": user_msg})
        return msgs

    def _maybe_compress(self, msgs: list[dict]) -> None:
        """ponytail: 占位。P1 接 token 计数，按 80% 阈值压早期 tool 结果（spec 6.7）。"""
        return None
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_agent_loop.py -v`
Expected: PASS（11 测试绿）

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat(p0b): AgentLoop 自主 ReAct 循环 + 护栏 + 取消（core/agent_loop.py）

同步 ReAct、结果旁路、三件套护栏（max_turns/重复/ask_user 上限）、
cancel_token 贯穿、ask_user 委托 SessionState 挂起、OpenAI tool_calls 格式转换。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: Normalizer pass-through 框架（core/normalizer.py）

**Files:**
- Create: `src/core/normalizer.py`
- Test: `tests/test_normalizer.py`

**设计要点：** P0b 纯 pass-through（不传 hook 则原样返回），P2 注入三层 hook 启用真实纠错（spec 6.3）。纯函数级组件，零 PG/Redis 依赖。`corrections_to_json` 供 orchestrator 落 AuditTrace.corrections_json。

- [ ] **Step 1: 写失败测试 `tests/test_normalizer.py`**

```python
import json
import pytest

from src.core.normalizer import Correction, Normalizer, corrections_to_json


@pytest.mark.asyncio
async def test_passthrough_no_hooks():
    n = Normalizer()
    text, corrections = await n.normalize("新疆省分公司6月发电量")
    assert text == "新疆省分公司6月发电量"
    assert corrections == []


@pytest.mark.asyncio
async def test_passthrough_empty_string():
    n = Normalizer()
    text, corrections = await n.normalize("")
    assert text == ""
    assert corrections == []


@pytest.mark.asyncio
async def test_passthrough_none():
    n = Normalizer()
    text, corrections = await n.normalize(None)
    assert text == ""
    assert corrections == []


@pytest.mark.asyncio
async def test_passthrough_whitespace_only():
    n = Normalizer()
    text, corrections = await n.normalize("   ")
    assert corrections == []


def test_correction_fields():
    c = Correction(raw="新疆省", standard="新疆", confidence=0.95, source="typo")
    assert c.raw == "新疆省"
    assert c.standard == "新疆"
    assert c.confidence == 0.95
    assert c.source == "typo"


def test_corrections_to_json_empty():
    assert corrections_to_json([]) == "[]"


def test_corrections_to_json_non_empty():
    c = Correction(raw="新疆省", standard="新疆", confidence=0.95, source="typo")
    out = json.loads(corrections_to_json([c]))
    assert out == [{"raw": "新疆省", "standard": "新疆",
                    "confidence": 0.95, "source": "typo"}]


@pytest.mark.asyncio
async def test_dict_fn_minimal_layer():
    """传 dict_fn 时走字典层最小管线（confidence>=0.9 才替换）。"""
    async def fake_dict(text):
        return Correction(raw="新疆省", standard="新疆", confidence=0.99, source="typo")

    n = Normalizer(dict_fn=fake_dict)
    text, corrections = await n.normalize("新疆省发电量")
    assert "新疆" in text
    assert "新疆省" not in text
    assert len(corrections) == 1
    assert corrections[0].standard == "新疆"


@pytest.mark.asyncio
async def test_dict_fn_low_confidence_no_replace():
    """confidence < 0.9 不替换。"""
    async def fake_dict(text):
        return Correction(raw="新疆省", standard="新疆", confidence=0.5, source="typo")

    n = Normalizer(dict_fn=fake_dict)
    text, corrections = await n.normalize("新疆省发电量")
    assert text == "新疆省发电量"  # 低置信度不替换
    assert corrections == []
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_normalizer.py -v`
Expected: FAIL（`No module named 'src.core.normalizer'`）

- [ ] **Step 3: 实现 `src/core/normalizer.py`**

```python
"""名称纠错前置（spec 6.3）。P0b pass-through，P2 注入三层 hook 启用真实纠错。
纯函数级组件，零 PG/Redis 依赖；P2 才接 name_dict 表。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable

from src.logging import get_logger

log = get_logger(__name__)


@dataclass
class Correction:
    """单条修正记录（对应 spec 6.3 输出契约 + AuditTrace.corrections_json 元素）。"""
    raw: str            # 原值（用户输入中的错误写法）
    standard: str       # 标准值（纠错后）
    confidence: float   # 置信度 0.0-1.0
    source: str         # typo/homophone/admin_area/llm


# P2 三层 hook 签名（P0b 全 None = pass-through）：
DictFn = Callable[[str], Awaitable["Correction | None"]]      # 字典层：精确命中
FuzzyFn = Callable[[str], Awaitable["Correction | None"]]     # 模糊层：Levenshtein+拼音
LLMFn = Callable[[str], Awaitable["tuple[str, list[Correction]]"]]  # LLM 兜底：语义改写


class Normalizer:
    """名称纠错前置。orchestrator 在 user_msg 进 agent_loop 之前调用。
    P0b 默认 pass-through：不传 hook 则 normalize() 原样返回 (text, [])。
    P2 注入 dict_fn/fuzzy_fn/llm_fn 后启用三层管线（spec 6.3）。"""

    def __init__(self, dict_fn: DictFn | None = None,
                 fuzzy_fn: FuzzyFn | None = None,
                 llm_fn: LLMFn | None = None) -> None:
        self._dict_fn = dict_fn
        self._fuzzy_fn = fuzzy_fn
        self._llm_fn = llm_fn

    async def normalize(self, text: str) -> tuple[str, list[Correction]]:
        """返回 (标准化文本, 修正记录)。P0b 无 hook → 原样返回。"""
        if not text or not (self._dict_fn or self._fuzzy_fn or self._llm_fn):
            # ponytail: P0b pass-through，真实三层纠错 P2（spec 6.3）
            return text or "", []
        return await self._apply_layers(text)

    async def _apply_layers(self, text: str) -> tuple[str, list[Correction]]:
        """三层管线：字典(精确) → 模糊(Levenshtein+拼音) → LLM(语义兜底)。
        P0b 仅字典层最小可用（confidence>=0.9 才替换），模糊/LLM 留 P2。"""
        out, corrections = text, []
        if self._dict_fn:
            cor = await self._dict_fn(text)
            if cor and cor.confidence >= 0.9:
                out = out.replace(cor.raw, cor.standard)
                corrections.append(cor)
        # TODO(P2): fuzzy_fn 编辑距离+拼音索引，过阈值才替换
        # TODO(P2): llm_fn 整句语义改写，给字典候选+上下文让 LLM 选
        return out, corrections


def corrections_to_json(corrections: list[Correction]) -> str:
    """供 orchestrator 落 AuditTrace.corrections_json（JSON 字符串）。"""
    return json.dumps([asdict(c) for c in corrections], ensure_ascii=False)
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_normalizer.py -v`
Expected: PASS（8 测试绿）

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat(p0b): Normalizer pass-through 框架（core/normalizer.py）

P0b 纯 pass-through，P2 注入三层 hook 启用字典/模糊/LLM 纠错。
corrections_to_json 供审计落库。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 9: 场景化 prompt 管理 + orchestrator 集成（prompts）

**依赖：** Task 1（types）、Task 2（ConfigStore 模式参考，但 Prompt 用独立结构化表）。**被依赖：** Task 11（Orchestrator 通过 PromptStore 读 system_prompt 透传给 loop）。

**Files:**
- Create: `src/core/prompt_store.py`, `src/web/routes/admin_prompts.py`
- Modify: `src/storage/models.py`（追加 `Prompt` 表）
- Modify: `src/core/orchestrator.py`（Task 11 实现时按本任务约定集成；本任务先把 `PromptStore` + admin API 备齐）
- Test: `tests/test_prompt_store.py`, `tests/test_routes_admin_prompts.py`

**设计要点：** `Prompt` 表（`scene PK / content Text / version / enabled / updated_at`，scene 如 `default`/`attribution`/`correction`）。`PromptStore` 提供 `get(scene='default')`（内存缓存 + enabled 校验）、`upsert(scene, content, enabled)`、`delete(scene)`、`list_all()`、`refresh()`。admin API：`GET /api/admin/prompts`（list）、`GET /api/admin/prompts/{scene}`、`POST /api/admin/prompts`、`PUT /api/admin/prompts/{scene}`、`DELETE /api/admin/prompts/{scene}`。orchestrator 组装 system prompt 时按 `default` 场景读（Task 11 集成），通过 `loop.run(system_prompt=...)` 注入到 `messages[0]`。

**集成契约（与 Task 11 Orchestrator 约定）：**
- `Orchestrator.__init__(normalizer, loop, sessions, prompt_store=None)`：新增可选 `prompt_store`。
- `handle_message` 内调 `loop.run` 前读 `prompt_store.get("default")`，作为 `system_prompt` 透传给 loop（Task 7 `loop.run` 已支持 `system_prompt` 参数）。
- 未注入 `prompt_store` 时 `system_prompt=None`，loop 行为与原 plan 一致（无 system message），保持 backward compatible。

- [ ] **Step 1: 写失败测试 `tests/test_prompt_store.py`**

```python
import pytest

from src.core.prompt_store import PromptStore
from src.storage.pg_client import init_db


@pytest.fixture
async def store():
    await init_db("sqlite+aiosqlite:///:memory:")
    return PromptStore()


@pytest.mark.asyncio
async def test_get_returns_none_when_absent(store):
    assert await store.get("default") is None


@pytest.mark.asyncio
async def test_upsert_then_get(store):
    v = await store.upsert("default", "你是问数助手")
    assert v == 1
    assert await store.get("default") == "你是问数助手"


@pytest.mark.asyncio
async def test_upsert_bumps_version(store):
    v1 = await store.upsert("default", "v1")
    assert v1 == 1
    v2 = await store.upsert("default", "v2")
    assert v2 == 2


@pytest.mark.asyncio
async def test_disabled_returns_none(store):
    await store.upsert("default", "x", enabled=True)
    await store.upsert("default", "x", enabled=False)
    assert await store.get("default") is None  # enabled=False 不返回


@pytest.mark.asyncio
async def test_delete(store):
    await store.upsert("default", "x")
    assert await store.delete("default") is True
    assert await store.get("default") is None
    # 二次删返回 False（幂等指示）
    assert await store.delete("default") is False


@pytest.mark.asyncio
async def test_list_all(store):
    await store.upsert("default", "d")
    await store.upsert("attribution", "a")
    items = await store.list_all()
    scenes = {it["scene"] for it in items}
    assert scenes == {"default", "attribution"}


@pytest.mark.asyncio
async def test_get_uses_cache(store):
    await store.upsert("default", "v1")
    assert await store.get("default") == "v1"
    # 改 PG 模拟外部写入，未 refresh 仍读缓存
    from src.storage.models import Prompt as PromptRow
    from src.storage.pg_client import AsyncSessionFactory
    async with AsyncSessionFactory() as s:
        row = await s.get(PromptRow, "default")
        row.content = "v2"
        await s.commit()
    assert await store.get("default") == "v1"
    await store.refresh()
    assert await store.get("default") == "v2"


@pytest.mark.asyncio
async def test_multiple_scenes_independent(store):
    await store.upsert("default", "D")
    await store.upsert("correction", "C")
    assert await store.get("default") == "D"
    assert await store.get("correction") == "C"
```

- [ ] **Step 2: 写失败测试 `tests/test_routes_admin_prompts.py`**

```python
import pytest
import httpx
from fastapi import FastAPI

from src.core.prompt_store import PromptStore
from src.storage.pg_client import init_db
from src.web.routes.admin_prompts import build_admin_prompts_router


@pytest.fixture
async def client():
    await init_db("sqlite+aiosqlite:///:memory:")
    store = PromptStore()
    app = FastAPI()
    app.include_router(build_admin_prompts_router(store))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_list_empty(client):
    resp = await client.get("/api/admin/prompts")
    assert resp.status_code == 200
    assert resp.json() == {"prompts": []}


@pytest.mark.asyncio
async def test_post_then_get(client):
    resp = await client.post("/api/admin/prompts", json={
        "scene": "default", "content": "你是助手", "enabled": True})
    assert resp.status_code == 200
    assert resp.json()["version"] == 1
    resp = await client.get("/api/admin/prompts/default")
    assert resp.json()["content"] == "你是助手"


@pytest.mark.asyncio
async def test_put_updates_existing(client):
    await client.post("/api/admin/prompts", json={
        "scene": "default", "content": "v1", "enabled": True})
    resp = await client.put("/api/admin/prompts/default", json={
        "scene": "default", "content": "v2", "enabled": True})
    assert resp.json()["version"] == 2
    resp = await client.get("/api/admin/prompts/default")
    assert resp.json()["content"] == "v2"


@pytest.mark.asyncio
async def test_delete(client):
    await client.post("/api/admin/prompts", json={
        "scene": "default", "content": "x", "enabled": True})
    resp = await client.delete("/api/admin/prompts/default")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    # 二次删返回 deleted=False
    resp = await client.delete("/api/admin/prompts/default")
    assert resp.json()["deleted"] is False


@pytest.mark.asyncio
async def test_list_after_inserts(client):
    await client.post("/api/admin/prompts", json={
        "scene": "default", "content": "d", "enabled": True})
    await client.post("/api/admin/prompts", json={
        "scene": "attribution", "content": "a", "enabled": True})
    resp = await client.get("/api/admin/prompts")
    scenes = {p["scene"] for p in resp.json()["prompts"]}
    assert scenes == {"default", "attribution"}
```

- [ ] **Step 3: 运行验证失败**

Run: `pytest tests/test_prompt_store.py tests/test_routes_admin_prompts.py -v`
Expected: FAIL（`No module named 'src.core.prompt_store'`）

- [ ] **Step 4: 追加 `Prompt` 表到 `src/storage/models.py`**

在 `src/storage/models.py` 末尾追加：

```python
class Prompt(Base):
    """场景化系统提示词（orchestrator 按 scene 读，组装 system message）。
    场景如 default / attribution / correction；default 是兜底。
    admin 后台 CRUD，热更新（PromptStore 缓存刷新）。"""
    __tablename__ = "prompts"
    scene: Mapped[str] = mapped_column(String(32), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())
```

- [ ] **Step 5: 实现 `src/core/prompt_store.py`**

```python
"""场景化 prompt 存储 + 内存缓存（页面配置模型基础）。
orchestrator 组装 system message 时按 scene 读，默认场景 'default'。
ponytail: 单进程内存缓存；跨进程广播 P5 改 Redis pub/sub。"""
from __future__ import annotations

from src.logging import get_logger
from src.storage.models import Prompt
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)

DEFAULT_SCENE = "default"


class PromptStore:
    """场景化 prompt：内存缓存 + PG 持久。
    get 内存缓存优先，miss 读 PG 回填；upsert 写 PG + bump version + 刷新缓存。"""

    def __init__(self) -> None:
        # scene -> (content, version)
        self._cache: dict[str, tuple[str, int]] = {}

    async def get(self, scene: str = DEFAULT_SCENE) -> str | None:
        """读场景 prompt。未配置或 enabled=False 返回 None（orchestrator 走默认无 system）。"""
        cached = self._cache.get(scene)
        if cached is not None:
            return cached[0]
        async with AsyncSessionFactory() as s:
            row = await s.get(Prompt, scene)
            if row is None or not row.enabled:
                return None
            content, version = row.content, row.version
        self._cache[scene] = (content, version)
        return content

    async def upsert(self, scene: str, content: str,
                     enabled: bool = True) -> int:
        """新增或更新场景 prompt。返回新版本号。"""
        async with AsyncSessionFactory() as s:
            row = await s.get(Prompt, scene)
            if row:
                row.content = content
                row.enabled = enabled
                row.version += 1
                new_version = row.version
            else:
                s.add(Prompt(scene=scene, content=content,
                             enabled=enabled, version=1))
                new_version = 1
            await s.commit()
        self._cache[scene] = (content, new_version)
        log.info("prompt 更新 scene=%s version=%s enabled=%s",
                 scene, new_version, enabled)
        return new_version

    async def delete(self, scene: str) -> bool:
        """删除场景 prompt。不存在返回 False（幂等指示）。"""
        async with AsyncSessionFactory() as s:
            row = await s.get(Prompt, scene)
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
        self._cache.pop(scene, None)
        return True

    async def list_all(self) -> list[dict]:
        """列出全部场景 prompt（admin 列表用）。"""
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(Prompt.__table__.select())).all()
        return [{"scene": r.scene, "content": r.content,
                 "version": r.version, "enabled": r.enabled} for r in rows]

    async def refresh(self) -> None:
        """清缓存重新加载已缓存的 scene（admin 改完手动刷）。
        ponytail: P5 跨进程时改成 Redis pub/sub 广播失效。"""
        scenes = list(self._cache.keys())
        self._cache.clear()
        for sc in scenes:
            await self.get(sc)
```

- [ ] **Step 6: 实现 `src/web/routes/admin_prompts.py`**

```python
"""admin prompt 管理路由：CRUD /api/admin/prompts。
ponytail: 鉴权层 P5 管理后台再补；P0b 暴露路由供页面调试。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.core.prompt_store import PromptStore


class PromptPayload(BaseModel):
    scene: str
    content: str
    enabled: bool = True


def build_admin_prompts_router(store: PromptStore) -> APIRouter:
    """构造 admin prompts 路由。CRUD 均委托 PromptStore。"""
    router = APIRouter()

    @router.get("/api/admin/prompts")
    async def list_prompts() -> dict:
        return {"prompts": await store.list_all()}

    @router.get("/api/admin/prompts/{scene}")
    async def get_prompt(scene: str) -> dict:
        content = await store.get(scene)
        return {"scene": scene, "content": content}

    @router.post("/api/admin/prompts")
    async def create_prompt(payload: PromptPayload) -> dict:
        version = await store.upsert(payload.scene, payload.content, payload.enabled)
        return {"ok": True, "scene": payload.scene, "version": version}

    @router.put("/api/admin/prompts/{scene}")
    async def update_prompt(scene: str, payload: PromptPayload) -> dict:
        version = await store.upsert(scene, payload.content, payload.enabled)
        return {"ok": True, "scene": scene, "version": version}

    @router.delete("/api/admin/prompts/{scene}")
    async def delete_prompt(scene: str) -> dict:
        deleted = await store.delete(scene)
        return {"ok": True, "deleted": deleted}

    return router
```

- [ ] **Step 7: 运行验证通过**

Run: `pytest tests/test_prompt_store.py tests/test_routes_admin_prompts.py -v`
Expected: PASS（13 测试绿）

- [ ] **Step 8: 提交**

```bash
git add -A
git commit -m "feat(p0b): 场景化 prompt 管理 + admin API（prompt_store + admin_prompts）

Prompt 表 + PromptStore 内存缓存 + admin CRUD 路由。
orchestrator（Task 11）按 default 场景读 prompt 注入 system message。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 10: SSE 事件 + 双模式过滤（web/sse.py）

**Files:**
- Create: `src/web/sse.py`
- Test: `tests/test_sse.py`

**设计要点：** 纯函数双模式过滤（spec 6.8 表）。admin 全发；user 隐藏技术细节事件（含两类：spec 6.8 表中的 4 类查询执行细节 `metadata_lookup/sql_generated/knowledge_hit/attribution_step`，以及 AgentLoop 产出的 6 类内部技术事件 `turn_start/assistant/tool_call/tool_result/warning/cancelled`）。user 模式只透传用户友好事件（correction/clarification_needed/plan/todo_update/query_progress/intermediate/answer_delta/done/error）。SSEEvent 从 core.types import（统一类型，避免重复定义）。format_sse 用 `ensure_ascii=False` 防 Unicode 转义。

- [ ] **Step 1: 写失败测试 `tests/test_sse.py`**

```python
import json

from src.core.types import SSEEvent
from src.web.sse import (
    SSEEventType, ViewerMode,
    filter_event, format_sse, should_emit,
)


# ---- should_emit ----
def test_admin_emits_all_types():
    """admin 模式所有事件都发。"""
    for t in SSEEventType:
        ev = SSEEvent(type=t.value, data={}, trace_id="t")
        assert should_emit(ev, ViewerMode.ADMIN) is True


def test_user_hides_technical_details():
    """user 模式隐藏所有技术细节事件：
    - spec 6.8 表的 4 类查询执行细节
    - AgentLoop 产出的 6 类内部技术事件（turn_start/assistant/tool_call/tool_result/warning/cancelled）
    """
    hidden = ["metadata_lookup", "sql_generated", "knowledge_hit",
              "attribution_step",
              # AgentLoop 内部技术事件：user 不关心
              "turn_start", "assistant", "tool_call", "tool_result",
              "warning", "cancelled"]
    for t in hidden:
        ev = SSEEvent(type=t, data={}, trace_id="t")
        assert should_emit(ev, ViewerMode.USER) is False


def test_user_emits_friendly_events():
    """user 模式只透传用户友好事件。"""
    friendly = ["correction", "clarification_needed", "plan",
                "answer_delta", "done", "error"]
    for t in friendly:
        ev = SSEEvent(type=t, data={}, trace_id="t")
        assert should_emit(ev, ViewerMode.USER) is True


def test_user_emits_visible_types():
    """user 模式可见事件。"""
    visible = ["correction", "clarification_needed", "plan", "todo_update",
               "query_progress", "intermediate", "answer_delta", "done", "error"]
    for t in visible:
        ev = SSEEvent(type=t, data={}, trace_id="t")
        assert should_emit(ev, ViewerMode.USER) is True


# ---- filter_event ----
def test_filter_event_none_for_hidden():
    ev = SSEEvent(type="sql_generated", data={"sql": "select 1"}, trace_id="t")
    assert filter_event(ev, ViewerMode.USER) is None


def test_filter_event_passthrough_visible():
    ev = SSEEvent(type="answer_delta", data={"text": "hi"}, trace_id="t")
    out = filter_event(ev, ViewerMode.USER)
    assert out is ev  # 原样透传


def test_filter_event_admin_passthrough_hidden():
    """admin 模式连隐藏事件也透传。"""
    ev = SSEEvent(type="metadata_lookup", data={}, trace_id="t")
    out = filter_event(ev, ViewerMode.ADMIN)
    assert out is ev


# ---- format_sse ----
def test_format_sse_structure():
    ev = SSEEvent(type="answer_delta", data={"text": "你好"}, trace_id="abc123")
    out = format_sse(ev)
    assert out.startswith("event: answer_delta\n")
    assert out.endswith("\n\n")
    # data 行是合法 JSON
    data_line = out.split("\n")[1].removeprefix("data: ")
    payload = json.loads(data_line)
    assert payload["data"] == {"text": "你好"}
    assert payload["trace_id"] == "abc123"


def test_format_sse_unicode_not_escaped():
    """中文不被 \\uXXXX 转义（ensure_ascii=False）。"""
    ev = SSEEvent(type="answer_delta", data={"text": "你好世界"}, trace_id="t")
    out = format_sse(ev)
    assert "你好世界" in out
    assert "\\u" not in out


def test_format_sse_done():
    ev = SSEEvent(type="done", data={"answer": "结果"}, trace_id="t1")
    out = format_sse(ev)
    assert "event: done\n" in out
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_sse.py -v`
Expected: FAIL（`No module named 'src.web.sse'`）

- [ ] **Step 3: 实现 `src/web/sse.py`**

```python
"""SSE 事件类型常量 + 双模式过滤 + 文本格式化（spec 6.8）。
SSEEvent 复用 core.types 的定义（统一类型，避免重复）。
type 用 str 保持序列化简单；SSEEventType Enum 仅作常量参考。"""
from __future__ import annotations

import json
from enum import Enum

from src.core.types import SSEEvent  # 统一类型，不在 sse.py 重复定义


class SSEEventType(str, Enum):
    """SSE 事件类型常量（spec 6.8 表 + AgentLoop 内部技术事件）。
    运行时 SSEEvent.type 用 str。"""
    # 用户友好事件（user 模式可见）
    CORRECTION = "correction"
    CLARIFICATION_NEEDED = "clarification_needed"
    PLAN = "plan"
    TODO_UPDATE = "todo_update"
    INTERMEDIATE = "intermediate"
    ANSWER_DELTA = "answer_delta"
    DONE = "done"
    ERROR = "error"
    # 技术事件（admin 可见，user 隐藏）
    QUERY_PROGRESS = "query_progress"        # spec 6.8：查询进度（user 可见的"进度感"事件）
    METADATA_LOOKUP = "metadata_lookup"      # spec 6.8：元数据查询（4 类技术细节之一）
    SQL_GENERATED = "sql_generated"          # spec 6.8：SQL 生成（4 类技术细节之一）
    KNOWLEDGE_HIT = "knowledge_hit"          # spec 6.8：知识命中（4 类技术细节之一）
    ATTRIBUTION_STEP = "attribution_step"    # spec 6.8：归因步骤（4 类技术细节之一）
    # AgentLoop 内部产出的技术事件（admin 可见，user 隐藏）
    TURN_START = "turn_start"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    WARNING = "warning"
    CANCELLED = "cancelled"


class ViewerMode(str, Enum):
    ADMIN = "admin"
    USER = "user"


# user 模式隐藏的事件集：
# - spec 6.8 表的 4 类查询执行细节
# - AgentLoop 产出的 6 类内部技术事件（turn_start/assistant/tool_call/tool_result/warning/cancelled）
# ponytail: query_progress 不在内——它给 user 提供进度感（spec 6.8 标 user 可见）
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
    """admin 全发；user 隐藏技术细节事件（spec 6.8 + loop 内部事件）。"""
    if mode == ViewerMode.ADMIN:
        return True
    return event.type not in _USER_HIDDEN


def filter_event(event: SSEEvent, mode: ViewerMode) -> SSEEvent | None:
    """按模式过滤。hidden 返回 None；可见事件 P0b 原样透传。
    ponytail: intermediate 用户模式精简规则 P2 再做（spec 6.8 △）。"""
    if not should_emit(event, mode):
        return None
    return event


def format_sse(event: SSEEvent) -> str:
    """格式化为 SSE 文本协议：event: <type>\\ndata: <json>\\n\\n。
    ensure_ascii=False 防中文 Unicode 转义。"""
    payload = {"data": event.data, "trace_id": event.trace_id}
    return f"event: {event.type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_sse.py -v`
Expected: PASS（9 测试绿，含新增 test_user_emits_friendly_events）

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat(p0b): SSE 事件类型 + 双模式过滤（web/sse.py）

13 种事件枚举、用户模式隐藏 4 类技术细节、SSE 文本格式化（中文不转义）。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 11: Orchestrator 编排入口（core/orchestrator.py）

**Files:**
- Create: `src/core/orchestrator.py`
- Test: `tests/test_orchestrator.py`

**设计要点：** 编排入口：查会话状态判 is_resume → 非 resume 走 normalizer 前置（有修正则发 correction 事件）→ 读 PromptStore（可选，Task 9 集成）拿 system_prompt → 透传 loop 事件，异常转 ERROR 不中断流。双模式过滤在路由层做，orchestrator 总 yield 全量。trace_id 贯穿所有事件。`prompt_store` 为可选依赖（构造时不传则不注入 system message），保持 backward compatible。

- [ ] **Step 1: 写失败测试 `tests/test_orchestrator.py`**

```python
import pytest

from src.config import RedisConfig
from src.core.normalizer import Correction
from src.core.orchestrator import Orchestrator
from src.core.types import SSEEvent
from src.memory.session import SessionManager
from src.storage.pg_client import init_db
from src.storage.redis_client import RedisClient
from src.web.sse import ViewerMode


class FakeNormalizer:
    """fake normalizer：normalize 返回 (text, corrections)。"""
    def __init__(self, text=None, corrections=None):
        self._text = text
        self._corrections = corrections or []

    async def normalize(self, text):
        if self._text is not None:
            return self._text, self._corrections
        return text, []  # pass-through


class FakeLoop:
    """fake agent_loop.run：按预设事件序列回放。接受 system_prompt 参数（Task 9 prompts 集成）。"""
    def __init__(self, events):
        self._events = list(events)
        self.calls = []

    async def run(self, session_id, user_id, user_msg, trace_id,
                  cancel_token, is_resume=False, system_prompt=None, **kwargs):
        self.calls.append({"session_id": session_id, "user_msg": user_msg,
                           "trace_id": trace_id, "is_resume": is_resume,
                           "system_prompt": system_prompt})
        for e in self._events:
            yield e


class BoomLoop:
    """fake loop 抛异常，验证 orchestrator 转 ERROR。"""
    async def run(self, **kw):
        yield SSEEvent("turn_start", {}, "t1")
        raise RuntimeError("炸了")


class FakePromptStore:
    """fake PromptStore：get(scene) 返回预设 prompt 或 None。"""
    def __init__(self, prompts=None):
        self._prompts = prompts or {}
        self.get_calls = []

    async def get(self, scene="default"):
        self.get_calls.append(scene)
        return self._prompts.get(scene)


@pytest.fixture
async def session_mgr():
    await init_db("sqlite+aiosqlite:///:memory:")
    redis = RedisClient(RedisConfig(host="unreachable.invalid", port=1))
    await redis.connect()
    return SessionManager(redis)


async def _collect(gen):
    return [e async for e in gen]


@pytest.mark.asyncio
async def test_new_session_emits_correction(session_mgr):
    """新会话：normalizer 有修正则发 correction 事件，loop 收到标准化文本。"""
    sid = await session_mgr.create_session("u1", "web")
    norm = FakeNormalizer(text="新疆分公司", corrections=[
        Correction(raw="新疆省", standard="新疆", confidence=0.99, source="typo")])
    loop = FakeLoop([SSEEvent("answer_delta", {"text": "结果"}, "t1"),
                     SSEEvent("done", {"answer": "结果"}, "t1")])
    orch = Orchestrator(norm, loop, session_mgr)
    events = await _collect(orch.handle_message("u1", sid, "新疆省发电量",
                                                ViewerMode.USER, "t1"))
    # 首个事件是 correction
    assert events[0].type == "correction"
    assert events[0].data["original"] == "新疆省发电量"
    assert events[0].data["normalized"] == "新疆分公司"
    assert len(events[0].data["corrections"]) == 1
    # loop 收到标准化后的文本，is_resume=False
    assert loop.calls[0]["user_msg"] == "新疆分公司"
    assert loop.calls[0]["is_resume"] is False


@pytest.mark.asyncio
async def test_new_session_no_correction_when_clean(session_mgr):
    """无修正时不发 correction 事件。"""
    sid = await session_mgr.create_session("u1", "web")
    norm = FakeNormalizer()  # pass-through，无修正
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "t1")])
    orch = Orchestrator(norm, loop, session_mgr)
    events = await _collect(orch.handle_message("u1", sid, "你好",
                                                ViewerMode.USER, "t1"))
    assert all(e.type != "correction" for e in events)


@pytest.mark.asyncio
async def test_resume_skips_normalizer(session_mgr):
    """awaiting_clarification 会话：跳过 normalizer，user_msg 原样，is_resume=True。"""
    sid = await session_mgr.create_session("u1", "web")
    await session_mgr.set_status(sid, "awaiting_clarification")
    norm = FakeNormalizer(text="不应使用")  # 若误调会污染
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "t1")])
    orch = Orchestrator(norm, loop, session_mgr)
    events = await _collect(orch.handle_message("u1", sid, "6月",
                                                ViewerMode.USER, "t1"))
    assert all(e.type != "correction" for e in events)
    assert loop.calls[0]["user_msg"] == "6月"
    assert loop.calls[0]["is_resume"] is True


@pytest.mark.asyncio
async def test_passthrough_events_in_order(session_mgr):
    """loop 事件原序透传。"""
    sid = await session_mgr.create_session("u1", "web")
    norm = FakeNormalizer()
    expected = [SSEEvent("query_progress", {"p": 1}, "t1"),
                SSEEvent("answer_delta", {"text": "x"}, "t1"),
                SSEEvent("done", {"answer": "x"}, "t1")]
    loop = FakeLoop(expected)
    orch = Orchestrator(norm, loop, session_mgr)
    events = await _collect(orch.handle_message("u1", sid, "你好",
                                                ViewerMode.ADMIN, "t1"))
    assert [e.type for e in events] == ["query_progress", "answer_delta", "done"]


@pytest.mark.asyncio
async def test_loop_exception_becomes_error_event(session_mgr):
    """loop 抛异常 → orchestrator yield ERROR 事件，不向上抛中断流。"""
    sid = await session_mgr.create_session("u1", "web")
    norm = FakeNormalizer()
    orch = Orchestrator(norm, BoomLoop(), session_mgr)
    events = await _collect(orch.handle_message("u1", sid, "你好",
                                                ViewerMode.USER, "t1"))
    types = [e.type for e in events]
    assert "error" in types
    err = [e for e in events if e.type == "error"][0]
    assert "炸了" in err.data["message"]


@pytest.mark.asyncio
async def test_trace_id_propagated_to_correction(session_mgr):
    """correction 事件 trace_id 与入参一致。"""
    sid = await session_mgr.create_session("u1", "web")
    norm = FakeNormalizer(text="x", corrections=[
        Correction(raw="y", standard="x", confidence=0.9, source="typo")])
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "mytrace")])
    orch = Orchestrator(norm, loop, session_mgr)
    events = await _collect(orch.handle_message("u1", sid, "y",
                                                ViewerMode.USER, "mytrace"))
    corr = [e for e in events if e.type == "correction"]
    assert all(e.trace_id == "mytrace" for e in corr)


@pytest.mark.asyncio
async def test_nonexistent_session_treated_as_new(session_mgr):
    """不存在的会话：当新会话处理（get_session 返回 None → is_resume=False）。"""
    norm = FakeNormalizer()
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "t1")])
    orch = Orchestrator(norm, loop, session_mgr)
    events = await _collect(orch.handle_message("u1", "ghost-sid", "你好",
                                                ViewerMode.USER, "t1"))
    assert loop.calls[0]["is_resume"] is False
    assert events[-1].type == "done"


@pytest.mark.asyncio
async def test_prompt_store_injects_system_prompt(session_mgr):
    """传 prompt_store 时 orchestrator 读 default 场景并作为 system_prompt 透传给 loop（Task 9 集成）。"""
    norm = FakeNormalizer()
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "t1")])
    prompts = FakePromptStore(prompts={"default": "你是问数助手"})
    orch = Orchestrator(norm, loop, session_mgr, prompt_store=prompts)
    sid = await session_mgr.create_session("u1", "web")
    await _collect(orch.handle_message("u1", sid, "你好",
                                       ViewerMode.USER, "t1"))
    assert loop.calls[0]["system_prompt"] == "你是问数助手"
    assert prompts.get_calls == ["default"]


@pytest.mark.asyncio
async def test_no_prompt_store_passes_none(session_mgr):
    """不传 prompt_store 时 system_prompt=None（backward compatible）。"""
    norm = FakeNormalizer()
    loop = FakeLoop([SSEEvent("done", {"answer": "ok"}, "t1")])
    orch = Orchestrator(norm, loop, session_mgr)  # 无 prompt_store
    sid = await session_mgr.create_session("u1", "web")
    await _collect(orch.handle_message("u1", sid, "你好",
                                       ViewerMode.USER, "t1"))
    assert loop.calls[0]["system_prompt"] is None
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_orchestrator.py -v`
Expected: FAIL（`No module named 'src.core.orchestrator'`）

- [ ] **Step 3: 实现 `src/core/orchestrator.py`**

```python
"""编排入口：纠错前置 → 查状态分流 → 读 system prompt → 透传 loop 事件（spec 6.1/6.3/6.4）。
- 非 resume：normalizer 前置（有修正发 correction 事件）→ loop
- resume（awaiting_clarification）：跳过纠错，user_msg 原样进 loop（断点恢复，spec 6.4）
- system prompt：可选 PromptStore（Task 9 集成），读 default 场景透传 loop.run(system_prompt=...)
- 双模式过滤在路由层做，orchestrator 总 yield 全量事件
- loop 异常转 ERROR 事件，不中断流
orchestrator 只读会话状态（判 is_resume），状态转移由 loop 内部 SessionState 驱动。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict

from src.core.types import CancelToken, SSEEvent
from src.logging import get_logger
from src.memory.session import SessionManager
from src.web.sse import SSEEventType, ViewerMode

log = get_logger(__name__)


class Orchestrator:
    """编排入口。组合 normalizer + agent_loop + session_manager + prompt_store(可选)。
    - normalizer: async normalize(text) -> tuple[str, list[Correction]]
    - loop: async run(session_id, user_id, user_msg, trace_id, cancel_token, is_resume, system_prompt) -> AsyncIterator[SSEEvent]
    - sessions: P0a SessionManager（读状态判 is_resume）
    - prompt_store: 可选 PromptStore（Task 9），读 default 场景 prompt 注入 system message
    """

    def __init__(self, normalizer, loop, sessions: SessionManager,
                 prompt_store=None):
        self._normalizer = normalizer
        self._loop = loop
        self._sessions = sessions
        self._prompts = prompt_store  # ponytail: 可选，None 时 loop 无 system message

    async def handle_message(self, user_id: str, session_id: str, text: str,
                             mode: ViewerMode, trace_id: str
                             ) -> AsyncIterator[SSEEvent]:
        # 1. 查会话状态：awaiting_clarification => 断点恢复，跳过纠错（spec 6.4）
        sess = await self._sessions.get_session(session_id)
        is_resume = bool(sess and sess.get("status") == "awaiting_clarification")

        # 2. 名称纠错前置（仅新轮；恢复轮不重走纠错/意图识别，spec 6.4）
        if is_resume:
            user_msg = text
        else:
            user_msg, corrections = await self._normalizer.normalize(text)
            if corrections:
                yield SSEEvent(
                    SSEEventType.CORRECTION.value,
                    {"original": text, "normalized": user_msg,
                     "corrections": [asdict(c) for c in corrections]},
                    trace_id)

        # 3. 读 system prompt（Task 9 prompts 集成点；prompt_store 为空则 None）
        system_prompt = None
        if self._prompts is not None:
            system_prompt = await self._prompts.get("default")

        # 4. 迭代 loop，透传事件；异常转 ERROR 不中断流
        cancel_token = CancelToken()  # ponytail: 每次调用新建；P1 接 cancel API 时持久化映射
        try:
            async for evt in self._loop.run(
                session_id=session_id, user_id=user_id,
                user_msg=user_msg, trace_id=trace_id,
                cancel_token=cancel_token, is_resume=is_resume,
                system_prompt=system_prompt,
            ):
                yield evt
        except Exception as e:
            log.exception("loop 执行异常 trace=%s", trace_id)
            yield SSEEvent(SSEEventType.ERROR.value,
                           {"message": str(e)}, trace_id)
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_orchestrator.py -v`
Expected: PASS（9 测试绿，含 prompt_store 注入 + 无 prompt_store backward compatible 两个新测试）

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat(p0b): Orchestrator 编排入口（core/orchestrator.py）

纠错前置→查状态分流→透传 loop 事件、恢复轮跳过纠错、异常转 ERROR 不中断流。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 12: FastAPI 路由 ask SSE + 会话列表/删除（web/routes/*）

**Files:**
- Create: `src/web/routes/ask.py`, `src/web/routes/session.py`
- Modify: `src/memory/session.py`（追加 `list_sessions`）
- Test: `tests/test_routes_ask.py`, `tests/test_routes_session.py`

**设计要点：** ask 路由用原生 StreamingResponse（ponytail：不引 sse-starlette，format_sse 已是 SSE 文本）。双模式过滤在路由层统一做。会话列表直接查 PG（低频，不缓存）。删除幂等。

- [ ] **Step 1: 写失败测试 `tests/test_routes_ask.py`**

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.types import SSEEvent
from src.web.routes.ask import build_ask_router


class FakeOrchestrator:
    """fake orchestrator：按预设事件序列回放。"""
    def __init__(self, events):
        self._events = events

    async def handle_message(self, user_id, session_id, text, mode, trace_id):
        for e in self._events:
            yield e


@pytest.fixture
def client():
    events = [
        SSEEvent("correction",
                 {"original": "x", "normalized": "y", "corrections": []}, "t1"),
        SSEEvent("sql_generated", {"sql": "select 1"}, "t1"),
        SSEEvent("answer_delta", {"text": "结果"}, "t1"),
        SSEEvent("done", {"answer": "结果"}, "t1"),
    ]
    app = FastAPI()
    app.include_router(build_ask_router(FakeOrchestrator(events)))
    return TestClient(app)


def test_ask_sse_returns_event_stream(client):
    resp = client.post("/api/ask/sse", json={
        "user_id": "u1", "session_id": "s1", "text": "你好", "mode": "user"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]


def test_ask_sse_user_mode_hides_sql(client):
    resp = client.post("/api/ask/sse", json={
        "user_id": "u1", "session_id": "s1", "text": "你好", "mode": "user"})
    body = resp.text
    assert "event: correction" in body
    assert "event: answer_delta" in body
    assert "event: done" in body
    assert "sql_generated" not in body  # user 模式隐藏
    assert "t1" in body  # trace_id 出现


def test_ask_sse_admin_mode_shows_all(client):
    resp = client.post("/api/ask/sse", json={
        "user_id": "u1", "session_id": "s1", "text": "你好", "mode": "admin"})
    body = resp.text
    assert "sql_generated" in body  # admin 可见


def test_ask_sse_default_mode_is_user(client):
    resp = client.post("/api/ask/sse", json={
        "user_id": "u1", "session_id": "s1", "text": "你好"})
    body = resp.text
    assert "sql_generated" not in body  # 默认 user


def test_ask_sse_invalid_mode_returns_422(client):
    resp = client.post("/api/ask/sse", json={
        "user_id": "u1", "session_id": "s1", "text": "你好", "mode": "ghost"})
    assert resp.status_code == 422
```

- [ ] **Step 1b: 写失败测试 `tests/test_routes_session.py`**

```python
import pytest
import httpx
from fastapi import FastAPI

from src.config import RedisConfig
from src.memory.session import SessionManager
from src.storage.pg_client import init_db
from src.storage.redis_client import RedisClient
from src.web.routes.session import build_session_router


@pytest.fixture
async def setup_db():
    await init_db("sqlite+aiosqlite:///:memory:")
    redis = RedisClient(RedisConfig(host="unreachable.invalid", port=1))
    await redis.connect()
    return SessionManager(redis)


@pytest.mark.asyncio
async def test_list_sessions_by_user(setup_db):
    mgr = setup_db
    sid1 = await mgr.create_session("u1", "web")
    sid2 = await mgr.create_session("u1", "app")
    await mgr.create_session("u2", "web")  # 其他用户

    app = FastAPI()
    app.include_router(build_session_router(mgr))
    # ponytail: async 测试用 httpx.AsyncClient + ASGITransport，
    # 同步 TestClient 在 async 测试中会卡 event loop（Starlette 的 anyio 冲突）
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/session", params={"user_id": "u1"})
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 2
        assert {s["id"] for s in sessions} == {sid1, sid2}


@pytest.mark.asyncio
async def test_list_sessions_fields(setup_db):
    mgr = setup_db
    sid = await mgr.create_session("u1", "web")
    app = FastAPI()
    app.include_router(build_session_router(mgr))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/session", params={"user_id": "u1"})
        s = resp.json()["sessions"][0]
        assert set(s.keys()) == {"id", "channel", "status", "created_at"}
        assert s["channel"] == "web"
        assert s["status"] == "idle"


@pytest.mark.asyncio
async def test_delete_session(setup_db):
    mgr = setup_db
    sid = await mgr.create_session("u1", "web")
    app = FastAPI()
    app.include_router(build_session_router(mgr))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/api/session/{sid}")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        # 删除后列表为空
        resp = await client.get("/api/session", params={"user_id": "u1"})
        assert len(resp.json()["sessions"]) == 0


@pytest.mark.asyncio
async def test_delete_session_idempotent(setup_db):
    """删除不存在的 sid 不报错（幂等）。"""
    mgr = setup_db
    app = FastAPI()
    app.include_router(build_session_router(mgr))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/api/session/ghost-sid")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_routes_ask.py tests/test_routes_session.py -v`
Expected: FAIL（`No module named 'src.web.routes.ask'`）

- [ ] **Step 3: 实现 `src/web/routes/ask.py`**

```python
"""POST /api/ask/sse 流式路由（spec 6.8 双模式过滤在路由层做）。
ponytail: 不引 sse-starlette，原生 StreamingResponse + format_sse 已是 SSE 文本。"""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.web.sse import ViewerMode, filter_event, format_sse


class AskRequest(BaseModel):
    user_id: str
    session_id: str
    text: str
    # ponytail: 字段类型直接用枚举，pydantic 收到非法字符串自动 422，
    # 不在路由内手写 ViewerMode(req.mode)（那样会抛 ValueError → 500）
    mode: ViewerMode = ViewerMode.USER


def build_ask_router(orchestrator) -> APIRouter:
    """构造 ask 路由。orchestrator: async handle_message(...) -> AsyncIterator[SSEEvent]。"""
    router = APIRouter()

    @router.post("/api/ask/sse")
    async def ask_sse(req: AskRequest):
        mode = req.mode  # 已是 ViewerMode 枚举，非法值在 pydantic 解析阶段已 422
        trace_id = uuid4().hex

        async def stream():
            # 双模式过滤在路由层统一做（spec 6.8）
            async for evt in orchestrator.handle_message(
                req.user_id, req.session_id, req.text, mode, trace_id,
            ):
                filtered = filter_event(evt, mode)
                if filtered is not None:
                    yield format_sse(filtered)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return router
```

- [ ] **Step 4: 实现 `src/web/routes/session.py`**

```python
"""会话列表/删除路由。薄层：只调一层 SessionManager。
ponytail: 用户隔离仅靠 user_id 查询参数，鉴权层 P5 管理后台再补。"""
from __future__ import annotations

from fastapi import APIRouter

from src.memory.session import SessionManager


def build_session_router(session_mgr: SessionManager) -> APIRouter:
    router = APIRouter()

    @router.get("/api/session")
    async def list_sessions(user_id: str):
        return {"sessions": await session_mgr.list_sessions(user_id)}

    @router.delete("/api/session/{sid}")
    async def delete_session(sid: str):
        await session_mgr.delete_session(sid)  # 幂等：不存在不报错
        return {"ok": True}

    return router
```

- [ ] **Step 5: 修改 `src/memory/session.py` 追加 `list_sessions`**

在 `SessionManager` 类末尾（`delete_session` 方法后）追加：

```python
    async def list_sessions(self, user_id: str) -> list[dict]:
        """查某用户全部会话，按创建时间倒序。
        ponytail: 列表低频，直接查 PG 不走 Redis 缓存；超 1000 会话再加 分页。"""
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(
                SessionRow.__table__.select()
                .where(SessionRow.user_id == user_id)
                .order_by(SessionRow.created_at.desc())
            )).all()
        return [{"id": r.id, "channel": r.channel, "status": r.status,
                 "created_at": r.created_at.isoformat() if r.created_at else None}
                for r in rows]
```

- [ ] **Step 6: 运行验证通过**

Run: `pytest tests/test_routes_ask.py tests/test_routes_session.py -v`
Expected: PASS（9 测试绿）

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "feat(p0b): FastAPI 路由 ask SSE + 会话列表/删除（web/routes）

POST /api/ask/sse 流式双模式过滤、GET/DELETE /api/session、
SessionManager.list_sessions。原生 StreamingResponse 不引 sse-starlette。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 13: Qwen3 自主 ReAct spike + 统计单测（tests/spike_qwen_react.py）

**Files:**
- Create: `tests/spike_qwen_react.py`（手动脚本，非单测）, `tests/test_spike_stats.py`

**设计要点：** spike 是 spec 13 P0 末尾关键里程碑，手动跑验证三大能力：(1) 自主循环收敛 (2) ask_user 准确 (3) 错误自愈。`classify` 抽成纯函数单测。脚本不进 pytest 收集（`spike_` 前缀非 `test_`）。前置：config/application.yml llm 网关可达 + OPENAI_API_KEY 已设。

- [ ] **Step 1: 写失败测试 `tests/test_spike_stats.py`（统计逻辑单测）**

```python
"""spike 统计逻辑单测（不连真网关，只测 classify 纯函数）。"""
from tests.spike_qwen_react import classify


def test_classify_converged():
    """收到 done = 收敛。"""
    evs = [{"type": "intermediate"}, {"type": "done", "data": {"answer": "x"}}]
    assert classify(evs) == (True, False, False)


def test_classify_ask_user_then_done():
    """clarification_needed 后 resume 收敛 = asked。"""
    evs = [{"type": "clarification_needed"},
           {"type": "done", "data": {"answer": "x"}}]
    assert classify(evs) == (True, True, False)


def test_classify_heal_after_error():
    """error 后仍 done = 自愈。"""
    evs = [{"type": "error", "data": {"stage": "execute_sql"}},
           {"type": "intermediate"},
           {"type": "done", "data": {"answer": "x"}}]
    assert classify(evs) == (True, False, True)


def test_classify_not_converged():
    """max_turns 耗尽无 done = 未收敛。"""
    evs = [{"type": "intermediate"}, {"type": "warning"}]
    assert classify(evs) == (False, False, False)


def test_classify_empty_events():
    assert classify([]) == (False, False, False)


def test_classify_ask_without_done_not_converged():
    """clarification_needed 但无 resume/done = 未收敛（挂起未补答）。"""
    evs = [{"type": "clarification_needed"}]
    assert classify(evs) == (False, True, False)
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_spike_stats.py -v`
Expected: FAIL（`No module named 'tests.spike_qwen_react'`）

- [ ] **Step 3: 实现 `tests/spike_qwen_react.py`**

```python
"""Qwen3 自主 ReAct 稳定性 spike（spec 13 P0 末尾关键里程碑）。

手动跑：python -m tests.spike_qwen_react
验证三大能力：
  (1) 自主循环收敛——闲聊/取数 case 在 max_turns 内收到 done
  (2) ask_user 准确——缺参 case 触发 clarification_needed，注入回答后 resume 收敛
  (3) 错误自愈——execute_sql stub 首次返回坏表名错误，LLM 重试正确表后 finish

前置：config/application.yml llm 网关可达 + OPENAI_API_KEY 已设。
脚本不进 pytest 收集（spike_ 前缀非 test_）；test_spike_stats.py 只测 classify 纯函数。
spike 对 agent_loop 用 duck-typed 最小接口（run），接口未冻结时只改适配层。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SpikeCase:
    id: str
    text: str
    expect_finish: bool = True
    expect_ask_user: bool = False
    expect_heal: bool = False
    clarification_answer: str | None = None
    max_turns: int = 10


@dataclass
class CaseResult:
    case_id: str
    converged: bool
    asked: bool
    healed: bool
    turns: int
    final_text: str
    corrections: list[dict] = field(default_factory=list)
    error: str | None = None


def classify(events: list[dict]) -> tuple[bool, bool, bool]:
    """从事件流判定 (converged, asked, healed)。纯函数，无 IO，可单测。
    - converged: 收到 done
    - asked: 收到 clarification_needed
    - healed: error 后仍 done（出错后仍收敛 = 自愈）
    """
    types = [e.get("type") for e in events]
    converged = "done" in types
    asked = "clarification_needed" in types
    healed = "error" in types and "done" in types
    return converged, asked, healed


CASES: list[SpikeCase] = [
    SpikeCase("chat-1", "你好"),
    SpikeCase("chat-2", "你是谁？能做什么？"),
    SpikeCase("query-1", "查新疆分公司2026年6月发电量"),
    SpikeCase("query-2", "展示各分公司上月发电量对比"),
    SpikeCase("ask-1", "查发电量", expect_ask_user=True,
              clarification_answer="新疆分公司"),
    SpikeCase("ask-2", "对比发电量", expect_ask_user=True,
              clarification_answer="6月和5月对比"),
    SpikeCase("typo-1", "新疆省分公司发电量"),   # normalizer pass-through 不改
    SpikeCase("typo-2", "内蒙分公司风电量"),
    SpikeCase("heal-1", "查新疆分公司发电量", expect_heal=True),  # stub 首次坏表名
    SpikeCase("multi-1", "哪些分公司发电量最高？给出前3名"),
    SpikeCase("multi-2", "新疆分公司6月比5月多了多少？"),
    SpikeCase("chitchat-1", "今天天气怎么样？"),
]


def build_stub_registry():
    """构造 stub 工具注册表（query_metadata/execute_sql + P0b builtins）。
    execute_sql 在 heal-1 case 首次返回坏表名错误，模拟需自愈的场景。"""
    from src.core.types import CancelToken, ToolDefinition, ToolResult
    from src.tools.builtins import default_registry

    reg = default_registry()

    # stub query_metadata：返回固定表元数据
    async def _query_metadata(args, ctx, tk):
        return ToolResult(summary="表 power_output(分公司,月份,发电量MWh)；"
                                  "表 dim_branch(分公司id,分公司名,区域)")

    # stub execute_sql：heal-1 case 首次返回坏表名错误触发自愈
    _first_call = {"heal-1": True}

    async def _execute_sql(args, ctx, tk):
        if ctx.session_id == "spike-heal-1" and _first_call["heal-1"]:
            _first_call["heal-1"] = False
            return ToolResult(summary="错误：表 'power' 不存在。可用表：power_output, dim_branch")
        return ToolResult(summary="查询成功，返回 5 行（含新疆/华北/华东等分公司）",
                          result_id="r-spike")

    reg.register(ToolDefinition(
        name="query_metadata", description="查表/字段元数据，选表用",
        parameters={"type": "object",
                    "properties": {"keyword": {"type": "string"}},
                    "required": ["keyword"]},
        handler=_query_metadata))
    reg.register(ToolDefinition(
        name="execute_sql", description="执行 SQL 查询并返回摘要",
        parameters={"type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"]},
        handler=_execute_sql))
    return reg


async def run_one(case: SpikeCase, loop, normalizer, session_id: str) -> CaseResult:
    """跑单个 case，收集事件流，判定 converged/asked/healed。"""
    from src.core.types import CancelToken

    text, corrections = await normalizer.normalize(case.text)
    events: list[dict] = []
    turns = 0
    final_text = ""
    try:
        async for ev in loop.run(session_id, "spike-user", text,
                                 f"trace-{case.id}", CancelToken(), is_resume=False):
            events.append({"type": ev.type, "data": ev.data})
            if ev.type == "turn_start":
                turns = ev.data.get("turn", turns)
            if ev.type == "done":
                final_text = ev.data.get("answer", "")
            # ask_user 挂起后注入用户回答恢复
            if (ev.type == "clarification_needed" and case.clarification_answer):
                async for ev2 in loop.run(session_id, "spike-user",
                                          case.clarification_answer,
                                          f"trace-{case.id}", CancelToken(),
                                          is_resume=True):
                    events.append({"type": ev2.type, "data": ev2.data})
                    if ev2.type == "done":
                        final_text = ev2.data.get("answer", "")
    except Exception as e:
        return CaseResult(case.id, False, False, False, turns, final_text,
                          [c.__dict__ for c in corrections], str(e))
    converged, asked, healed = classify(events)
    return CaseResult(case.id, converged, asked, healed, turns, final_text,
                      [c.__dict__ for c in corrections])


def print_report(results: list[CaseResult]) -> None:
    """输出控制台表格 + 写 markdown 报告 + 落 jsonl 原始流。"""
    total = len(results)
    conv = sum(1 for r in results if r.converged)
    asked = sum(1 for r in results if r.asked)
    healed = sum(1 for r in results if r.healed)
    err_cases = [r for r in results if r.error]

    print(f"\n{'=' * 64}")
    print(f"Qwen3 自主 ReAct spike 报告（共 {total} case，基于 stub 工具）")
    print(f"{'=' * 64}")
    print(f"{'case':<12} {'收敛':>5} {'ask':>5} {'自愈':>5} {'轮数':>5} {'最终答案':<24}")
    for r in results:
        mark_c = "是" if r.converged else "否"
        mark_a = "是" if r.asked else "-"
        mark_h = "是" if r.healed else "-"
        print(f"{r.case_id:<12} {mark_c:>5} {mark_a:>5} {mark_h:>5} "
              f"{r.turns:>5} {r.final_text[:24]:<24}")
    print(f"\n收敛率: {conv}/{total}  ask_user 触发: {asked}  自愈: {healed}")
    if err_cases:
        print(f"异常 case: {[r.case_id for r in err_cases]}")

    # 写 markdown 报告
    out_dir = Path(__file__).parent / "spike_output"
    out_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    lines = [f"# Qwen3 spike 报告 {ts}", "",
             f"收敛 {conv}/{total}，ask_user {asked}，自愈 {healed}", ""]
    for r in results:
        lines.append(f"- **{r.case_id}**: conv={r.converged} asked={r.asked} "
                     f"healed={r.healed} turns={r.turns} "
                     f"final=`{r.final_text[:40]}` err={r.error}")
    (out_dir / f"report-{ts}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写：{out_dir / f'report-{ts}.md'}")


async def main():
    """spike 主入口：连真 Qwen3 网关跑全部 case。"""
    try:
        from src.config import load_config
        from src.core.agent_loop import AgentLoop
        from src.core.normalizer import Normalizer
        from src.core.session import SessionState
        from src.llm.service import LLMService
        from src.memory.session import SessionManager
        from src.storage.pg_client import init_db
        from src.storage.redis_client import RedisClient
    except ImportError as e:
        print(f"[spike] 依赖未就绪：{e}。请先完成 P0b 其他子系统。")
        return

    cfg = load_config("config")
    await init_db("sqlite+aiosqlite:///:memory:")
    redis = RedisClient(cfg.redis)
    await redis.connect()
    mgr = SessionManager(redis)
    state = SessionState(mgr)
    llm = LLMService(cfg.llm)
    registry = build_stub_registry()
    loop = AgentLoop(llm, registry, state, max_turns=10)
    normalizer = Normalizer()  # P0b pass-through

    results = []
    # ponytail: spike 需要固定可预测的 session_id（execute_sql stub 按 sid 识别
    # heal-1 case），故绕过 create_session 的 uuid，直接 ORM 插入指定 id 的 Session 行。
    # 这保证 sid 入库，后续 transition(RUNNING/DONE/...) 不会因"会话不存在"抛错。
    from datetime import datetime, timedelta, timezone
    from src.storage.models import Session as SessionRow
    from src.storage.pg_client import AsyncSessionFactory

    for c in CASES:
        sid = f"spike-{c.id}"
        # 直接 ORM 插入指定 id 的 Session（绕过 create_session 的 uuid）
        async with AsyncSessionFactory() as s:
            existing = await s.get(SessionRow, sid)
            if existing is None:
                s.add(SessionRow(
                    id=sid, user_id="spike-user",
                    channel="web", status="idle",
                    ttl_at=datetime.now(timezone.utc) + timedelta(hours=1)))
                await s.commit()
        try:
            result = await asyncio.wait_for(
                run_one(c, loop, normalizer, sid), timeout=120)
        except asyncio.TimeoutError:
            result = CaseResult(c.id, False, False, False, 0, "", [],
                                "timeout 120s")
        results.append(result)
    print_report(results)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: 运行统计单测验证通过**

Run: `pytest tests/test_spike_stats.py -v`
Expected: PASS（6 测试绿）

- [ ] **Step 5: 手动跑 spike（可选，需真网关）**

Run: `python -m tests.spike_qwen_react`
Expected: 输出 12 case 的收敛/ask_user/自愈统计表 + 写 markdown 报告到 `tests/spike_output/`。若网关不可达则报错退出（不阻塞单测）。

- [ ] **Step 6: 全量测试验证**

Run: `pytest -v`
Expected: PASS（P0a 19 + P0b 全部新增测试，约 130+ 测试绿，含 config_store/llm_config/prompts/admin routes 新增测试）

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "feat(p0b): Qwen3 spike 手动验证脚本 + 统计单测

spike 覆盖自主循环收敛/ask_user/错误自愈三大里程碑，classify 抽纯函数单测。
脚本手动跑（需真网关），不进 pytest 收集。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

### spec P0b 范围覆盖核对

| spec 要求 | 覆盖任务 | 状态 |
|---|---|---|
| ToolRegistry 动态 Schema 重建（spec 6.2.2） | Task 3 `openai_tools()` 每次现算 | ✅ |
| coerce_tool_args 类型强转（spec 6.2.3） | Task 3 `coerce_tool_args` | ✅ |
| 运行时可用性检查/自动隐藏（spec 6.2.1） | Task 3 `availability` + `require_module` | ✅ |
| 动态配置基础设施（页面配置模型基础） | Task 2 `ConfigStore` KV + 内存缓存 + 版本号 | ✅ |
| 动态 LLM 配置 + admin API | Task 5 `LlmConfig` 表 + `LLMService` 读动态 + admin 路由 | ✅ |
| 场景化 prompt 管理 + orchestrator 集成 | Task 9 `Prompt` 表 + `PromptStore` + admin 路由 + Task 11 orchestrator 读 | ✅ |
| ask_user 挂起恢复跨消息（spec 6.4） | Task 6 `SessionState.suspend/resume` + Task 7 loop 观察 suspended | ✅ |
| 结果旁路 result_id（spec 6.5） | Task 7 只回灌 summary、result_id 只进事件 | ✅ |
| 双模式 SSE（spec 6.8） | Task 10 `should_emit/filter_event` + Task 12 路由层过滤 | ✅ |
| 取消令牌贯穿 loop + 工具（spec 6.6） | Task 1 `CancelToken` + Task 7 三处检查点 + 透传 registry.execute | ✅ |
| 护栏 max_turns/重复/ask_user 上限（spec 6.1） | Task 7 三件套 | ✅ |
| 名称纠错前置（spec 6.3，P0b pass-through） | Task 8 `Normalizer` | ✅ |
| Qwen3 spike（spec 13 P0 末尾） | Task 13 `spike_qwen_react.py` | ✅ |
| 会话状态机（spec 6.4） | Task 6 `SessionState` + 转换表 | ✅ |
| 编排入口（纠错→分流→loop） | Task 11 `Orchestrator` | ✅ |

### 占位符扫描

- **无 TBD/TODO 占位代码**。所有任务代码完整可运行。
- 唯一 TODO 在 normalizer.py 注释里（`# TODO(P2): fuzzy_fn/llm_fn`），是标注 P2 待实现的真实三层纠错，非占位实现——P0b pass-through 分支完整可用。
- `_maybe_compress` 是 no-op 钩子（spec 6.7 P1 实现），有 `ponytail:` 注释说明升级路径，非占位。
- 新增 3 任务（Task 2 config_store / Task 5 llm_config / Task 9 prompts）全部 TDD 完整代码，无 TBD：ConfigStore/PromptStore/LLMService 改造/admin 路由均可直接运行。
- 动态配置跨进程广播为 P5 升级路径（ConfigStore.refresh / PromptStore.refresh 注释已标 `ponytail: P5 改 Redis pub/sub`），非占位而是显式延后。
- LLMService 协议固定 OpenAI 兼容（base_url 可换），明确不做 Anthropic/Gemini 原生协议适配——这是范围决策不是占位。

### 类型一致性核对

所有跨子系统接口已按"接口统一决策"表对齐：

1. **共享类型集中在 `src/core/types.py`**：CancelToken / LoopContext / ToolResult / SSEEvent / ToolDefinition / ToolHandler。tools/core/web 三个包都从此 import，无重复定义、无循环依赖（types 零内部依赖，是依赖图最底层）。

2. **依赖方向无环**（含新增 config_store / llm_config / prompts）：
   - `core.types` ←（被依赖）`tools.registry` ← `tools.builtins`
   - `core.types` ← `core.session` ← `core.agent_loop` → `tools.registry` + `llm.service`
   - `core.types` ← `core.normalizer`
   - `core.types` ← `core.prompt_store`（仅依赖 storage，不依赖 types 实际符号，但同属 core 包）
   - `core.types` ← `web.sse` ← `core.orchestrator` → `core.agent_loop` + `core.normalizer` + `core.prompt_store`（可选）
   - `config_store.store` → `storage.models` / `storage.pg_client`（独立包，零 core 依赖）
   - `llm.service` → `storage.models` / `storage.pg_client`（动态配置查询）+ `config.LLMConfig`（fallback）
   - `web.routes` → `web.sse` + `core.orchestrator` + `core.prompt_store` + `storage.models`（admin 路由直接 ORM 操作）
   - 无任何反向 import。

3. **handler 三参签名统一**：`(args: dict, ctx: LoopContext, cancel_token: CancelToken) -> ToolResult`。所有工具（echo/finish/ask_user + spike stub + P1 execute_sql）遵守此契约。

4. **ToolRegistry 三方法统一**：`available_defs() -> list[ToolDefinition]` / `openai_tools() -> list[dict]` / `execute(name, args, ctx, cancel_token) -> ToolResult`。agent_loop 调 `openai_tools()` 喂 LLM、`execute()` 跑工具。

5. **SSEEvent 单一来源**：`core/types.py` 定义，`web/sse.py` 直接 import（不在 sse.py 重复定义）。agent_loop 和 orchestrator 都产 SSEEvent，路由层消费。`SSEEventType` 枚举覆盖所有 loop 实际产出事件类型（`turn_start/assistant/tool_call/tool_result/warning/cancelled/done/error/clarification_needed` 等）。

6. **AgentLoop.run 签名统一**：`run(session_id, user_id, user_msg, trace_id, cancel_token, is_resume=False, system_prompt=None) -> AsyncIterator[SSEEvent]`。loop 内部组装 messages（resume 从 SessionState.resume 加载；新会话 `[system?, user]`），orchestrator 读 PromptStore 后透传 `system_prompt`。`system_prompt=None` 时与原行为一致（backward compatible）。

7. **动态配置三件套接口一致**：
   - `ConfigStore.get(key, default=None) -> Any | None` / `set(key, value) -> int（新版本号）` / `refresh() -> None`
   - `PromptStore.get(scene='default') -> str | None` / `upsert(scene, content, enabled=True) -> int` / `delete(scene) -> bool` / `list_all() -> list[dict]` / `refresh() -> None`
   - `LLMService._resolve_config() -> LLMConfig`（动态优先 fallback yml）/ `reset_dynamic() -> None`（admin PUT 后清缓存重建 client）
   - 三者均遵循"内存缓存 + PG 持久 + 版本号 + refresh 接口"模式，P0b 单进程，P5 跨进程改 Redis pub/sub。

8. **admin 路由统一用 `build_xxx_router(...)` 工厂函数**：`build_ask_router(orchestrator)` / `build_session_router(mgr)` / `build_admin_llm_router(llm_service=None)` / `build_admin_prompts_router(store)`，路由组装在应用启动时由 `app.include_router()` 注入依赖，便于测试替换。

### 命名统一决策汇总（综合 5 子系统时的冲突解决）

| 冲突 | 原方案分歧 | 最终决策 | 理由 |
|---|---|---|---|
| 上下文命名 | tool_registry 用 `ToolContext`，agent_loop 用 `LoopContext` | **LoopContext**（含 channel） | loop 是编排核心，工具经 loop 间接调用；含 channel 更完整 |
| handler 签名 | tool_registry 两参，agent_loop 三参 | **三参 (args, ctx, cancel_token)** | spec 6.6 要求取消贯穿工具；两参无法在工具内 check |
| ToolResult 字段 | tool_registry 含 finished/suspended，agent_loop 不含 | **含 finished/suspended** | 工具标志位驱动 loop 终止/挂起，是工具与 loop 的契约 |
| 事件类型 | agent_loop `LoopEvent`，sse `SSEEvent` | **统一 SSEEvent**，废弃 LoopEvent | 避免两套事件类型转换；type 用 str 不用 Enum（序列化简单） |
| AgentLoop.run 入参 | agent_loop 收预组装 messages，sse 收 user_msg+is_resume | **收 user_msg+is_resume，loop 内部组装 messages** | 简化 orchestrator；ContextAssembler P1 再在 loop 外接 |
| ask_user 挂起机制 | tool_registry 标志位、agent_loop 硬编码、session_state 异常 | **工具返回 suspended=True，loop 观察后委托 SessionState** | 工具零依赖（tool_registry 意图）；不用异常做控制流（ponytail 反模式） |
| checkpoint 操作 | agent_loop 模块级函数，session_state 方法 | **收敛到 SessionState** | 单一职责；避免两套 checkpoint 读写竞态 |
| ToolRegistryProtocol | agent_loop 定义 Protocol | **不要 Protocol** | 单实现接口是过度抽象（ponytail）；agent_loop 直接 import ToolRegistry，无循环 |
| Normalizer 返回 | normalizer 返 tuple，orchestrator 期望对象属性 | **统一 tuple，orchestrator 解包** | tuple 更简洁；orchestrator 实现 `asdict(c)` 序列化 corrections |
| 状态机 RUNNING→IDLE | spec 状态机无此转换 | **新增合法转换** | 取消场景 cancelled→IDLE 需要此路径；状态机表驱动易扩展 |

### 已知技术风险（spike 需验证）

1. **langchain tool_calls 格式**：assistant 消息的 tool_calls 已转 OpenAI 格式（`function/arguments` 字符串），spike 需验证 Qwen 网关下一轮能正确识别。`_to_openai_tool_calls` 已处理。
2. **Qwen args 字符串**：`_normalize_args` 兜底 str→dict，spike 验证 Qwen 是否真的返回字符串 args。
3. **messages 全 dict 兼容性**：langchain ChatOpenAI 接收 dict 列表自动转 Message，spike 验证 role=tool 消息兼容性。
4. **取消的非瞬时性**：P0b 靠客户端断连 GeneratorExit + loop 检查点，不提供显式 cancel API（P1 补）。

### 文件清单

P0b 新建 17 个源文件 + 15 个测试文件，修改 4 个（requirements.txt、memory/session.py、storage/models.py、llm/service.py）：
- 源（新建）：
  - `src/core/{__init__,types,session,agent_loop,normalizer,prompt_store,orchestrator}.py`（7 个）
  - `src/config_store/{__init__,store}.py`（2 个）
  - `src/tools/{__init__,registry,builtins}.py`（3 个）
  - `src/web/{__init__,sse}.py` + `src/web/routes/{__init__,ask,session,admin_llm,admin_prompts}.py`（7 个，含 routes 的 __init__）
- 源（修改）：`src/storage/models.py`（追加 AppConfigRow / LlmConfigRow / Prompt 三表）、`src/llm/service.py`（LLMService 改造动态配置）、`src/memory/session.py`（追加 list_sessions）、`requirements.txt`（补 httpx>=0.27）
- 测试（新建）：`tests/test_{cancel_token,config_store,tool_registry,llm_config,routes_admin_llm,session_state,agent_loop,normalizer,prompt_store,routes_admin_prompts,sse,orchestrator,routes_ask,routes_session,spike_stats}.py`（15 个）+ `tests/spike_qwen_react.py`（手动脚本）

### 任务依赖顺序汇总

```
Task 1  types          （无依赖，依赖图根）
Task 2  config_store   （依赖 Task 1 包结构）
Task 3  registry       （依赖 Task 1 types）
Task 4  builtins       （依赖 Task 1 types + Task 3 registry）
Task 5  llm_config     （依赖 Task 2 ConfigStore 模式；独立结构化 LlmConfigRow 表）
Task 6  session        （依赖 P0a SessionManager + storage.models）
Task 7  agent_loop     （依赖 Task 1 types + Task 3 registry + Task 6 session + llm.service）
Task 8  normalizer     （依赖 Task 1 types；纯函数级零持久化）
Task 9  prompts        （依赖 Task 2 ConfigStore 模式；独立结构化 Prompt 表）
Task 10 sse            （依赖 Task 1 types；纯函数级）
Task 11 orchestrator   （依赖 Task 1 types + Task 8 normalizer + Task 10 sse + Task 9 prompt_store(可选) + agent_loop duck-typed）
Task 12 routes         （依赖 Task 10 sse + Task 11 orchestrator + P0a SessionManager）
Task 13 spike          （依赖全部子系统，手动脚本）
```

无依赖环；Task 2/5/9 三件动态配置基础设施分散在依赖链不同位置，被 agent_loop/orchestrator 通过可选依赖方式集成（backward compatible）。
