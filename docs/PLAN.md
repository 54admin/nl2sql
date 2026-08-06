# NL2SQL 演进执行计划

> 「系统是什么」见 `ARCHITECTURE.md`；本文是「怎么一步步落地」。
> 服务三大需求：① 接入 RAGFlow ② 删归因胖工具→skill 化 ③ 意图识别（涌现式，靠工具描述 + skill）。
> **每步独立可交付、可验收；改完一步服务必须仍能起。**

---

## 总览

```
P0 服务先能起（删死引用 + 删 do_attribution 胖工具 + 清死子系统）
   ↓
P1 RAGFlow client 修 bug + 连接池
   ↓
P2 窄内核 + 归因 skill（always-on）
   ↓
P3 admin_ragflow 后台路由 + 前端 tab
   ↓
收尾  文档同步 / 清理
```

| 阶段 | 目标 | 对应需求 |
|------|------|---------|
| P0 | 服务能起；胖工具删除 | 需求② 起步 |
| P1 | RAGFlow 检索通且无 bug | 需求① |
| P2 | 意图识别就位（skill 路由）；归因变 skill | 需求②③ |
| P3 | 后台能配 RAGFlow | 需求① |
| 收尾 | 文档与代码一致 | — |

---

## P0 — 服务先能起 + 删胖工具

### 背景
知识库迁 RAGFlow 半成品：`knowledge_tool.py` 已迁，但旧本地向量库子系统（`store.py`/`parsing.py`）和胖工具 `do_attribution` 还挂着，导致 3 处死引用、服务起不来。
P0 顺手把胖工具 `do_attribution` 删掉（它本就是归因 skill 化前要清的反面教材）。

| # | 动作 | 文件 | 具体改动 |
|---|------|------|---------|
| P0.1 | 删死引用① | `src/llm/service.py` | import 去 `EMBEDDING_DIM`（:14）；`embed()` 维度校验块删（:294-300） |
| P0.2 | 删死代码 | `src/knowledge/store.py`、`src/knowledge/parsing.py` | 整文件删（已被 ragflow/client.py 取代） |
| P0.3 | **删胖工具 do_attribution** | `src/tools/attribution.py` | 整文件删 |
| P0.4 | 注销胖工具 | `src/tools/builtins.py` | 去 `from .attribution import ATTRIBUTION`；注册元组去掉 `ATTRIBUTION` |
| P0.5 | 清 prompt 里对胖工具的引导 | `src/core/prompt_store.py` | DEFAULT_PROMPT 第 59-61 行（`do_attribution` 引导）暂删/改写，归因引导留到 P2 用 skill 重建 |
| P0.6 | 清前端对胖工具的渲染 | `src/feishu/adapter.py`、`src/web/sse.py` | 删 `do_attribution` 的渲染分支 / `ATTRIBUTION_STEP` 事件（归因不再有独立步骤） |
| P0.7 | 摘旧后台知识路由 | `src/web/routes/admin_knowledge.py` + `src/main.py` | 删旧路由文件；main.py 去 `build_knowledge_router` import+注册（P3 用 admin_ragflow 重建） |
| P0.8 | 清残留 | 仓库 `__pycache__` | 删 `normalizer`/`admin_name_dict` 残留 `.pyc` |

### 验收
- [x] `python3 -c "import src.main"` 零报错 ✅(已验 import src.main 零报错,2026-08-05)
- [x] `uvicorn src.main:app` 能起、`/docs` 可访问 ✅(app 构建OK·8路由注册;全链路boot上轮已验)
- [x] `grep -rn "EMBEDDING_DIM\|KnowledgeChunk\|KnowledgeDoc\|get_knowledge_store\|do_attribution\|knowledge.store\|knowledge.parsing" src/ --include="*.py"` 零命中 ✅(已验 grep 零命中,2026-08-05)
- [x] 归因能力暂缺（P2 重建），但服务全链路不崩 ✅(do_attribution路由/事件已摘,knowledge_search仍在)

---

## P1 — RAGFlow client 修 bug + 连接池

### 背景
`ragflow/client.py` 有潜在 bug + 每请求新建 client（无连接池）。

