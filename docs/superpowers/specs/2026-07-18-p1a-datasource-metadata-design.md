# P1a 数据源与元数据地基 设计

> 定位：P1「数据查询」三段拆分的第一段——**地基**。只做基础设施（连库、拿元数据、留配置口径），不碰 LLM、不碰 SQL 生成执行。
> 上游 spec：[`2026-07-17-nl2sql-ai-wenshu-design.md`](./2026-07-17-nl2sql-ai-wenshu-design.md) 第 7、8、12 章。
> 后续：P1b 执行链路（query_metadata/execute_sql/SQL 安全/result 旁路存取）依赖本段；P1c 多表关联 + 字段展示规则消费本段留的配置口径。

## 1. 背景与目标

P0 已跑通 ReAct 内核 + HTTP 流式对话，但 `execute_sql` 还是 stub——查不到真实数据。P1 要把真实数据查询能力做出来，范围太大，拆三段：

| 段 | 内容 | 依赖 |
|----|------|------|
| **P1a 地基**（本文档） | 连库 + 元数据同步 + 配置口径表 | 无（纯基础设施，不碰 LLM） |
| P1b 执行链路 | NL2SQL 生成/校验/执行 + SQL 安全 + result 旁路存取 | P1a |
| P1c 增强 | 多表关联 JOIN + 字段展示规则 | P1a 口径表 + P1b |

**P1a 目标**：能配置一个 StarRocks 数据源、把它的表/字段元数据同步进系统库、并留好「逻辑关系」和「业务规则」两个人工录入配置口径，供后续阶段消费。

## 2. 范围

**含**：
- `datasources` 表 + CRUD：连接配置（StarRocks，MySQL 协议），密码 AES 加密存。
- SQLAlchemy 连接池管理：按 datasource 维护 engine。
- 元数据同步：反向拉表名/表注释/字段名/字段注释/类型/逻辑主键标记，写入 `metadata_tables`/`metadata_columns`，保留手写覆盖。
- `table_relations` 表 + CRUD：逻辑主外键关系人工录入口径（用户系统无物理主外键）。
- `business_rules` 表 + CRUD：业务规则人工录入口径（指标口径/查询约束/交互/归因）。
- `sql_templates` 表 + CRUD：SQL 模板人工录入口径（spec 8.2 配置层，应用在 P1b）。
- 元数据手动同步 API（定时同步后置）。

**不含**（YAGNI / 后续阶段）：
- ❌ `query_metadata` / `execute_sql` 工具（P1b）。
- ❌ SQL 生成、校验、执行、安全护栏（P1b）。
- ❌ result 旁路存取（P1b；`query_results` 表 P0a 已建，P1a 不动）。
- ❌ 多表 JOIN 逻辑、字段展示规则渲染（P1c；本段只建口径表，不做消费）。
- ❌ `sql_templates` 的**应用**（命中触发/参数化执行/缺参 ask_user，属 P1b 执行链路）。
- ❌ 数据权限（spec 7.7 本期暂缓）。
- ❌ 元数据定时同步、热更新广播（P2 config_store）。
- ❌ MySQL/PG/Doris 以外的方言适配（P1a 只验证 StarRocks；架构按多数据源设计，不锁死）。

## 3. 关键决策（ADR）

