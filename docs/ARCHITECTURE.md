# NL2SQL 技术架构文档

> 内网单租户问数工具。本文是「系统是什么 + 为什么这么设计」的可迭代单一事实源，按当前代码（`test` 分支）核对。
> 「怎么一步步落地」见 `PLAN.md`。代码是事实源，本文与代码不一致时以代码为准。

---

## 0. 一句话总览

用户用自然语言问业务数据 → **Agent 自主 ReAct 循环**（查元数据 → 生成只读 SQL → 在**业务库**执行 → 结果摘要回灌 LLM）→ 流式回答。
全程 SSE 推送（思考 / SQL / 表格）。Web 与飞书两条入口共用同一个编排内核。

技术栈：FastAPI + SQLAlchemy 2.0(async) + asyncpg + Redis（降级进程内 dict）+ openai/anthropic SDK（直连，去 langchain）+ sqlglot + RAGFlow（外部知识库）。

---

## 一、双库分离（最重要的心智模型）

改任何涉及「数据/数据库」的代码前，先分清写的是哪个库：

| 库 | 存什么 | 怎么连 |
|----|--------|--------|
| **平台库**（PostgreSQL） | 会话/消息/审计/元数据/配置表/提示词/SQL 模板 | ORM，全局 `AsyncSessionFactory`（`storage/pg_client.py`） |
| **业务库**（MySQL/StarRocks/PG） | 用户真正要查的业务数据 | `execute_sql` 按 `datasource_id` **动态建连、查完即弃**，**绝不经 ORM**（`datasource/manager.py` 缓存 engine） |
| **RAGFlow**（外部） | 文档/向量（解析、分段、embedding 全归它） | `knowledge_search` 工具转发 `/retrieval`（`ragflow/client.py`） |
| **Redis** | 会话热态 / 查询结果旁路(TTL) / 飞书会话绑定 | 连不上自动降级进程内 dict（`storage/redis_client.py`） |

**铁律**：元数据/审计/配置写平台库；问数结果从业务库捞。混淆两者是最常见的错。

---

## 二、三层抽象（内核 / 工具 / 技能）

```
🧠 KERNEL 内核   角色 prompt(纯协议) + 当前时间 + ReAct 循环    core/agent_loop.py   零业务
🔧 TOOL  工具    原子能力：JSON schema(教参数) + handler(真跑)   tools/*.py           一个工具一件事
📖 SKILL  技能   纯文本方法论：教 loop 何时/怎么组合工具         prompts 表一个 scene 只进 prompt
```

**两条铁律**：
1. 新能力 = 先造 tool handler（决定「能不能」），再造 skill 教用（决定「何时用」）。纯 skill 长不出执行能力。
2. 能力不重造：一个能力只挂一个工具，复用而非重写。（归因 = 方法论 skill + 复用 `execute_sql`/`knowledge_search`，不是另起胖工具。）

---

## 三、目录结构

```
src/
  main.py            FastAPI 入口：lifespan 装配单例 + HMAC 单账号 auth
  config.py          YAML→dataclass（只读基础设施，不读 LLM）
  logging.py         统一日志（nl2sql.* 命名空间，stdout）
  core/              编排内核
    orchestrator.py    入口编排：会话忙闸门 + 日期注入 + 透传 loop 事件
    agent_loop.py      ReAct 循环 + 护栏 + 上下文压缩 + 审计挂载
    session.py         会话状态机 + ask_user 挂起/恢复
    audit.py           逐事件落 audit_events（按 trace_id 隔离，防并发互冲）
    prompt_store.py    prompts 表读写 + 装配 system_prompt（内核+always-on skill）
    types.py           零依赖共享类型（SSEEvent/ToolResult/CancelToken…）
  llm/service.py     双协议(openai/anthropic)流式 + RPM/并发限流 + 建连退避重试 + 透传思考链
  tools/             Agent 工具 + registry
    registry.py        动态 schema 重建 + 参数强转 + 可用性过滤 + 可恢复错误重试
    catalog.py         工具注册总入口（内核工具 ∪ skill 声明的工具）
    builtins.py        finish / ask_user（控制流，置标志位）
    metadata.py        query_metadata（白名单表+列+关联+表级规则）
    sql_engine.py      execute_sql（sqlglot 只读校验 + 执行前列名校验 + 结果旁路）
    sql_template.py    get_sql_template（按名取样板 SQL，免占 system prompt）
    knowledge_tool.py  knowledge_search（转发 RAGFlow）
  datasource/        业务数据源管理 + 元数据反向同步（只同步表名/注释，列按需 live 取）
  ragflow/client.py  RAGFlow HTTP 客户端（文档管理 + 混合检索）
  memory/session.py  会话管理（Redis 热态 + PG 持久）
  storage/           平台库 ORM(models.py=表结构单一事实源) + pg/redis 客户端 + 结果旁路
  web/
    sse.py             SSE 事件类型 + 格式化（单租户，过滤关闭）
    routes/            对话侧(ask/session/result) + admin_* CRUD
config/              application.yml + profile（本地填值、不入库）
db/                  schema.sql / seed.sql（由 scripts/gen_*.py 从 ORM 生成）
static/index.html    前端单页（每次请求读文件，--reload 不监听 html）
docs/                本文档 + PLAN.md
```