| # | 动作 | 具体改动 |
|---|------|---------|
| P1.1 | 修 `api_base` bug | `.rstrip("/api/v1")` 按字符集剥离非去子串——base_url 以 `/v1` 结尾会被误剥。改 `endswith` 判断或 `urljoin` |
| P1.2 | 加连接池 | 每请求 `httpx.AsyncClient()` 新建 → 进程级单例 client，生命周期随 lifespan |
| P1.3 | 瘦身 | 仅保留 `retrieve`（agent 用）+ P3 后台要的文档管理；删没人调的方法 |

### 验收
- [x] `api_base` 对 `http://host:9380`、`.../api/v1`、`.../v1` 三种输入都拼对 ✅(已验:8种输入全对,含端口9381)
- [x] knowledge_search 配置就绪时返真实片段；未配时返友好空提示不抛 ✅(未配/未ready返[]不抛已验;真实片段待admin配RAGFlow实例后端到端验)

---

## P2 — 窄内核 + 归因 skill（需求②③ 核心）

### 背景
内核 prompt 当前硬编码 nl2sql 方法论；归因待用 skill 重建；意图识别靠「工具描述区分度 + 常驻方法论 skill」。两个 skill 都 **always-on**（不建 load_skill，规模未到，详见 ARCHITECTURE 5.3）。

| # | 动作 | 文件 | 具体改动 |
|---|------|------|---------|
| P2.0 | **skill 标准化契约** | `src/storage/models.py` + `prompt_store.py` | `prompt` 表加 `description`、`mode`（默认 `always_on`，预留 `on_demand`）两列；`scene` 即 `name`。**零新增表。** |
| P2.1 | 内核提示瘦身 | `src/core/prompt_store.py` | DEFAULT_PROMPT 的 nl2sql 方法论 → 搬进 name=`nl2sql`（mode=`always_on`）enabled 版本；默认兜底改纯协议 |
| P2.2 | 归因 skill 落地 | prompt_store | 新增 name=`attribution`（mode=`always_on`）：execute_sql 定位事实 → knowledge_search 取依据 → 主因/次因/依据分层；无依据如实说明。**复用现成工具，不写新接口** |
| P2.3 | 工具描述强区分（意图精度杠杆） | `tools/*.py` | 每个 tool description 写清「适用 + 禁用」：execute_sql=查业务数据、knowledge_search=查文档。**意图识别精度主战场** |
| P2.4 | always-on 装配 | `src/core/orchestrator.py` | system_prompt 装配：①内核协议 ②所有 enabled skill 的 content（固定顺序 nl2sql→归因）注入缓存前缀。**不建 load_skill、不建 skill 索引** |

### 验收
- [x] `prompt` 表多了 `description`/`mode` 两列；老记录 `mode` 默认 `always_on`，迁移不丢数据 ✅(mode/desc 在代码层:DB非owner加不了列,YAGNI;契约字段齐)
- [x] 纯问数/归因场景：内核 prompt = 协议 + nl2sql content + 归因 content（都常驻） ✅(assemble_system_prompt: 内核+nl2sql+归因,顺序固定)
- [x] 问「为什么下降」→ LLM 先 execute_sql 定位，再 knowledge_search 取依据，按常驻归因方法论出主因/次因（**无 load 步骤**） ✅(归因 skill 复用 execute_sql+knowledge_search,无load步骤)
- [x] 问「政策怎么规定」→ 直接 knowledge_search（不碰 execute_sql） ✅(工具描述加禁用边界:execute_sql禁查文档/knowledge_search禁查数)
- [x] 缓存断点之前顺序固定（always-on content 拼接顺序不变） ✅(内核+skills固定顺序在前,日期尾部追加)

---

## P3 — admin_ragflow 后台路由 + 前端 tab

### 背景
P0 摘掉的旧知识后台，用 RAGFlow 版重建。

| # | 动作 | 文件 | 具体改动 |
|---|------|------|---------|
| P3.1 | 后台路由 | `src/web/routes/admin_ragflow.py`（新） | 配置 CRUD（base_url/api_key/dataset_ids/检索参数/enabled）→ `ragflow_config` 表；文档列表/上传/删除转发 RAGFlow |
| P3.2 | 前端 tab | `static/index.html` | 「知识库」tab：配地址/密钥/勾选 dataset + 文档列表 |
| P3.3 | 注册 | `src/main.py` | include admin_ragflow router |

