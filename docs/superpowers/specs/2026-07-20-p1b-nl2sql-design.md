# P1b NL2SQL 执行链路 设计

> 定位：P1「数据查询」第二段——**核心查数**。把 P1a 的地基（数据源/元数据/连接管家）变成真能力：用户问句 → LLM 选表 → 生成 SQL → 执行 → 结果旁路 → 摘要回灌 → 最终答案。
> 上游：[`2026-07-18-p1a-datasource-metadata-design.md`](./2026-07-18-p1a-datasource-metadata-design.md)（数据源/元数据/enabled）、总 spec 第 6.1/6.5/7.3 章。

## 1. 背景与目标

P1a 给了数据源、元数据、连接管家（DataSourceManager）、配置口径，但 `execute_sql` 还是 P0 的 stub——查不到真数据。P1b 把两个工具做实：

- **`query_metadata`**：LLM 用它看「有哪些表能查」（只看 `enabled=True` 的表，即配置页勾选的白名单），选出本次要查的表。
- **`execute_sql`**：LLM 基于选中表的 schema 生成 SQL → 在业务库执行 → 全量结果旁路（result_id）→ 只把摘要回灌 LLM → LLM 基于摘要给最终答案。

**目标**：一条「问句 → 真实数据答案」的完整链路跑通，带错误自愈（SQL 失败回灌 LLM 重试）。

## 2. 范围

**含**：
- `tools/metadata.py`：`query_metadata` 工具——读 `metadata_tables` where `enabled=True`，返回表名 + 中文注释（+ 字段名/注释），供 LLM 选表。
- `tools/sql_engine.py`：`execute_sql` 工具——LLM 给 SQL → 执行 → result 旁路 → 摘要回灌。
- result 旁路：执行结果全量存 `query_results`（PG）+ Redis（TTL），分配 `result_id`；回灌 LLM 的是摘要（行数/列名/前 5 行）。
- 错误自愈：SQL 执行失败 → 错误信息作为 tool result 回灌 LLM → LLM 修正重试（loop 已支持，6.1）。
- 注册两个工具到 `default_registry`。

**不含 / 推迟**：
- ❌ **SQL 安全护栏（spec 7.6）**——用户决定**先不做**（DDL/DML 拦截、超时、行数限制、危险函数黑名单全部推迟）。⚠️ **已知风险**：LLM 可能生成 `DROP/DELETE` 等危险语句直接执行，**生产前必须补**（见第 9 章）。本 spec 留 `validate_sql(sql)` 占位（pass-through），护栏后补在那一处。
- ❌ SQL 模板应用（命中模板优先）—— P2（P1a 已留 `sql_templates` 表 + CRUD 口）。
- ❌ 多表关联 JOIN 的智能推导（`table_relations` 消费）—— P1c。本段 LLM 可用 `table_relations` 已配的关联生成 JOIN，但不做"无配置推导"。
- ❌ 字段展示规则渲染（`display_columns`）—— P1c。
- ❌ 数据权限（spec 7.7，本期暂缓）。

## 3. 关键决策（ADR）

1. **两步工具链路**（spec 7.3）：`query_metadata`（选表，省 token）→ `execute_sql`（生成+执行）。不一步发所有表 schema——enabled 表多了 token 爆；两步让 LLM 先选 ≤N 表再只发选中表 schema。
2. **白名单衔接配置页**：`query_metadata` 只返回 `metadata_tables.enabled=True` 的表。用户在配置页勾选的表才参与问数。
3. **result 旁路 PG + Redis**（spec 6.5）：全量结果存 `query_results`（PG，审计/持久）+ Redis（`result:{id}` TTL，快速取全量）。回灌 LLM 只给摘要（行数/列名/前 5 行/关键聚合），避免大结果 token 爆。最终答案用 `result_id` 引用全量，前端按 `result_id` 渲染表格。
4. **错误自愈**（spec 6.1）：execute_sql 执行失败（语法错/表字段错）→ 错误信息作为 tool result 回灌 → LLM 看到错误修正 SQL 重试。loop 的 ReAct 天然支持（工具返回错误 summary，LLM 下一轮修正）。
5. **SQL 护栏推迟**（用户决定）：`execute_sql` 内 `validate_sql(sql)` 占位 pass-through。**生产前必修**。
6. **数据源上下文**：工具执行需要知道查哪个数据源。本段从会话/请求带 `datasource_id`（默认单源场景：取第一个 enabled 数据源；多源时 P1c 再做会话绑源）。

## 4. 模块结构