---

## 四、一次问数的完整请求流

```
POST /api/ask/sse（Web）或 飞书消息（feishu/adapter.py）
  → Orchestrator.handle_message()   async generator[SSEEvent]，全程流式
      0. 会话忙闸门：同一 session 已有 run 在跑 → 直接拒（ERROR 事件），绝不并发
      1. 判 resume：status==awaiting_clarification → 断点恢复
      2. 装配 system_prompt = PromptStore.assemble_system_prompt()
         = KERNEL_PROMPT + 所有 enabled 且 mode=always_on 的 skill（按 order）
         + 尾部追加【当前日期】（让"本月/上月"可换算）
      3. 迭代 AgentLoop.run() 透传每个 SSEEvent
  → AgentLoop.run()
      准备 messages = [system?, ...历史, user]（resume 时用 checkpoint）
      for turn in range(max_turns):
          流式收 LLM（chat_stream）：
            - content 片段  → answer_delta（打字机）
            - reasoning 片段 → reasoning_delta（思考链）
            - tool_call 增量 → 累积成完整 tool_calls
          护栏：重复调用去重 / max_ask_user / max_meta_per_run / max_sql / 连续空错熔断
          逐工具 registry.execute() → ToolResult(摘要回灌 msgs)
            - ask_user  → 挂起(存 checkpoint) + clarification_needed + return
            - finish    → 取 args.answer 为最终答案 + break
          无 tool_call/finish → 结束
          逼近上下文阈值 → _maybe_compress（按 group 摘要，保留 query_metadata 结果）
      末事件必为 done / clarification_needed / cancelled / error 之一
      回写会话历史 + 审计 finalize
```

---

## 五、内核详解（`core/`）

### 5.1 Orchestrator（`orchestrator.py`）
- `handle_message()`：①会话忙闸门（内存 `_running` 集合，防同 session 两个 run 重叠冲掉审计）②判 resume ③装配 system_prompt + 注入日期 ④透传 loop 事件，loop 异常转 ERROR 不中断流。
- **不含业务逻辑**。状态转移由 loop 内的 `SessionState` 驱动。

### 5.2 AgentLoop（`agent_loop.py`）— ReAct 循环
- `run()`：流式收 content+tool_calls 增量 → 逐工具执行 → 摘要回灌 → 重复，直到 finish / 无 tool_call / 护栏 / 取消。
- **护栏**（`_limits`，admin 改 `agent_limits` 表后 `reload_limits` 热更新）：
  | 护栏 | 默认 | 作用 |
  |------|------|------|
  | `max_turns` | 30 | 最大推理轮数 |
  | `max_ask_user` | 2 | 单轮最多追问几次 |
  | `max_sql` | 10 | 单次对话 execute_sql 硬上限（防烧网关额度） |
  | `max_sql_fail_streak` | 3 | 连续空/错几次 → 提示 LLM 收手（**已接上**：streak 自增+比较） |
  | `max_meta_per_run` | 1 | 每轮 query_metadata 上限 |
- **冗余 SQL 检测**（`_sql_redundant`）：sqlglot 抽 (表, WHERE 谓词, 列)，同表同谓词且列⊆已查 → 判冗余，提示 LLM 别重查。
- **上下文压缩**（`_maybe_compress`）：逼近 token 阈值（构造参数→env→模型窗口表→LLM 配置→32000 兜底）按 group 摘要，保留最近 2 group + 所有 query_metadata 结果。token 估算用 4 字符/token 启发式。
- `reload_registry()`：admin 改 skill 后热替换工具集。

### 5.3 SessionState（`session.py`）
状态机 `IDLE/RUNNING/AWAITING_CLARIFICATION/DONE/ERROR` + `ALLOWED_TRANSITIONS`。`ask_user` 挂起存 `LoopCheckpoint`，回答后 `resume` 注入为 tool 结果消息、删 checkpoint、转回 RUNNING。`sweep_stale_suspended` 清 30 分钟以上的僵死挂起。

### 5.4 AuditSink（`audit.py`）
逐事件缓冲，按 **trace_id 隔离**（`_traces` dict，每个 trace 独立 `_TraceState`）——并发 run 不会互冲丢记录（这是历史上"审计没记上"bug 的修复）。`finalize` 写 `audit_traces` + 批量 `audit_events`，DB 错误吞掉不中断。

