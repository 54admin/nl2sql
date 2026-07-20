# 配置页设计（集成对话框 + 表级参与问数）

> 定位：P1a 数据源/元数据 API 已齐（Task 1-9），本设计补一个**配置页 UI**，集成到现有 `static/index.html` 对话框，替代 Task 10 的 curl smoke——让用户在网页上配数据源、同步、勾选哪些表参与问数。同时给 P1a 补一个 `metadata_tables.enabled`（表级白名单）。
> 上游：[`2026-07-18-p1a-datasource-metadata-design.md`](./2026-07-18-p1a-datasource-metadata-design.md)（P1a API）。

## 1. 背景与目标

P1a 把数据源管理、元数据同步、配置口径都做成了 HTTP API，但没有 UI——只能 curl。用户要直接在网页上：
- 配多个数据源（增删改、测连接、同步表结构）
- 看同步进来的表
- **勾选哪些表参与问数**（数仓表多，只有部分业务表该参与；全参与会让 LLM 选表混乱）

所以做配置页，集成到对话框页面（Tab 切换），并补表级参与开关。

## 2. 范围

**含**：
- `static/index.html` 顶部加 Tab（对话 / 配置）。
- 配置 Tab：左侧数据源列表（多源 CRUD + 测试连接 + 同步），右侧选中源的表列表 + 参与勾选。
- P1a 扩展：`metadata_tables` 加 `enabled` 字段（默认 False = 不参与问数）。
- 元数据读 API 返回 `enabled`；新增「改表 enabled」接口。
- 同步保护：已有表的 `enabled` 勾选状态不被同步冲掉（同 manual 注释一样保护）。

**不含**（第二批 / 后续）：
- ❌ 业务规则、SQL 模板的录入 UI（API 已有，UI 后补）。
- ❌ 字段展示规则、逻辑关系录入 UI（P1c）。
- ❌ 鉴权（admin 接口本期无鉴权，沿用 P1a）。

## 3. 关键决策（ADR）

1. **Tab 切换集成**：index.html 顶部加「对话/配置」Tab，一个页面两用，不跳转。
2. **表级白名单**：`metadata_tables.enabled` 默认 **False**（同步新表不参与），用户手动勾选要问数的表。理由：StarRocks 数仓表多，只有部分是业务表，全参与让 LLM 选表混乱；白名单让用户精确控制。
3. **同步不碰 enabled**：`sync_metadata` 只更新 `comment`/`type`，**不碰 `enabled`**（也不碰 `is_primary`/`role_tag`，已是现状）。新表 INSERT 用 ORM default `enabled=False`；已有表的勾选状态保留。所以 `sync_metadata` 实现不用改，只靠 ORM default + 不碰 enabled。
4. **数据源多源**：配置页左侧数据源列表，点选切换右侧表。datasource CRUD 已支持多源（P1a）。
5. **纯前端 fetch**：配置页用浏览器 fetch 调 P1a 的 admin API，不新增后端组件。

## 4. UI 结构

```
[ 对话 ]  [ 配置 ]                    ← 顶部 Tab
─────────────────────────────────────────────
 数据源                          │ 表（{选中源名}）
 [+ 新建数据源]                  │ [同步元数据] 刷新
 ┌──────────────────────────┐   │ ┌────────────────────────────────┐
 │● 风电数仓                 │   │ │☑ fact_power    发电量事实表     │
 │  type=starrocks           │   │ │☐ fact_order    工单事实表       │
 │  [测试连接][编辑][删除]    │   │ │☑ dim_station   场站维度         │
 │  [同步元数据]              │   │ │  └ 字段：kwh(度数) station_id   │
 ├──────────────────────────┤   │ │☐ ods_raw       （被 sync_scope  │
 │○ 运营库                   │   │ │                过滤掉的不显示）  │
 └──────────────────────────┘   │ └────────────────────────────────┘
```

- 左：数据源列表（名称/type/连接信息），选中高亮；每源有「测试连接」「编辑」「删除」「同步元数据」；顶部「+新建」。
- 右：选中源的表清单（表名/注释/参与勾选框）；勾选即时保存（调 PUT enabled）；点表可展开看字段（只读）。
- 「同步元数据」按钮调 `POST /datasources/{id}/sync`，同步后右侧表刷新。
- 新建/编辑数据源：弹层表单（name/host/port/db/user/password/sync_scope），密码框；提交调 POST/PUT。

## 5. P1a 扩展：metadata_tables.enabled

```sql
metadata_tables(
  ... 现有字段 ...,
  enabled BOOL DEFAULT FALSE     -- 是否参与问数（白名单）。P1a 扩展
)
```

- ORM `MetadataTable` 加 `enabled: Mapped[bool] = mapped_column(Boolean, default=False)`。
- P1a 未上线、无生产数据：直接改 ORM，`Base.metadata.create_all` 新建含字段；若 PG 已有旧 `metadata_tables` 表（之前跑过 sync），需 `ALTER TABLE` 或 drop 重建（手动，spec 注明）。
- `sync_metadata` **不改**：新表 INSERT 走 ORM default（enabled=False）；已有表 UPDATE 只碰 comment/type，不碰 enabled。

## 6. API 改动（admin_metadata.py）

| 方法 | 路径 | 改动 |
|------|------|------|
| GET | `/api/admin/metadata?datasource_id=` | 返回每张表加 `enabled` 字段 |
| PUT | `/api/admin/metadata/tables/{table_id}` | **新增**：`{enabled: bool}` → `{ok}`；改单表参与开关 |
| （GET 还可加 `?enabled_only=true`） | 同上 | 可选：只返回参与的表，给 P1b query_metadata 用 |

其余 admin API（datasource CRUD/test/sync、table-relations/business-rules/sql-templates CRUD）**不动**，配置页直接 fetch 调。

## 7. 交互流程（用户视角）

1. 进配置 Tab → 左侧「+新建数据源」填 StarRocks 连接 → 保存。
2. 点「测试连接」验通。
3. 点「同步元数据」→ 右侧出现同步进来的表（默认全未勾选）。
4. 勾选要参与问数的表（fact_power、dim_station…）→ 即时保存。
5. 回对话 Tab 问数 → P1b（未做）的 query_metadata 只从勾选的表里选。

## 8. 错误处理

- 测试连接失败：按钮旁红字提示（400 错误信息）。
- 同步失败：右侧表区提示错误，保留旧表清单。
- 保存勾选失败：勾选框回滚 + 提示。
- 新建/编辑数据源校验：必填项（name/host/port/db/user/password）前端校验。

## 9. 测试策略

- 后端：`metadata_tables.enabled` ORM round-trip；GET metadata 返回 enabled；PUT enabled 接口；同步后新表 enabled=False、已有表 enabled 不被重置（扩 test_metadata_sync）。
- 前端：index.html 配置 Tab 手动验（无自动化框架，靠浏览器点）。