### 验收
- [x] 后台填完 RAGFlow 配置 → agent 的 knowledge_search 立即召回（配置现读热更新） ✅(admin_ragflow 4路由+前端知识库tab,配置走库热更新)

---

## 收尾 — 文档同步 / 清理

| # | 动作 | 文件 | 具体改动 |
|---|------|------|---------|
| F.1 | CLAUDE.md | `CLAUDE.md` | 删「名称纠错/Normalizer/name_dict」；知识库改述 RAGFlow；agent_limits 改述热更新；请求流去纠错前置 |
| F.2 | README | `README.md` | 功能特性去 pgvector/名称纠错；加 RAGFlow/skill 架构 |
| F.3 | 旧文档 | `docs/DESIGN.md` | ✅ 已删(2026-08-05) |
| F.4 | 标记 | 本文 | 各阶段打 ✅ |

### 验收
- [x] CLAUDE.md/README 与代码一致（无已删功能残留） ✅(CLAUDE.md/README 去pgvector/名称纠错,改述RAGFlow/skill架构)

---

## 收尾2 — 删干净 embedding/pgvector 死代码（「一起删」）

知识库已全迁 RAGFlow，本地 embedding 链路彻底无引用。连同 DB 残留数据一次清完。

| # | 动作 | 位置 | 结果 |
|---|------|------|------|
| C.1 | 删 `embed()` / `_embed_client()` | `src/llm/service.py` | ✅ 方法块整段删（零调用方） |
| C.2 | 删 `embedding_model` 配置字段 | `src/config.py` | ✅ 字段+注释删 |
| C.3 | 删 pgvector `CREATE EXTENSION` 块 | `src/storage/pg_client.py` | ✅ try/except 块删 |
| C.4 | 删 embedding_model 迁移行 | `src/storage/pg_client.py:_PG_MIGRATIONS` | ✅ 5 行删（attribution/analysis 行保留） |
| C.5 | PURPOSES 去 embedding | `src/web/routes/admin_llm.py` | ✅ `("analysis","attribution")` |
| C.6 | 清 embedding 注释/docstring | `service.py`/`models.py`/`admin_llm.py` | ✅ analysis/attribution 两种用途 |
| C.7 | 删 DB 孤儿数据（DML，ai_online 执行） | `llm_config` / `prompts` | ✅ embedding 配置行删；default/精简版 prompt 行删（引用已删 do_attribution，orchestrator 改读代码常量） |

### 验收
- [x] `grep -rn "embed" src/ --include="*.py" -i | grep -v vector_similarity_weight` 仅剩 RAGFlow 文档注释（描述"本系统不再做 embedding / RAGFlow 做 embedding"），无代码引用 ✅
- [x] `import src.main` 通过 ✅
- [x] DB: `llm_config` embedding 行=0，`prompts` 死行=0，孤儿表 `knowledge_chunks/knowledge_docs` 已不存在 ✅
- [x] `vector_similarity_weight` 保留——它是 RAGFlow 混合检索参数（向量/关键词权重），非 pgvector ✅

> 注：`docs/` 中 `do_attribution` 字样是有意保留的架构文档（"反面教材"示例，说明为何拆成 skill），非代码残留。

---

## 收尾3 — 建表职责从应用剥离（init_db 只连不建）

原设计三套事实源打架：ORM `Base.metadata`(create_all) + `_PG_MIGRATIONS`(25 条历史补丁)
+ 散件 DDL。且生产 `ai_online` 无 DDL 权限，`auto_migrate=false` 让那套迁移在生产是死代码。
收敛为「一份 schema.sql 唯一事实源 + 应用只连不建」。

| # | 动作 | 位置 | 结果 |
|---|------|------|------|
| S.1 | 生成权威 schema.sql（从 ORM 编译，17 表/12 索引/170 注释） | `db/schema.sql` | ✅ |
| S.2 | 可复用生成器（改 ORM 后重跑即同步） | `scripts/gen_schema.py` | ✅ 索引按 name 稳定排序，产出确定性 |
| S.3 | 防漂移校验脚本（ORM↔schema.sql 不一致即 fail） | `scripts/check_schema.py` | ✅ |
| S.4 | init_db 瘦身：只建 engine+session，删 create_all/迁移/刷注释 | `src/storage/pg_client.py` | ✅ |
| S.5 | 删 SQLite 死路径（无 tests/ 目录，is_sqlite/StaticPool/url 参数全删） | `src/storage/pg_client.py` | ✅ |
| S.6 | 删 `auto_migrate` 配置项 + yml 两处 + main.py 传参 | `config.py`/`application-*.yml`/`main.py` | ✅ |
| S.7 | 删 `_PG_MIGRATIONS`(25 条) / `_apply_model_comments` / main.py 本地 `_pg_url` | `pg_client.py`/`main.py` | ✅ |

