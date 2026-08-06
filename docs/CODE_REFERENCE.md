# NL2SQL 代码逐文件说明书

> 本文档逐文件、逐函数解释 `src/` 下每个模块干什么。配合 `ARCHITECTURE.md`（大图）阅读。
> 按 test 分支代码核对。「函数签名」列真实参数；「作用」一句话说清。

---

## 总览（调用层次）

```
入口层   main.py（FastAPI lifespan + auth） → web/routes/*（HTTP/SSE）
                                   ↓
编排层   core/orchestrator.py → core/agent_loop.py（ReAct 循环）
                                   ↓
能力层   llm/service.py（LLM） + tools/*（工具，registry 调度）
                                   ↓
数据层   storage/*（平台库ORM） + datasource/*（业务库） + ragflow/*（知识库） + memory/*（会话）
                                   ↓
飞书层   feishu/adapter.py + card.py（独立入口，复用编排层）
```

---

# 一、入口与配置

## `src/main.py`
FastAPI 应用入口。装配 lifespan 管理的单例（LLM/orchestrator/sessions/feishu），并在所有路由上挂 HMAC 单账号鉴权。

| 符号 | 作用 |
|------|------|
| `_sign_token(username, secret)` | 造无状态 token `username:expire:hmac` |
| `_verify_token(val, secret)` | 校验 Bearer token（HMAC+过期），返回 username 或 None |
| `_bearer(req)` | 从 `Authorization: Bearer …` 取 token |
| `_Lazy(key)` | 代理对象，`__getattr__` 惰性解析到 `_app_state[key]`——让同步注册期路由能拿到异步初始化的组件 |
| `lifespan(app)` | 启动：并行连 PG+Redis → 建 LLMService/PromptStore/AgentLoop/Orchestrator → 启动飞书 → 启动+定时清理僵死挂起会话；关闭：dispose 连接池 |
| `create_app()` | 建 FastAPI，加 CORS，注册 `/`、`/api/login`、`/api/logout`、`/api/me`、auth_guard 中间件、~14 个路由（经 `_Lazy`） |
| `app` | 模块全局 = `create_app()`（ASGI 对象 `src.main:app`） |

- **常量**：`_TOKEN_TTL=7天`；`_INDEX_HTML`（import 时读，`index()` 仍每次读文件——因 `--reload` 不监听 html，故每次重读保证热更新，**不是死代码**）；`SWEEP_INTERVAL=300`/`SWEEP_MAX_AGE=30`。
- **注意**：import 时全局关闭 SSL 验证（`CERT_NONE`）——内网自签 CA 绕过，仅限内网。

## `src/config.py`
YAML → dataclass。**只读基础设施，绝不读 LLM**（LLM 在数据库）。

| 符号 | 作用 |
|------|------|
| `LLMConfig` | dataclass：api_key/api_base/model/temperature=0.0/timeout=60/max_context=32000/protocol="openai"/rpm_limit/concurrency（LLMService 内部持有 DB 读出的配置） |
| `RedisConfig`/`PostgresConfig`/`AppConfig`/`FeishuConfig`/`AuthConfig` | 各基础设施段（FeishuConfig 含 whitelist/card_throttle_ms；AuthConfig password 空则关闭鉴权） |
| `ApplicationConfig` | 以上聚合 + profiles 列表 |
| `_deep_merge(base, override)` | 递归合并 dict，override 胜 |
| `_build(d)` | 规范化 profiles.active（str 或 list）→ 构造 ApplicationConfig |
| `load_config(config_dir, profile)` | 读 base YAML + 合并 profile YAML |

## `src/logging.py`
统一日志，全部在 `nl2sql.*` 命名空间、输出 stdout。
- `setup_logging(level)`：设 level + 只加一次 StreamHandler（`_CONFIGURED` 守卫幂等）。
- `get_logger(name)`：返回 `nl2sql.<name>`。

---

# 二、编排内核（`src/core/`）

## `src/core/orchestrator.py`
消息入口编排。**不含业务逻辑**。