1. **数据源配置走数据库**：`datasources` 表存连接信息（host/port/db/user/密码密文/scope），后台 CRUD，不写 yml、不硬编码（spec 7.1）。区分**两层库**：系统 PG（`application.yml` 的 `nl2sql` 库，存所有系统表）vs 业务 StarRocks（被查的目标，`datasources` 表指向它）。
2. **密码对称加密存 PG**：AES/Fernet 密文存 `datasources.password_enc`，密钥走环境变量 `NL2SQL_DS_KEY`（不入库不入 git）。运行时解密建连接。
3. **连接池用 SQLAlchemy**：spec 7.1 已定。每个 datasource 一个 `AsyncEngine`（StarRocks 兼容 MySQL 协议，走 `mysql+aiomysql`），`dict[datasource_id, AsyncEngine]` 管理，CRUD 时建/销。
4. **元数据同步用 SQLAlchemy Inspector**：`inspector.get_table_names/get_columns/get_table_comment`（底层走 `information_schema`，StarRocks 支持）。少写 SQL、复用 spec 选型。同步范围裁剪为「表名/表注释/字段名/字段注释/类型 + 主键标记」——外键/索引/关系本期不自动拉（StarRocks OLAP 无物理约束，且关系走人工录入）。
5. **逻辑关系人工录入**：系统无物理主外键，全是逻辑主外键。建 `table_relations` 表 + CRUD 作为录入口径，P1c JOIN 消费。`metadata_columns.is_primary` 同理人工标逻辑主键。
6. **`source` 字段保手写**：`metadata_tables/columns` 带 `source`（synced/manual）。反向同步只覆盖 `source=synced` 的行，`manual` 的手写注释/描述不被冲掉。
7. **业务规则留口不消费**：`business_rules` 表 + CRUD 现在只做录入存储，怎么喂给 NL2SQL 是 P1b/P2 的事。通用结构化 KV（category/key/value_json）+ 分类，覆盖 spec 8.3 四类规则。
8. **多数据源架构、单数据源验证**：表和连接池按多 datasource 设计（`datasource_id` 外键贯穿），P1a 只接一个 StarRocks 验证，不锁死单源。

## 4. 模块结构

```
src/
├── datasource/                  # P1a 新增
│   ├── crypto.py                # Fernet 加解密（密钥走环境变量）
│   ├── manager.py               # DataSourceManager：连接池 + datasource CRUD + 元数据读取
│   └── metadata_sync.py         # 同步流程（Inspector 拉 → 写系统库，保留 manual）
├── storage/models.py            # 扩展：+ Datasource/MetadataTable/MetadataColumn/TableRelation/BusinessRule
└── web/routes/
    ├── admin_datasource.py      # /api/admin/datasources CRUD + POST /sync
    ├── admin_metadata.py        # /api/admin/metadata（读元数据，供 P1b query_metadata）+ /api/admin/table-relations CRUD
    ├── admin_business_rules.py  # /api/admin/business-rules CRUD
    └── admin_sql_templates.py   # /api/admin/sql-templates CRUD
```

## 5. 数据模型

ORM 沿用 `storage/models.py` 风格（SQLAlchemy 2.0 `Mapped`/`mapped_column`）。P1a 新表 PK 用 Integer 自增（大量内部行 + 后台列表友好），与现有 String 主键的会话/配置表区分。