```
src/
├── tools/
│   ├── metadata.py        # 新建：query_metadata 工具（读 enabled 表元数据）
│   └── sql_engine.py      # 新建：execute_sql 工具（执行 + result 旁路 + 自愈友好错误）
├── storage/
│   └── query_results.py   # 新建：result 旁路存取（save/query by result_id，PG + Redis）
├── tools/builtins.py      # 修改：default_registry 注册 query_metadata/execute_sql
└── core/types.py          # LoopContext 可能加 datasource_id（多源场景，本期默认单源可省）
```

## 5. NL2SQL 链路（一次问句的流程）

1. 用户问「上月发电量多少」→ orchestrator → agent_loop。
2. LLM 调 `query_metadata` → 工具读 `metadata_tables where enabled=True`，返回 `[{table_name, table_comment, columns:[{name,comment,type}]}]`。
3. LLM 选出 `fact_power`，调 `execute_sql(sql="SELECT sum(kwh) FROM fact_power WHERE month='2026-06'")`。
4. `execute_sql`：
   - `validate_sql(sql)` —— 占位 pass-through（护栏推迟）。
   - 取数据源 engine（DataSourceManager.get_engine）。
   - 执行 SQL → 拿全量结果（columns + rows）。
   - result 旁路：`save_result(columns, rows)` → 存 query_results（PG）+ Redis TTL，返回 `result_id`。
   - 回灌 LLM 摘要：`{result_id, rows: N, columns: [...], preview: 前5行, summary: "共 N 行"}`。
5. LLM 基于摘要生成最终答案文本（「上月发电量 X 度」），引用 `result_id`。
6. loop 结束 → done，answer 含文本 + `result_id`（前端按 result_id 渲染表格）。

**自愈**：步骤 4 执行失败（SQL 语法错）→ execute_sql 返回 `{error: "SQL 错误: ..."}`（不抛异常，作为 tool result）→ LLM 看到错误，下一轮调 execute_sql 修正版 → 成功。

## 6. result 旁路（query_results.py）

```python
async def save_result(session_id, columns, rows, datasource_id) -> str:
    """全量结果存 PG query_results + Redis，返回 result_id。"""
    result_id = uuid
    # PG（审计/持久）
    async with AsyncSessionFactory() as s:
        s.add(QueryResult(result_id=result_id, session_id=session_id,
                          columns_json=json, rows_json=json, total=len(rows)))
        await s.commit()
    # Redis（TTL，快速取全量）
    await redis.setex(f"result:{result_id}", TTL, rows_json)
    return result_id

async def get_result(result_id) -> dict | None:
    """取全量结果：Redis 优先，miss 回 PG。"""
    # Redis miss → PG query_results
    ...
```
- PG `query_results` 表 P0a 已建（Task 1 可按需补 `datasource_id` 字段）。
- Redis TTL（如 1 小时）；PG 永久（审计）。
- 前端 `GET /api/result/{result_id}` 取全量渲染表格（P1b 加这个只读端点）。

## 7. 错误处理 + 自愈

- execute_sql 执行异常（SQL 错误/连接错）：**捕获，返回 `{error: 具体信息}` 作为 tool result**（不抛异常打断 loop）。LLM 看到错误，ReAct 下一轮修正重试。
- 连续失败：loop 的 `max_turns` / 重复调用护栏兜底（P0b 已有）。
- result 旁路写失败：记日志，execute_sql 仍返回摘要（旁路是非关键路径，不影响主链路）。

## 8. API 新增

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/result/{result_id}` | 取全量结果（前端渲染表格用）|

（其余走工具内部，不经 HTTP。）

## 9. ⚠️ 已知风险（SQL 护栏推迟）

用户决定 P1b 先不做 SQL 安全护栏。**这意味着 LLM 生成的任意 SQL（含 DROP/DELETE/ALTER）会直接在业务库执行。** 缓解：
- LLM system prompt 强引导「只生成 SELECT 查询」。
- 业务库账号最小权限（只读账号最佳——建议用户配 StarRocks 只读账号）。
- `validate_sql(sql)` 占位已留，生产前补：sqlglot 解析拦 DDL/DML + 超时 + 行数限制。

**生产上线前必须补护栏。** 记入技术债。

## 10. 测试策略

- `query_metadata`：mock 元数据（enabled/disabled 表），断言只返回 enabled 表 + 字段。
- `execute_sql`：mock engine 执行返回固定 rows，断言 result 旁路写入 + 摘要正确。
- 自愈：mock engine 第一次抛错、第二次成功，断言 LLM 看到错误 summary（loop 层测，或 execute_sql 单测错误返回）。
- result 旁路：save/get round-trip（sqlite PG + fake Redis 或真 Redis）。
- 集成测（真 StarRocks）：可选，`@pytest.mark.integration`，CI 跳过。

## 11. 新增依赖

无（SQLAlchemy/Redis/asyncpg 已有；sqlglot 留到补护栏时加）。