| 符号 | 作用 |
|------|------|
| `Orchestrator.__init__(loop, sessions, prompt_store=None, audit=None)` | 组合 agent_loop + session_manager + prompt_store；初始化内存 `_running` 闸门 |
| `handle_message(user_id, session_id, text, mode, trace_id, cancel_token=None)` → AsyncIterator[SSEEvent] | ①会话忙闸门（同 session 已有 run → 拒，ERROR 事件）②判 resume（status=awaiting_clarification）③装配 system_prompt + 注入【当前日期】④透传 loop.run 事件，loop 异常转 ERROR |

## `src/core/agent_loop.py`
自主 ReAct 循环 + 护栏 + 上下文压缩 + 审计挂载。

| 符号 | 作用 |
|------|------|
| `_args_key(args)` | args 稳定 JSON 序列化（重复调用去重用） |
| `_sql_failed(summary)` | execute_sql 结果是否空/错（rows==0 或非 JSON） |
| `_sql_extract(sql)` | sqlglot 抽 (表, WHERE谓词, 列)，失败返空集 |
| `_sql_redundant(sql, prev)` | 判冗余 SQL：同表同谓词且列⊆已查 |
| `_normalize_args(raw)` | LLM 返回的 args(str/dict/其他)→dict |
| `_to_openai_tool_calls(tool_calls)` | 累积的 [{name,args,id}] → OpenAI assistant 消息 tool_calls 格式 |
| `_model_window(model)` | 按模型名前缀查上下文窗口（_MODEL_WINDOWS 表），未知返 0 |
| `_split_into_groups(msgs)` | 按轮次（user 起）切消息组 |
| `AgentLoop.__init__(llm, registry, state, *, max_turns=30, max_ask_user=2, max_context=None, max_sql=10, max_sql_fail_streak=3, max_meta_per_run=1, session_manager=None, audit=None)` | 存依赖；护栏存 `_limits` dict |
| `reload_limits(limits)` | 热更新 `max_*` 护栏（admin 改 agent_limits 表后调） |
| `reload_registry(registry)` | 热替换工具集（admin 改 skill 后调） |
| `run(session_id, user_id, user_msg, trace_id, cancel_token, is_resume=False, system_prompt=None)` → AsyncIterator[SSEEvent] | 主循环。每轮：流式收 answer_delta/reasoning_delta + 累积 tool_call_delta → 护栏（重复去重/max_ask_user/max_meta_per_run/max_sql/连续空错熔断）→ 逐工具执行 → ask_user 挂起 / finish 结束 / max_turns 兜底。末事件 done/cancelled/error，回写历史 |
| `_audit_event(evt_type, data, trace_id, turn)` | 转发一事件给审计 sink（无则跳过，吞错） |
| `_audit_finalize(success, final_answer, trace_id)` | 关闭审计 trace |
| `_persist_history(session_id, user_msg, assistant_msg, trace_id)` | 回写用户+助手轮次到会话历史 |
| `_prepare_messages(session_id, user_msg, is_resume, system_prompt)` | resume 用 checkpoint；否则转 RUNNING 拼 [system?, ...历史, user] |
| `_maybe_compress(msgs)` | 逼近 token 阈值 → 按 group 摘要，保留最近 2 group + 所有 query_metadata 结果 |
| `_resolve_max_context()` | 阈值解析：env → 构造参数 → 模型窗口表 → LLM 配置 → 32000 |
| `_summarize_segment(segment)` | 摘要一段消息（有 `summarize` 用之，否则 chat+COMPACT_SUMMARY_PROMPT——**注：LLMService 无 summarize，恒走 chat，该分支死代码**） |

- **常量**：`ASK_USER`；`COMPACT_SUMMARY_PROMPT`；`_MODEL_WINDOWS`（deepseek-v3/v4 64k，qwen3-32b/72b 32k，qwen2.5-72b 131072）。

## `src/core/session.py`
会话状态机 + ask_user 挂起/恢复。

