# NL2SQL AI 问数平台

用自然语言问业务数据：用户提问 → Agent 自主查元数据 → 生成只读 SQL → 在**业务库**执行 → 结果摘要回灌 LLM → 流式回答。对话全程 SSE 推送（思考过程 / SQL / 表格分区），支持多轮、澄清、取消、上下文压缩。

> 内网单租户工具平台。技术栈：FastAPI + SQLAlchemy 2.0(async) + asyncpg + Redis + openai SDK（直连，去 langchain）+ sqlglot + RAGFlow（外部知识库）。

## 功能特性

- **NL2SQL 问数**：自然语言 → 只读 SQL → 业务库查询 → 表格 + 文字摘要
- **自主 ReAct 循环**：LLM 查元数据 → 执行 SQL → 摘要回灌 → 多轮，带试错熔断（防反复查烧网关额度）
- **多轮对话 + 上下文压缩**：对齐 Claude Code auto-compact（按 group 摘要）
- **窄内核 + skill 架构**：内核纯协议（角色+ReAct+时间），领域方法论（问数 nl2sql / 归因 attribution）以 always-on skill 注入稳定前缀；无 load_skill、无分类器，意图靠工具描述区分度涌现
- **澄清挂起**：缺关键信息用 ask_user 挂起，用户回答后断点恢复
- **知识库（RAGFlow）+ 归因**：文档检索转发外部 RAGFlow；归因复用 execute_sql+knowledge_search，方法论以 skill 常驻（不另起胖工具）
- **SQL 护栏**：sqlglot 只读校验（禁 DDL/DML/危险函数）
- **管理后台**：数据源 / 元数据 / 模型配置 / 业务规则 / SQL 模板 / 知识库 / 问数统计
- **模型配置**：网关分组 + 模型发现（调 `/v1/models`）+ 一键导入 + 用途程序识别 + 限流

## 双库分离（核心心智）

| 库 | 存什么 | 连接 |
|---|---|---|
| **平台库**（PG） | 会话/消息/审计/元数据/配置/知识库 | `config/application.yml` 的 postgres 段 |
| **业务库**（StarRocks/MySQL/PG） | 用户真正要查的数据 | `execute_sql` 按 datasource_id 动态连 |

元数据同步、审计、会话历史写平台库；问数结果从业务库捞。

## 快速开始

```bash
# 1. 配置 postgres + redis（复制 config/application-dev.yml.example 为 application-dev.yml 填值）
# 2. 装依赖
pip install -r requirements.txt

# 3. 启动
python3 -m uvicorn src.main:app --reload --port 8000

# 4. 首次配 LLM（模型/密钥不在 yml，存数据库）
#    启动后进 admin 后台（顶部「⚙ 设置」→ 模型）或 PUT /api/admin/llm-config

# 5. 配数据源 + 勾选参与问数的表（管理后台 → 数据源）

# 6. 打开 http://127.0.0.1:8000 问数
```

接口文档：http://127.0.0.1:8000/docs

## 配置说明

- **app / redis / postgres**：`config/application.yml` + profile（`application-dev.yml`，本地填值、不入库）
- **LLM 模型 / 密钥 / base_url / 协议 / 限流**：数据库 `llm_config` 表（**不进 yml**），admin 后台热更新。一行 = 一个模型，用途 `purposes` 多选（analysis/embedding/attribution），启用互斥

## 管理后台

顶部「⚙ 设置」进，子标签：

- **数据源**：业务库连接 + 元数据反向同步 + 白名单勾选（DBeaver 式 源>库>表>字段）
- **提示词**：default 场景 system prompt（热更新）
- **名称字典**：别名→标准名纠错
- **业务规则**：全局/表级人工口径（进 prompt 或附表）
- **SQL 模板**：复杂查询样板（同比/环比/行转列）
- **知识库**：TXT/MD/CSV/docx/xlsx 上传，分段向量入库
- **模型**：网关分组 + 🔍发现模型 + 一键导入 + 用途识别 + 限流

顶部「📊 统计」：成功率 / 平均耗时 / SQL 次数 / trace 详情（全链路复盘）

## 目录结构

```
src/
  core/          编排：orchestrator / agent_loop / session / audit / prompt_store(skill 装配)
  llm/           LLM 服务（双协议 openai/anthropic + 限流 + 重试）
  tools/         Agent 工具 + registry（schema 重建 / 参数强转 / 熔断）
  storage/       平台库 ORM（models.py 是表结构单一事实源）+ pg/redis 客户端
  datasource/    业务数据源管理 + 元数据反向同步
  knowledge/     知识库解析 + 向量存储
  web/routes/    对话侧（ask/session/result）+ admin_* CRUD
config/          application.yml + profile（application-dev.yml 本地配置，不入库）
static/          前端单页（index.html）
```