### 5.5 PromptStore（`prompt_store.py`）
`prompts` 表是提示词单一事实源。`assemble_system_prompt()` = `KERNEL_PROMPT` + enabled&always_on skill（缓存）。`get/upsert/delete/list_all` 供 admin。**注意**：缓存是单进程的，多 worker 部署下 admin 改 prompt 不会跨进程生效（待 Redis pub/sub，P5）。

---

## 六、工具层（`tools/`，全原子，无胖工具）

| 工具 | 作用 | 连哪 | 备注 |
|------|------|------|------|
| `query_metadata` | 白名单表+列+关联+表级规则 | 平台库 + 业务库 live 取列 | 列分类 metric/dimension，短维度列抽样真实值 |
| `execute_sql` | 只读查业务数据 | 业务库动态连 | sqlglot 禁 DDL/DML + **执行前列名校验**（拦截幻觉字段，根因：模板+模式推断） |
| `get_sql_template` | 按名取样板 SQL（unpivot/同比/环比） | 平台库 sql_templates | 免占 system prompt，LLM 按需调 |
| `knowledge_search` | 检索文档片段 | RAGFlow | 空/失败返回提示让 LLM 别重试 |
| `finish` / `ask_user` | 结束 / 澄清 | 置标志位 | 控制流，loop 观察 |

- **Registry**（`registry.py`）：每次 `openai_tools()` 重建 schema（防幻觉调隐藏工具）+ `coerce_tool_args` 强转 LLM 字符串参数 + `availability` 过滤（缺可选依赖自动隐藏工具）+ `_call_with_retry` 对超时/连接错退避重试，非可恢复错误回 `ToolResult` 让 LLM 自愈。
- **Catalog**（`catalog.py`）：注册总入口 = 内核工具 ∪ enabled skill 声明的工具。
- **execute_sql 列校验**（`sql_engine._validate_columns`）：执行前 sqlglot 解析 `alias.col`，对照 `information_schema` 真实列（5 分钟缓存），命中幻觉字段直接返回带真实列名的错误，**不访问数据库**。这是"字段不存在"报错频发的根治点。

---

## 七、LLM 层（`llm/service.py`）

- `chat_stream(messages, tools, purpose="analysis")`：按用途（analysis/attribution）从 `llm_config` 表取配置，双协议分发（openai / anthropic）。
- **限流**：入口 RPM 时间窗（`_throttle`）+ 并发信号量（`_sem`），配置为 None 则不限。
- **重试**：建连阶段可恢复错误（Timeout/Operational/Connection…）指数退避+jitter+retry-after；流中途断不重试（避免重复输出），交 loop 错误自愈。
- **思考链透传**：openai 的 `reasoning_content` / anthropic 的 `thinking_delta` 映射到 `_Chunk.reasoning`，破除工具阶段静默。

> **已知性能点**：生成复杂 SQL（如 unpivot 60+ 列）那一轮 LLM 调用慢（思考模型 + 长 SQL + 内网网关）。`get_sql_template` 取模板秒回，慢的是模型"想+手写"巨型 SQL。根治方向：确定性的 unpivot 用代码生成器拼，模型只选表/指标。

---

## 八、飞书通道（`feishu/`）

### 8.1 Adapter（`adapter.py`）
- 独立 daemon 线程跑 Lark WebSocket，`_on_message_sync` 收消息 → 白名单 → `run_coroutine_threadsafe` 投到主 loop 处理（ACK 秒回，长 run 不阻塞 ACK）。
- 复用同一个 Orchestrator，区别只在把 SSE 事件桥接成**飞书流式卡片**更新。
- 菜单：建会话 / 列会话；卡片按钮：切会话 / 选澄清选项。
- 会话并发防护在 **Orchestrator 的 `_running` 闸门**（同 session 忙即拒），不靠消息级去重。

### 8.2 流式卡片设计（`card.py` + `adapter.CardStream`）— 当前实现

**硬约束（实测）**：
- `card_element.acreate`（流式插入）：`collapsible_panel` 必须最小化（仅 tag/expanded/header.title/elements），加 background/border/padding 报 300315。
- `card_element.acontent`：**顶级 markdown 元素可靠**（答案打字机就靠它）；**折叠面板内部嵌套元素不可靠**（返回 success 但前端不刷新）。

**因此生成期用"单清单流式"，不用实时折叠面板**：
- 流式卡 body = `[操作过程清单 PROC_EID, 答案 ANSWER_EID]` 两个顶级 markdown。
- 每步 `on_tool` → `acontent(PROC_EID)` 往清单追加一条 `✓ 步骤`（全程只更新这一个元素，不再每步 insert 折叠框）。
- 答案 `acontent(ANSWER_EID)` 全量打字机。
- `on_done`：关流式 + `build_final_card` 全量重建 → 把所有步骤(+思考)折进**一个** collapsible_panel，答案在面板外可见。