| 符号 | 作用 |
|------|------|
| `SessionStatus(Enum)` | IDLE/RUNNING/AWAITING_CLARIFICATION/DONE/ERROR |
| `ALLOWED_TRANSITIONS` | 合法状态转移表 |
| `ResumedContext` | dataclass：messages/checkpoint_id/pending_tool |
| `SessionState(session_manager)` | 状态机驱动器 |
| `current_status(sid)` / `transition(sid, target)` | 读/写状态（非法转移抛 ValueError） |
| `is_suspended(sid)` | 是否挂起 |
| `suspend(sid, messages, pending_tool)` | 存 LoopCheckpoint + 转 awaiting，返 checkpoint id |
| `resume(sid, user_reply)` | 注入用户回答为 tool 结果消息 + 删 checkpoint + 转 RUNNING |
| `expire_suspended(sid)` | 丢 checkpoint + awaiting→IDLE |
| `sweep_stale_suspended(max_age_minutes=30)` | 清 30 分钟以上僵死挂起，返清理数 |

## `src/core/types.py`
零依赖共享类型（刻意无 import 打破循环）。
- `CancelToken`（cancel/cancelled/check）、`LoopContext`(session_id/user_id/trace_id/channel)、`ToolResult`(summary/result_id/finished/suspended/options)、`SSEEvent`(type/data/trace_id)、`ToolHandler`、`ToolDefinition`(name/description/parameters/handler/availability)。

## `src/core/audit.py`
逐事件落库，按 trace_id 隔离防并发互冲。
- `_TurnBuffer`、`_TraceState`（每 trace 累加器）。
- `AuditSink.begin/event/finalize`：begin 建 trace、event 记事件（answer_delta 合并进 turn buffer）、finalize 弹 trace 写 AuditTrace+AuditEvent（吞 DB 错）。

## `src/core/prompt_store.py`
prompts 表单一事实源 + 装配 system_prompt。
- `KERNEL_PROMPT`（纯协议角色 prompt）、`PromptStore.get/upsert/delete/list_all/list_active_skills/assemble_system_prompt/refresh`。
- `assemble_system_prompt()` = KERNEL_PROMPT + enabled&always_on skill（缓存 `_ASSEMBLED_KEY`）。
- **注意**：缓存单进程，多 worker 不跨进程生效（待 Redis pub/sub）。

---

# 三、LLM 层

## `src/llm/service.py`
双协议流式 + 限流 + 重试 + 思考链透传。

| 符号 | 作用 |
|------|------|
| `_Resp` / `_Chunk` | 内部响应/流式片段（content/reasoning/tool_call_delta） |
| `LLMService.__init__()` | 初始化（动态配置缓存 + 限流闸） |
| `_load_dynamic(purpose)` | 从 llm_config 表按用途取最新 enabled 模型行 |
| `reset_dynamic()` | 清动态配置缓存（admin 改模型后调） |
| `_resolve_config(purpose)` | 取配置（无则抛"未配置"） |
| `_get_client(cfg)` | 建 openai/anthropic 客户端（按协议） |
| `chat_stream(messages, tools, purpose="analysis")` → AsyncIterator[_Chunk] | 入口：按用途取配置 → 限速 → 并发闸 → 分发 |
| `_dispatch(proto, cfg, messages, tools)` | 选 openai/anthropic 流式 |
| `_apply_rate_limit(rpm_limit, concurrency)` | 建 RPM 窗 deque + 并发 Semaphore |
| `_throttle()` | RPM 时间窗满则 sleep |
| `_stream_openai(cfg, messages, tools)` | openai 流式；捕 `reasoning_content` 思考链 |
| `_stream_anthropic(cfg, messages, tools)` | anthropic 流式；捕 `thinking_delta` 思考链（max_tokens=4096） |
| `chat(messages, tools, purpose)` | 非流式一次性（压缩摘要用） |
| `_split_anthropic_messages(messages)` | 拆出 system（anthropic 单独传） |
| `_is_retryable/_is_fatal_quota/describe_llm_error/_retry_after_seconds/_err_body` | 错误分类：可重试(超时/连接) / 致命(额度) / 友好描述 / retry-after 解析 |