```sql
-- 数据源连接配置（密码加密存）
datasources(
  id PK autoincrement,
  name           VARCHAR(64) UNIQUE,    -- 如 "风电数仓"
  type           VARCHAR(32),           -- starrocks（P1a 唯一验证；架构留 mysql/pg）
  host           VARCHAR(128),
  port           INT,
  db_name        VARCHAR(128),          -- StarRocks 目标库
  username       VARCHAR(128),
  password_enc   TEXT,                  -- Fernet 密文
  sync_scope     VARCHAR(256),          -- 同步范围：表名前缀/白名单（逗号分隔），空=全库；过滤系统表
  enabled        BOOL DEFAULT TRUE,
  version        INT DEFAULT 1,
  created_at, updated_at)

-- 元数据·表
metadata_tables(
  id PK autoincrement,
  datasource_id  INT FK→datasources.id,
  table_name     VARCHAR(128),
  table_comment  TEXT,                  -- 自动同步拉；manual 可覆盖
  source         VARCHAR(16),           -- synced / manual
  display_columns_json  TEXT,           -- P1c 展示规则，P1a 建字段不填
  hidden_columns_json   TEXT,           -- P1c 用
  updated_at,
  UNIQUE(datasource_id, table_name))

-- 元数据·字段
metadata_columns(
  id PK autoincrement,
  table_id       INT FK→metadata_tables.id,
  column_name    VARCHAR(128),
  column_comment TEXT,
  data_type      VARCHAR(64),
  is_primary     BOOL DEFAULT FALSE,    -- 逻辑主键，人工标（系统无物理 PK）
  role_tag       VARCHAR(16),           -- core/dim/tech/sensitive（P1c 用，P1a 建字段）
  source         VARCHAR(16),           -- synced / manual
  updated_at,
  UNIQUE(table_id, column_name))

-- 逻辑关系（人工录入，P1c JOIN 消费）
table_relations(
  id PK autoincrement,
  datasource_id  INT FK→datasources.id,
  main_table     VARCHAR(128),          -- 主表名
  rel_table      VARCHAR(128),          -- 关联表名
  join_keys_json TEXT,                  -- [{"main":"a.id","rel":"b.a_id"}]，支持多字段复合
  join_type      VARCHAR(16),           -- inner / left
  business_note  TEXT,                  -- 业务说明，给 LLM 看
  created_at, updated_at)

-- 业务规则（人工录入，后续阶段消费）
business_rules(
  id PK autoincrement,
  category       VARCHAR(32),           -- metric/constraint/interaction/attribution（spec 8.3）
  key            VARCHAR(128),          -- 规则键，如指标名/约束名
  value_json     TEXT,                  -- 规则内容（灵活 JSON）
  enabled        BOOL DEFAULT TRUE,
  version        INT DEFAULT 1,
  created_at, updated_at)

-- SQL 模板（人工录入，P1b 应用：命中→参数化→执行，缺参 ask_user）
sql_templates(
  id PK autoincrement,
  datasource_id  INT FK→datasources.id,   -- 模板绑数据源（不同库 SQL 不同）
  name           VARCHAR(128),
  trigger_keywords  TEXT,                 -- 触发关键词
  trigger_semantics TEXT,                 -- 触发语义描述
  sql_template   TEXT,                    -- SQL 模板，带 :param 占位
  params_json    TEXT,                    -- 参数定义 [{name, default, required, validate}]
  formatters_json TEXT,                   -- 格式化规则（小数位/单位/空值替换）
  enabled        BOOL DEFAULT TRUE,
  version        INT DEFAULT 1,
  created_at, updated_at)
```

**ceiling 标注**（`ponytail:`）：
- `table_relations` 只表达**等值 JOIN**（`main.col = rel.col`）。多对多/非等值关联本期不支持——标 `source=inferred` 让 P1c 的 LLM 推导兜底，或后台手工扩。
- `business_rules` 是通用 KV，不约束 `value_json` 结构（各 category 自定义）。

## 6. 数据源管理（连接池 + 加密 + CRUD）

**`datasource/crypto.py`**：`encrypt(plain)/decrypt(token)`，`Fernet(os.environ["NL2SQL_DS_KEY"])`。密钥缺失启动报错（fail-fast，不留裸奔口子）。

**`datasource/manager.py` — `DataSourceManager`**：
- 持有 `dict[int, AsyncEngine]`（按 datasource_id）+ 系统 PG 会话工厂（注入 `AsyncSessionFactory`）。
- `create/update/delete_datasource(row)`：CRUD `datasources` 表；create/update 后预建对应 engine（解密密码 → `create_async_engine("mysql+pymysql://...")`）并连通性自检；delete 时 dispose engine。
- `get_engine(ds_id) -> AsyncEngine`：懒建 + 缓存（miss 则读表解密建）。
- `test_connection(ds_id)`：`SELECT 1` 探活。

**双库边界**（重要）：`DataSourceManager` 的 engine 连**业务 StarRocks**（查数用）；`AsyncSessionFactory` 连**系统 PG**（存元数据/配置）。两者不混。

## 7. 元数据同步

