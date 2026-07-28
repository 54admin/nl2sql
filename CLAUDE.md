# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 本项目用中文沟通、中文写代码注释。回答也用简体中文。

## 常用命令

```bash
# 启动（uvicorn src.main:app --reload，监听 8000）
./run.sh

# 装依赖
pip install -r requirements.txt

# 全部测试（pytest，asyncio_mode=auto，testpaths=tests）
pytest

# 单个测试文件 / 单个用例
pytest tests/test_agent_loop.py
pytest tests/test_agent_loop.py::test_xxx

# 接口文档（启动后）
# http://127.0.0.1:8000/docs
```

**首次启动前**：先在 `config/application-dev.yml` 配好 postgres + redis 连接，再 `./run.sh`。LLM 模型/密钥**不在 yml 里配**——启动后调 `PUT /api/admin/llm-config` 存进数据库 `llm_config` 表（热更新）。

## 这是什么

NL2SQL AI 问数平台：用户用自然语言提问，Agent 自主查元数据 → 生成只读 SQL → 在**业务库**执行 → 把结果摘要回灌 LLM → 流式回答。对话全程 SSE 推送（思考/SQL/表格分区），支持多轮、澄清（ask_user 挂起）、取消、上下文压缩。

技术栈：FastAPI + SQLAlchemy 2.0(async) + asyncpg + Redis + pgvector + openai SDK（直连，去 langchain）+ sqlglot（SQL 护栏）。

## 请求主链路（改东西前先顺着这条线读一遍）

1. [src/web/routes/ask.py](src/web/routes/ask.py) `POST /api/ask/sse` → 起 SSE 流，生成 `trace_id` + `CancelToken`。
2. [src/core/orchestrator.py](src/core/orchestrator.py) `handle_message` 编排：
   - 名称纠错前置（[Normalizer](src/core/normalizer.py)：dict 精确 → fuzzy → LLM 兜底），有修正先发 `correction` 事件。
   - 组装 system_prompt = PromptStore(default 场景) + 业务规则(RuleStore) + SQL 样板。
   - 透传给 AgentLoop。`resume` 轮（会话处于 `awaiting_clarification`）跳过纠错，断点恢复。
3. [src/core/agent_loop.py](src/core/agent_loop.py) `run` —— 自主同步 ReAct 循环：
   - LLM 流式输出 → 收 `content`（边收边发 `answer_delta` 打字机）+ `tool_calls` 增量。
   - 逐工具执行 → 摘要回灌消息 → 重复，直到无 tool_call / finish / 护栏触发 / 取消。
   - 护栏：`max_turns`、重复调用检测、`ask_user` 次数上限。
   - `ask_user` 工具返回 `suspended` → 存 [LoopCheckpoint](src/storage/models.py) 挂起，等用户回答后 resume；`finish` 工具返回 `finished` → 终止发 `done`。
   - 上下文逼近窗口阈值 → 按 group 切分摘要压缩（对齐 Claude Code auto-compact）。
4. 工具见 [src/tools/](src/tools/)：`execute_sql`（业务库只读查询）/ `query_metadata`（查平台元数据）/ `knowledge_search`（向量检索）/ `attribution`（归因）/ `finish` / `ask_user` / `echo`(stub)。

## 双库分离（核心心智模型）

- **平台库**（PG，[src/storage/](src/storage/)）：存会话、消息、审计、元数据、配置、知识库。全程 SQLAlchemy ORM，连接串来自 `config/application.yml` 的 `postgres` 段。
- **业务库**（[src/datasource/manager.py](src/datasource/manager.py)）：用户真正要查的 StarRocks/MySQL/PG。`execute_sql` 工具按 `datasource_id` 动态拿 engine 连过去查，**不走平台库**。

别把两库搞混：元数据同步、审计、会话历史写平台库；问数结果从业务库捞。

## 必须知道的几个约定（读多文件才看得出来）

1. **LLM 配置走数据库，不走 yml**。[src/config.py](src/config.py) 只读 app/redis/postgres；模型/密钥/base_url/协议/限流全在 `llm_config` 表，按 `purpose`（analysis/embedding/attribution）分行 + 启停。[LLMService](src/llm/service.py) 按 purpose 取 enabled 行。同一网关常按 `protocol`（openai/anthropic）分额度桶。

2. **不用 Alembic 做迁移**。[src/storage/pg_client.py](src/storage/pg_client.py) 的 `_PG_MIGRATIONS` 是一条条幂等 `ALTER ... IF NOT EXISTS`。**加列/改约束就在这数组追加一条**，`init_db` 启动时幂等跑。`create_all` 只对全新表生效，已存在表靠这些 ALTER 演进。

3. **模型注释是单一事实源**。[src/storage/models.py](src/storage/models.py) 每个列的 `comment=` 中文注释会被 `_apply_model_comments` 刷进 PG 的 `COMMENT ON`。**改表/列注释只改 models.py，别维护第二份 SQL**。新增表/列在这里定义即可。

4. **测试用 sqlite 内存库，不走真 PG**。pytest `asyncio_mode=auto`；测试里 `init_db("sqlite://")` 走 [aiosqlite + StaticPool](src/storage/pg_client.py)，[conftest.py](tests/conftest.py) 每个测试结束 dispose engine。pgvector 的 `Vector` 在 [Embedding](src/storage/models.py) 里**延迟 import**（避免顶层 import 在 sqlite 下注册扩展导致测试 hang）。

5. **SSE 双模式过滤已废弃**。[src/web/sse.py](src/web/sse.py) 的 `ViewerMode`(user/admin) 枚举保留（前端/请求体在用），但 `should_emit` 恒 True——内网单租户，过程/SQL 全透传。要鉴权隔离等真有多租户再说。

6. **取消是进程内的**。[ask.py](src/web/routes/ask.py) 维护 `_running: trace_id → CancelToken`，`POST /api/ask/cancel` 置位，loop 在三处检查点响应。单进程够用；多 worker 部署需换 Redis 标志。

## 目录速览

- [src/core/](src/core/) 编排：orchestrator（入口）/ agent_loop（ReAct 循环）/ normalizer（纠错）/ session（状态机）/ audit（审计落库）/ `*_store`（prompt/rule/name 字典）
- [src/tools/](src/tools/) Agent 工具 + registry（schema 动态重建、参数 coerce 强转、可用性过滤、可恢复错误重试）
- [src/storage/](src/storage/) 平台库 ORM（[models.py](src/storage/models.py) 是表结构单一事实源）+ pg/redis 客户端 + 结果旁路
- [src/datasource/](src/datasource/) 业务数据源管理 + 元数据反向同步
- [src/knowledge/](src/knowledge/) 知识库解析 + 向量存储
- [src/web/routes/](src/web/routes/) 对话侧（ask/session/result）+ `admin_*` 后台 CRUD
- [docs/superpowers/](docs/superpowers/) 设计文档（specs）+ 实施计划（plans）—— 架构背景都在这

## 写代码的口径

- 新增/改表结构 → 改 [models.py](src/storage/models.py) + 追加一条 `_PG_MIGRATIONS`（PG 才需要，sqlite 测试自动重建）。
- 新增工具 → 写 `ToolDefinition` + 在 [builtins.py](src/tools/builtins.py) `default_registry()` 注册；`registry.execute` 已做参数强转和错误兜底回灌，handler 里**不要抛异常**，返回带错误摘要的 `ToolResult` 让 LLM 自愈。
- 任何会写库失败但不应打断对话主链路的操作（审计、历史回写），用 `try/except` 吞掉 + `log.warning`，参考 agent_loop 里的做法。