- **常量**：`_RETRYABLE_HINTS`、`_FATAL_QUOTA_HINTS`。
- **重试策略**：建连阶段可恢复错误指数退避+jitter+retry-after；流中途断不重试（避免重复输出）。

---

# 四、工具层（`src/tools/`）

## `src/tools/registry.py`
运行时工具注册表。

| 符号 | 作用 |
|------|------|
| `coerce_tool_args(parameters, args)` | 按 schema 强转 LLM 字符串参数(int/float/bool/array/object)，失败保留原值 |
| `require_module(module_name)` | 闭包 availability：缺可选依赖则自动隐藏该工具 |
| `ToolRegistry.register/get/available_defs/openai_tools` | 注册/查/按可用性过滤/每次重建 OpenAI schema（防幻觉隐藏工具） |
| `ToolRegistry.execute(name, args, ctx, cancel_token)` | 查工具→可用性→强转→`_call_with_retry` 跑 handler；错误返 ToolResult 让 LLM 自愈 |
| `_call_with_retry(coro_fn, *, max_retries=2, base_delay=0.5)` | 可恢复错误退避重试；CancelledError/不可恢复立即抛 |
| `_is_retryable(err)` | Timeout/Operational/Connection/Reset/Interface |

## `src/tools/builtins.py`
内核控制流工具（置标志位，loop 观察）。
- `_finish` → `ToolResult(summary=answer, finished=True)`；`FINISH` 定义（answer 必填）。
- `_ask_user` → `ToolResult(summary=question, suspended=True, options)`；`ASK_USER` 定义（question 必填，options 可选）。

## `src/tools/catalog.py`
工具注册总入口。
- `KERNEL_TOOL_NAMES=("finish","ask_user")`。
- `build_catalog(sql_template_desc)`：注册 FINISH/ASK_USER/QUERY_METADATA/EXECUTE_SQL/KNOWLEDGE_SEARCH + get_sql_template。
- `resolve_active_tool_names(active_skills)`：内核工具 ∪ 各 skill 声明的工具（去重保序）。
- `build_registry(sql_template_desc, prompt_store, active_skills)`：读 DB active skill → 组装 ToolRegistry（声明了不存在工具则告警跳过）。

## `src/tools/metadata.py` — `query_metadata`
返回白名单表(名/注释 + live 列) + 逻辑关联 + 表级规则。
- `_list_enabled_tables`/`_list_relations`/`_list_table_rules`：读平台库。
- `_classify(name, comment, type_str)`：列分 metric/dimension + 是否抽样。
- `_enrich_columns`：标 role + 短维度列 `SELECT DISTINCT ... LIMIT 10` 抽样真实值（标识符经 dialect 引号转义）。
- `query_metadata(args, ctx, cancel_token)` → JSON `{tables, relations}`；`QUERY_METADATA` 定义。

## `src/tools/sql_engine.py` — `execute_sql`
只读查业务库 + 结果旁路。
- `validate_sql(sql)`：sqlglot 只允许 Select/Union/Intersect/Except，禁 DDL/DML。
- `_execute(engine, sql)`：`wait_for(EXEC_TIMEOUT=30)`，fetchmany 上限 MAX_ROWS=10000。
- `_real_columns(engine, ds_id, schema, table)`：5 分钟缓存取真实列名（information_schema）。
- `_validate_columns(sql, engine, ds_id)`：**执行前** sqlglot 解析 alias.col 对照真实列，命中幻觉字段返带真实列名的错误（**不访问数据库**——根治"字段不存在"频发）。
- `execute_sql(...)`：校验→取数据源→列校验→执行→save_result 旁路→过滤敏感列→返 JSON `{result_id, rows, columns, preview}`；失败返带提示的 ToolResult（自愈，不抛）。
- **常量**：MAX_ROWS=10000、EXEC_TIMEOUT=30、_COL_CACHE_TTL=300。