**取舍**：生成期清单是顶级文字（不可点击——飞书流式卡无法可靠刷新"实时折叠面板"），跑完那一瞬变成可点的折叠面板。这是平台限制下的可靠最优解；不赌 `aupdate`（返回 success 不保证前端重画）。

---

## 九、数据层表结构（`storage/models.py` 是单一事实源）

平台库主要表（无独立 migration，`models.py` 的 ORM 类即表定义，`db/schema.sql` 由 `gen_schema.py` 生成）：

| 表 | 作用 | 关键列 |
|----|------|--------|
| `sessions` / `messages` | 会话 / 消息流 | session_id, role, content, trace_id |
| `audit_traces` / `audit_events` | 审计汇总 / 逐事件 | trace_id, seq, event_type, content_json |
| `loop_checkpoints` | ask_user 挂起快照 | messages_json, pending_tool |
| `query_results` | execute_sql 全结果旁路 | result_id, columns_json, rows_json |
| `llm_config` | 动态 LLM 配置（多行） | model, base_url, api_key, protocol, purposes[], enabled |
| `feishu_config` | 飞书通道配置 | app_id, app_secret, whitelist, enabled |
| `agent_limits` | AgentLoop 护栏（单行 default） | max_turns/max_sql/… |
| `prompts` | 提示词/skill 单一事实源 | scene(PK), content, tools, mode, order, enabled, version |
| `datasources` | 业务库连接 | host/port/db_name/username/password_enc/sync_scope |
| `metadata_tables` / `metadata_columns` | 同步表名/列元数据 | datasource_id, table_name, enabled |
| `table_relations` | 逻辑关联 | datasource_id, join_keys_json |
| `business_rules` | 表级业务口径 | table_name, rule_text, enabled |
| `sql_templates` | SQL 样板（**全局，无 datasource_id**） | name, sql_template, usage, enabled |
| `ragflow_config` | RAGFlow 配置（单行 default） | base_url, api_key, dataset_ids, enabled |

---

## 十、配置分层（别放错地方）

- **`config/application.yml` + profile**（`application-dev.yml` 本地填值、不入库）：**只放基础设施**——app / redis / postgres / feishu / auth。加载在 `src/config.py`。
- **数据库表**：LLM 模型/密钥/协议/限流（`llm_config`）、飞书（`feishu_config`）、护栏（`agent_limits`）、提示词（`prompts`）、SQL 模板（`sql_templates`）、RAGFlow（`ragflow_config`）、业务规则（`business_rules`）——全部 admin 后台热更新。
- **铁律**：LLM 的 model / api_key / base_url / 协议 / 限流 **永远不进 yml**，只存 `llm_config` 表。`src/config.py` 根本不读 llm 段。

---

## 十一、设计原则

1. **极简内核 + 只处理真见过的失败**：内核只做协议；稳健靠四样——完整 schema + 写到位的 description、错误回传不静默、loop 上限、仅危险操作确认。
2. **能力 = tool × skill**：tool 决定能不能，skill 决定何时。一个能力只挂一个工具，复用而非重写。
3. **热更新优先于重启**：可变配置进数据库表，不进 yml；改完重建 registry / reload_limits 即时生效。
4. **优雅失败 > 重型防御**：超时/报错让 LLM 干净汇报，错误回传 loop 即重试。
5. **意图涌现，不分类器**：靠工具 description 区分度（execute_sql=业务数据 / knowledge_search=文档）+ 常驻 skill，单 loop 内自然分流。

---

## 十二、已知问题 / 技术债（按代码现状核对）

| 项 | 现状 | 备注 |
|----|------|------|
| `delete_datasource` 曾引用不存在的 `SqlTemplate.datasource_id` | **已修**（本次） | sql_templates 是全局表，删源不再级联删模板 |
| `_summarize_segment` 的 `hasattr(llm,'summarize')` 分支 | 死代码 | LLMService 无 summarize，恒走 chat()；无害可清 |
| Prompt/工具缓存单进程 | 多 worker 不跨进程生效 | 待 Redis pub/sub（P5） |
| `metadata_columns` 表 | sync 不写列（列 live 取） | 表主要靠 admin 手编，sync 数据部分为空 |
| SSL 全局关验证（main.py） | 内网自签 CA 绕过 | 安全味，仅限内网部署 |
| 生成期操作过程不可点击 | 飞书流式卡平台限制 | 跑完即折叠，见 §8.2 |

---

## 十三、不做（防过度工程边界）

- 独立意图分类器（引入往返+误差，和 loop 内决策重复）
- 胖工具（自包方法论+重复调知识库）——归因走 skill + 原子工具
- 强制任务拆解 planner（简单查询纯加延迟）
- circuit breaker / 静默重试（提前替没出现的问题写代码）
