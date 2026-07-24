# Claude Code 使用洞察报告

> 112 个会话 · 77 个已分析 · 1240 小时 · 110 次提交 · 2026-06-22 至 2026-07-23

---

## 一、你做了什么

| 领域 | 会话数 | 内容 |
|---|---|---|
| **NL2SQL AI 问数平台** | 16 | 从零设计实现：FastAPI + 自主 ReAct + 双协议 LLM + 会话状态机 + SSE 流式 + 限流加固 + 知识库 pgvector + 归因 + 名称纠错 + SQL 模板 + 业务规则分层 + admin 后台 UI |
| **Spring AI Agent 平台** | 5 | Java/Spring Boot 重构为 Spring AI 原生：工具调用/记忆/RAG/MCP + JWT→网关透传简化 |
| **数据工程 & ETL** | 7 | 60 万行 MySQL 导出（API 富化+缓存+并发+断点续传）、飞书组织架构爬取（982 人/9 部门）、会议数据标准化 MySQL 迁移 |
| **基础设施** | 7 | Docker PG+pgvector、青龙头文件下载修复、Claude Code 配置管理 |
| **技术文档** | 6 | 代码库分析、技术方案编辑、HTML 原型、会议纪要 |

---

## 二、做得好的

- **端到端平台构建**：NL2SQL 从需求到上线（200+ 测试绿），坚持真库验证而非 mock
- **大规模数据管线**：60 万行导出 + 飞书爬取，工程扎实（缓存/并发/断点续传）
- **Java 框架重构**：6 阶段全量重构，79 测试绿 + 真模型冒烟，砍掉过度设计（JWT→网关透传）

---

## 三、卡在哪（3 大摩擦）

### 1. mock 测试通过、真库炸（最大痛点）
6+ 个会话中，mock 全绿但真 PG/StarRocks 上 500 错误（asyncpg 连接、缺列、加密 key 缺失），多轮紧急 debug。**根因：mock fixture 不覆盖真库 schema 元数据。**

### 2. 初始架构选错
过度设计（JWT auth vs 网关透传）、SQL 模板方向错（SQL 模板 vs 知识库）、弹框尺寸反复迭代（640→820→1100→min-height 75vh）。**根因：动手前没对齐设计。**

### 3. 未授权改动 + 工作区污染
Claude 改 DB 配置/.env 不问、引用前序项目、git 清理时误还原前端 dist、残留构建产物。**根因：边界不明确。**

---

## 四、建议

### CLAUDE.md 该加的规则

| 规则 | 内容 |
|---|---|
| **验证策略** | 数据库相关代码**必须连真库验证**，不许只靠 mock 就说完成 |
| **边界** | DB 配置 / .env / 加密设置**不许改，先问** |
| **最小改动** | 只做要求的，不加戏、不过度设计、不"顺手优化" |
| **UI 标准** | 弹框 ≥ 800px、输入框 ≥ 400px、中文标签、flex-basis 检查 |
| **技术栈** | Python FastAPI asyncpg SQLAlchemy / PG+pgvector+StarRocks / 不引用前序项目 |
| **SQL 约定** | 查询前验证列名存在、中文注释按需（不加戏）、StarRocks/PG 方言测试 |

### 推荐功能

1. **MCP 连数据库**（解决最大痛点）
   ```bash
   claude mcp add postgres -- npx @modelcontextprotocol/server-postgres "postgresql://user:pass@localhost:5432/nl2sql"
   ```
   Claude 直连真库验证 SQL/schema，不再靠 mock。

2. **Hooks**（拦截回归）
   ```json
   // .claude/settings.json
   { "hooks": { "PostToolUse": [{ "matcher": "Edit|Write", "command": "python -m pytest tests/ -x -q 2>&1 | tail -5" }] } }
   ```

3. **Custom Skills**（复用规则）
   - `/sql`：验证列名 + 方言 + 中文注释规则
   - `/ui`：弹框 ≥800px + flex 检查 + 中文标签

### 工作方式优化

- **真库验证**：每次 DB 任务，先连真库跑一遍，别只 mock
- **拆小会话**：一次只做一个功能（大会话摩擦翻倍，47 小时的大会话回归最多）
- **先设计后写码**：复杂功能先出 ≤200 字方案，你批准再写

---

## 五、数据一览

| 指标 | 值 |
|---|---|
| 总会话 | 112 |
| 分析会话 | 77 |
| 总时长 | 1240 小时 |
| 提交数 | 110 |
| 满意 | 122 |
| 不满 | 49 |
| 沮丧 | 53 |
| buggy code 事件 | 72 |
| 错误方向 | 52 |
| 过度改动 | 24 |

**满意率 ~52%**——你是个高标准的用户，对 mock 测试/过度设计/未授权改动容忍度很低。

---

## 六、趣事

Claude 多次猜错 Claude Code 自己的上下文窗口/压缩机制（百分比 vs 绝对阈值），用户反复纠正——AI 不懂自己的宿主应用架构 😅