## `src/tools/sql_template.py` — `get_sql_template`
按名取样板 SQL（免占 system prompt）。
- `list_enabled_templates()`、`get_sql_template(...)`（无则返"现有模板：…"提示）、`build_template_desc(tpls)`（拼工具 description）、`make_get_sql_template(desc)`。
- **注意**：工具 description 是启动快照（新模板需重启才进 description），handler 内容是 live 的。

## `src/tools/knowledge_tool.py` — `knowledge_search`
转发 RAGFlow 检索。
- `knowledge_search(args, ctx, cancel_token)`：调 `get_ragflow_client().retrieve(query, top_k=5)`；空/失败返友好提示让 LLM 别重试；片段截 800 字。`KNOWLEDGE_SEARCH` 定义。

---

# 五、数据层

## `src/storage/models.py`
ORM = 表结构单一事实源（无独立 migration）。`Base(DeclarativeBase)` + 16 个模型：
Session/messages、AuditTrace/AuditEvent、LoopCheckpoint、QueryResult、LlmConfigRow、FeishuConfigRow、AgentLimitsRow、Prompt、Datasource、MetadataTable/MetadataColumn、TableRelation、BusinessRule、SqlTemplate、RagflowConfigRow。
- **约束**：uq_ds_schema_table、uq_table_col。
- **注意**：MetadataColumn 表 sync 不写列（列 live 取），主要靠 admin 手编；SqlTemplate 无 datasource_id（全局表）；QueryResult 无 datasource_id 列（execute_sql 传了但被忽略）。

## `src/storage/pg_client.py`
平台库 engine + 异步 session 工厂。
- `_pg_url(config)`：拼 `postgresql+asyncpg://`（user/pwd quote_plus）。
- `init_db(config)`：建全局 `_engine` + `_AsyncSessionFactory`（expire_on_commit=False）。
- `AsyncSessionFactory()`：返 session（未 init 抛 RuntimeError）。

## `src/storage/redis_client.py`
Redis + 自动降级进程内 dict。
- `_InMemory`：降级后端（TTL 语义，ttl<=0 即删）。
- `RedisClient.connect/get/set/delete`：ping 失败则装 _InMemory，available=False；统一 TTL 语义。

## `src/storage/query_results.py`
结果旁路（PG 持久 + Redis 1h TTL 快取）。
- `save_result(session_id, columns, rows, datasource_id=None)` → result_id（PG 必成、Redis 尽力；datasource_id 当前忽略）。
- `get_result(result_id)`：Redis 优先，miss 回 PG → `{columns, rows, total}`。

## `src/datasource/manager.py`
业务库连接池（每数据源缓存 engine）+ CRUD。
- `get_engine(ds_id)`/`test_connection`/`list_schemas`/`get_sync_scope`/`list_datasources`/`create_datasource`/`update_datasource`/`delete_datasource`。
- engine URL：`mysql+aiomysql://`（password_enc 明文）。
- **`delete_datasource`（已修）**：级联删 metadata_columns/tables/relations（外键无 CASCADE），**不再**误删 sql_templates（该表无 datasource_id，原代码必崩 AttributeError）。

## `src/datasource/metadata_sync.py`
反向同步**表名/注释**（不同步列，列按需 live 取）。
- `sync_metadata(ds_id, engine, sync_scope, schema_name)`：保 source=manual 行，返 {added,updated,skipped}。
- `fetch_table_columns(engine, table_name, schema)`：live 取列（information_schema 优先 → Inspector → SHOW FULL COLUMNS 兜底）。
- `fetch_objects(engine, schema)`：live 对象列表。
- **注意**：不删业务库已移除的表（待办）；`_fallback_columns` 拼 `{schema}.{table}` 未引号转义（受信 admin 输入，低危）。