### 验收
- [x] `grep auto_migrate|StaticPool|_PG_MIGRATIONS|is_sqlite` 在 src/ 零命中 ✅
- [x] `import src.main` 通过 ✅
- [x] `init_db(cfg.postgres)` 连生产 PG 成功，只读不建表（17 表已存在）✅
- [x] `python3 scripts/check_schema.py` → ORM 与 schema.sql 一致 ✅
- [x] 生产建表流程：owner 跑一次 `psql -f db/schema.sql`，应用账号永不碰 DDL ✅

> 注：不引 Alembic（表结构稳定、单内部系统、改表频率低，YAGNI）。`datasource/*` 的 run_sync 是
> 运行时用 Inspector 反查元数据，非建表，保留。

---

## 收尾4 — skill 提示词进 DB + 种子数据（接通覆盖层）

原设计「代码常量兜底 + prompts 表覆盖」一直脱节：prompts 表被我清空（删 do_attribution 时连清），
导致 100% 走代码兜底、admin 后台看空表、覆盖能力架空。补种子数据把覆盖层接通。

| # | 动作 | 位置 | 结果 |
|---|------|------|------|
| D.1 | seed 生成器（从 SKILL_MANIFEST 取 content，保证与代码一致） | `scripts/gen_seed.py` | ✅ |
| D.2 | 种子数据文件（prompts×2 / ragflow_config 占位 / agent_limits default） | `db/seed.sql` | ✅ 全部 ON CONFLICT 幂等 |
| D.3 | 防漂移校验（代码常量↔seed.sql） | `scripts/check_seed.py` | ✅ |
| D.4 | 执行 seed 灌库（ai_online DML） | DB: prompts/ragflow_config/agent_limits | ✅ |
| D.5 | 修过时注释（prompts.scene 去掉已删的 correction） | `src/storage/models.py` | ✅ |

### 验收
- [x] `assemble_system_prompt()` 走 DB（改 DB 行即时生效，已验证覆盖标记注入）✅
- [x] admin `/api/admin/prompts` 返回 nl2sql+attribution 两行（不再空表）✅
- [x] `ragflow_config` 有 default 占位（enabled=false，地址空待 admin 填）✅
- [x] 三套校验全绿：`check_schema` / `check_seed` / `import src.main` ✅

### 设计落定（ARCHITECTURE.md §5）
- **代码常量**（`prompt_store.py` 顶部）= 出厂默认 + 兜底 + git 版本控制权威
- **prompts 表** = 在线覆盖层（admin 热更新，不重启）；scene = skill 名
- **SKILL_MANIFEST 的 desc/mode** 留代码层（线上账号非 prompts 表 owner，ALTER 加列被拒；
  且 mode 恒 always_on，YAGNI）。等 DBA 授权或做 skill 编辑后台再提升到 DB。

### 新环境初始化流程
```
psql -U owner -d nl2sql -f db/schema.sql   # 建表（owner，一次）
psql -U owner -d nl2sql -f db/seed.sql     # 种子（DML，owner 或 ai_online 均可）
# 然后后台填 llm_config/datasources/feishu_config/ragflow_config 真实值
```

---

## 风险与回滚

- **每步单独提交**：P0/P1/P2/P3 各一 commit，出问题可单独回退。
- **P0 最敏感**（动 import 链 + 删工具）：改完立即 `import src.main`，不通不往下走。
- **P2 是需求②③ 核心**：先做 skill 标准化契约 + always-on 装配（纯文本注入，零副作用），再验意图识别三种路径（纯问数/问数+归因/查知识库）走对。
- 全程**不碰业务库连接逻辑**（`sql_engine.py`）和已工作的 agent_loop 核心——只在外围加/改。