**`datasource/metadata_sync.py` — `sync_metadata(ds_id)`**：
1. 取 datasource 的 engine（连 StarRocks）。
2. 在同步连接上跑 Inspector（`engine.run_sync` 包 `inspect`）→ `get_table_names()`，按 `sync_scope` 过滤（白名单/前缀 + 排除 `INFORMATION_SCHEMA` 等系统表）。
3. 逐表：`get_columns(table)`（字段名/类型/注释）、`get_table_comment(table)`。
4. 写**系统 PG** `metadata_tables`/`metadata_columns`：
   - 表/字段存在且 `source=synced` → 更新（注释/类型覆盖）。
   - `source=manual` → **跳过**（保留手写）。
   - 新表/新字段 → 插入，`source=synced`。
   - 库里已删的表/字段 → 标记软删或保留（P1a 保留不删，避免误删手写；后续可加清理）。
   - 同步只写 `table_comment`/`column_comment`/`data_type`；`is_primary`/`role_tag` 不碰（人工标，默认 false/空）。
5. 返回同步摘要（新增/更新/跳过计数）。

**触发**：`POST /api/admin/datasources/{id}/sync`（手动）。定时同步后置（P2）。

## 8. 配置口径（table_relations / business_rules / sql_templates CRUD）

三者都是纯 CRUD（存系统 PG），不消费。沿用 `admin_prompts.py` 的 upsert/delete/list 模式：
- `POST/PUT/DELETE/GET /api/admin/table-relations`
- `POST/PUT/DELETE/GET /api/admin/business-rules`
- `POST/PUT/DELETE/GET /api/admin/sql-templates`
- `GET /api/admin/metadata?datasource_id=&table=`（读元数据，P1b query_metadata 将调它）

version bump + updated_at，便于后续 config_store 热更新接入。本期不做内存缓存/广播（直接读 PG，量小）。

## 9. API 清单

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/admin/datasources` | 列数据源 |
| POST | `/api/admin/datasources` | 建数据源（密码明文入参，密文落库） |
| PUT | `/api/admin/datasources/{id}` | 改 |
| DELETE | `/api/admin/datasources/{id}` | 删（dispose engine） |
| POST | `/api/admin/datasources/{id}/test` | 连通性自检 |
| POST | `/api/admin/datasources/{id}/sync` | 触发元数据同步 |
| GET | `/api/admin/metadata` | 读元数据（供 P1b） |
| CRUD | `/api/admin/table-relations` | 逻辑关系录入 |
| CRUD | `/api/admin/business-rules` | 业务规则录入 |
| CRUD | `/api/admin/sql-templates` | SQL 模板录入 |

密码只在 POST/PUT 入参为明文（传输靠 HTTPS/内网），落库前 `encrypt`；GET 永不返回 `password_enc`/明文。

## 10. 错误处理

- 密钥缺失 → 启动期 fail-fast。
- 连接失败（建 engine / test / sync）→ 捕获，返回结构化错误（不 crash 服务），日志带 `datasource_id`。
- 同步单表失败 → 跳过该表继续，汇总失败列表返回（不整批回滚）。
- Inspector 拿不到注释（StarRocks 某些版本 `column COMMENT` 缺失）→ 字段留空，不报错（后续人工补 manual）。

## 11. 测试策略

沿用 P0 模式：`sqlite` 内存库测系统 PG 侧（CRUD/同步写入/source 保留逻辑），业务库侧 mock。
- `crypto`：加解密 round-trip；密钥缺失抛错。
- `DataSourceManager`：CRUD（sqlite）；engine 缓存/销毁（mock create_async_engine）。
- `metadata_sync`：mock Inspector 返回固定表/字段，断言写入 `metadata_tables/columns`；`manual` 行不被覆盖；`sync_scope` 过滤生效。
- `table_relations`/`business_rules`：CRUD 端点（httpx AsyncClient + ASGITransport，沿用 P0b 测试法）。
- 集成测（连真 StarRocks）：可选，标 `@pytest.mark.integration`，CI 默认跳过。

## 12. 新增依赖

- `aiomysql`：StarRocks MySQL 协议 **async** 驱动（async 栈不用同步的 pymysql）。
- `cryptography`：Fernet 加密。
- `SQLAlchemy`：已有（P0 用），复用。

（`sqlglot` 留到 P1b SQL 安全时再加。）