## `src/ragflow/client.py`
RAGFlow HTTP 客户端（文档管理 + 混合检索）。本系统不做向量/embedding，全归 RAGFlow。
- `RagflowConfig`(base_url/api_key/dataset_ids/top_k=5/similarity_threshold=0.2/vector_similarity_weight=0.3/enabled)；`ready`/`api_base`（正确去 `/api/v1`/`/v1` 后缀，避 rstrip 字符集 bug）。
- `RagflowClient.load_config/_require/list_datasets/create_dataset/upload_document/parse_documents/list_documents/delete_documents/retrieve`。
- `retrieve(...)` 配置不全/无结果返 `[]`（不抛）。
- `get_ragflow_client()`：进程单例。

## `src/memory/session.py`
会话管理（Redis 热态 + PG 持久）。
- `SessionManager.create_session/get_session/set_status/append_message/get_messages/delete_session(逻辑删)/rename_session/fill_title_if_empty/list_sessions(PG only)`。
- **常量**：SESSION_TTL=3600、REDIS_ROOT="nl2sql"、SESSION_KEY/MSGS_KEY。

---

# 六、Web 层（`src/web/`）

## `src/web/sse.py`
- `SSEEventType(Enum)`：全部事件类型（CLARIFICATION_NEEDED/ANSWER_DELTA/REASONING_DELTA/DONE/ERROR/TOOL_CALL/TOOL_RESULT/WARNING/CANCELLED…）。
- `ViewerMode(Enum)`：ADMIN/USER。
- `should_emit/filter_event`：**当前恒 True**（单租户全透明，过滤关闭，`_USER_HIDDEN` 空）。
- `format_sse(event)`：`event: <type>\ndata: <json {data,trace_id}>\n\n`。

## 对话侧路由
- **`routes/ask.py`**：`POST /api/ask/sse`（流式，生 trace_id+CancelToken、自动起标题、透传 orchestrator 事件）、`POST /api/ask/cancel`、`POST /api/ask`（同步，飞书/Aily 用）。`_running` 进程内取消注册表（多 worker 不共享）。
- **`routes/session.py`**：会话 列表/建/消息/改名/逻辑删（user_id 仅 query 参数，无鉴权，P5）。
- **`routes/result.py`**：`GET /api/result/{id}`（columns/rows/total）、`GET /api/result/{id}/export`（xlsx，openpyxl）。

## Admin 路由（CRUD，多为薄封装）
| 路由文件 | 端点 | 说明 |
|---------|------|------|
| `admin_llm.py` | GET/PUT/DELETE `/api/admin/llm-config`、POST `/discover` | 模型配置 CRUD + `/v1/models` 发现 + purposes 互斥 + reset_dynamic |
| `admin_prompts.py` | GET/POST/PUT/DELETE `/api/admin/prompts` | 提示词/skill CRUD，写后 `_hot_reload` 重建 registry |
| `admin_metadata.py` | GET metadata/enabled-tables/columns/objects、table_relations CRUD、dashboard | 元数据读 + 关联 CRUD + 看板（live 业务库 left-join PG） |
| `admin_datasource.py` | GET/POST/PUT/DELETE datasource、test_connection、schemas、sync | 数据源 CRUD + 测连 + 同步 |
| `admin_audit.py` | GET traces/trace/stats/filters | 审计列表/详情/统计/筛选项 |
| `admin_feishu.py` | GET/PUT `/api/admin/feishu-config` | 飞书通道配置（单行 default） |
| `admin_agent_limits.py` | GET/PUT `/api/admin/agent-limits` | 护栏热更新（写后 loop.reload_limits） |
| `admin_business_rules.py` | GET/POST/PUT/DELETE `/api/admin/business-rules` | 表级业务口径 CRUD |
| `admin_sql_templates.py` | GET/POST/PUT/DELETE `/api/admin/sql-templates` | SQL 模板 CRUD |
| `admin_ragflow.py` | GET/PUT config、datasets/documents(列表/上传/解析/删) | RAGFlow 配置 + 文档管理 |

---

# 七、飞书层（`src/feishu/`）

## `src/feishu/adapter.py`
飞书机器人适配器，独立线程跑 Lark WS，复用同一 Orchestrator。

**CardStream（每条 run 一张流式卡）：**
| 符号 | 作用 |
|------|------|
| `__init__(lark_client, card_id, throttle_s)` | 管理双元素流式卡（操作过程清单 PROC_EID + 答案 ANSWER_EID） |
| `on_answer_delta(text)` | 累加答案 + 调度节流 flush |
| `on_reasoning_delta(text)` | 累加思考（不实时 flush，done 折进面板） |
| `on_tool(token, line, rows=None)` | 往 PROC_EID acontent 追加一条 ✓（全程只更新这一个元素） |
| `on_tool_call/on_tool_result(name, args/summary)` | 造友好步骤行（重复调用去重） |
| `on_done(answer=None)` | flush + 关流式 + build_final_card 全量重建（失败兜底 acontent 答案） |
| `on_clarify/on_error` | 渲染澄清/错误 + 关流式 |
| `_schedule_flush/_flush_after/_cancel_and_flush/_flush` | 答案节流打字机 |
| `_stream_text(eid, content)` | card_element.acontent（全量打字机） |
| `_close_streaming(answer)` | card.asettings 关 streaming_mode + 更新 summary |
| `_update_card_full(card_json)` | card.aupdate 全量替换 |
| `_next_seq()` | 卡片操作单调 sequence |

**FeishuAdapter：**
- `start/reload/stop`、`_resolve_runtime_cfg`（DB 读最新飞书配置，回退 yml）、`_run_ws`（独立 loop + 注册 p2 handler + WsClient.start 阻塞）。
- `_on_message_sync`（收消息→白名单→投主 loop，ACK 秒回）、`_on_card_sync`（卡片按钮：切会话/选澄清）、`_on_menu_sync`（菜单：建会话/列会话）。
- `_handle_menu/_switch_session/_send_session_list/_strip_at_mention/_send_text/_create_card`。
- **并发防护**：靠 Orchestrator `_running` 闸门（同 session 忙即拒），不靠消息级去重。

## `src/feishu/card.py`
飞书卡片 JSON 构造。
- `ANSWER_EID="answer"`/`PROC_EID="proc"`。
- `_icon`（标准图标，当前未渲染）、`_summary_text`（答案去 markdown 取前 50 字作 summary）、`_panel`（极简 collapsible_panel，流式 insert 校验严，不能加 background/border/padding）。
- `progress_markdown(titles)`：流式态操作过程清单内容（逐条 ✓）。
- `_proc_panel(proc_items, reasoning)`：done 后汇总折叠面板。
- `build_streaming_card()`：流式卡 body = `[PROC_EID, ANSWER_EID]` 两个顶级 markdown。
- `build_final_card(proc_items, answer, reasoning)`：done 全量卡 = 折叠面板(步骤+思考) + hr + 答案。
- `build_session_list_card(sessions, current_sid)`：会话列表卡片（按钮切会话）。
- **设计取舍**：飞书流式卡顶级 markdown 的 acontent 可靠，折叠面板内部嵌套元素不可靠——故生成期用单清单流式（不可点），done 后才折叠。

---

# 八、辅助

## `db/schema.sql` / `db/seed.sql`
由 `scripts/gen_schema.py`/`gen_seed.py` 从 ORM 生成。`schema.sql` = 全部建表+注释；`seed.sql` = 初始数据（默认 prompt/护栏/配置行）。
## `scripts/check_schema.py` / `check_seed.py`
校验生成产物与 ORM 一致。
## `config/application*.yml`
基础设施配置（app/redis/postgres/feishu/auth）。`application-dev.yml`/`-online.yml` 本地填值、不入库。
## `static/index.html`
前端单页（管理后台 + 问数界面），每次请求读文件（热更新）。

---

> **维护规则**：改了表结构改 `models.py`（事实源）后跑 `python3 scripts/gen_schema.py`；本文档随代码迭代。
