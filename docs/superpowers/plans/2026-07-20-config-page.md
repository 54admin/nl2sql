# 配置页 Implementation Plan（集成对话框 + 表级参与问数）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development 或 executing-plans。Steps 用 `- [ ]` 跟踪。

**Goal:** index.html 加「对话/配置」Tab，配置页管多数据源（CRUD/测试/同步）+ 勾选哪些表参与问数；P1a 扩展 `metadata_tables.enabled` 白名单字段。

**Architecture:** 前端纯 HTML/JS（fetch 调 P1a admin API），后端小扩展（metadata_tables.enabled + 2 个 API 改动）。同步不碰 enabled（新表 default False，已有表勾选保留）。

**Tech Stack:** FastAPI（已有 admin API）、SQLAlchemy 2.x、原生 HTML/JS（index.html）、pytest（后端 TDD）

**对应设计：** `docs/superpowers/specs/2026-07-20-config-page-design.md`

---

## File Structure

```
src/storage/models.py              # 修改：MetadataTable + enabled
src/web/routes/admin_metadata.py   # 修改：GET 加 enabled + PUT /metadata/tables/{id}
tests/test_datasource_models.py    # 修改：MetadataTable.enabled round-trip
tests/test_routes_admin_metadata.py# 修改：GET 返回 enabled + PUT enabled
tests/test_metadata_sync.py        # 修改：新表 enabled=False、已有不重置
static/index.html                  # 修改：+ Tab + 配置面板 UI/JS
```

---

## Task 1: metadata_tables 加 enabled（ORM + 测试）

**Files:** Modify `src/storage/models.py`（MetadataTable）、`tests/test_datasource_models.py`

- [ ] **Step 1: 写失败测试** —— 在 `tests/test_datasource_models.py` 加：
```python
@pytest.mark.asyncio
async def test_metadata_table_enabled_defaults_false(db):
    async with AsyncSessionFactory() as s:
        ds = Datasource(name="d3", type="starrocks", host="h", port=1,
                        db_name="db", username="u", password_enc="c")
        s.add(ds); await s.flush()
        mt = MetadataTable(datasource_id=ds.id, table_name="t", source="synced")
        s.add(mt); await s.commit()
        assert mt.enabled is False   # 默认不参与问数（白名单）
```

- [ ] **Step 2: 跑红** —— `pytest tests/test_datasource_models.py::test_metadata_table_enabled_defaults_false -v` → FAIL（无 enabled 字段）

- [ ] **Step 3: ORM 加字段** —— `src/storage/models.py` 的 `MetadataTable` 类加（在 `source` 字段后）：
```python
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否参与问数（白名单，默认不参与）
```

- [ ] **Step 4: 跑绿** —— `pytest tests/test_datasource_models.py -v` 全过

- [ ] **Step 5: 迁移说明** —— P1a 未上线、无生产数据。测试用 sqlite memory（create_all 重建含字段）。若 PG 已有旧 `metadata_tables` 表，`Base.metadata.create_all` 不会 alter 已有表——需手动 `ALTER TABLE metadata_tables ADD COLUMN enabled BOOLEAN DEFAULT FALSE` 或 drop 重建。在本任务 commit message 注明。

- [ ] **Step 6: Commit** ——
```bash
git add src/storage/models.py tests/test_datasource_models.py
git commit -m "feat(config): metadata_tables 加 enabled 白名单字段（默认 False）"
```

---

## Task 2: 元数据 API（GET 返回 enabled + PUT 改 enabled）

**Files:** Modify `src/web/routes/admin_metadata.py`、`tests/test_routes_admin_metadata.py`

- [ ] **Step 1: 写失败测试** —— `tests/test_routes_admin_metadata.py` 的 fixture 预置的 MetadataTable 后，加 2 个测试：
```python
@pytest.mark.asyncio
async def test_read_metadata_includes_enabled(client):
    r = await client.get("/api/admin/metadata", params={"datasource_id": client._ds_id})
    t = r.json()["tables"][0]
    assert "enabled" in t
    assert t["enabled"] is False   # 默认不参与

@pytest.mark.asyncio
async def test_toggle_table_enabled(client):
    # 取第一张表的 id（fixture 预置的 fact_power）—— 先读出来
    r = await client.get("/api/admin/metadata", params={"datasource_id": client._ds_id})
    table_id = r.json()["tables"][0]["id"]
    # 勾选参与
    r = await client.put(f"/api/admin/metadata/tables/{table_id}", json={"enabled": True})
    assert r.json()["ok"] is True
    # 再读确认
    r = await client.get("/api/admin/metadata", params={"datasource_id": client._ds_id})
    assert r.json()["tables"][0]["enabled"] is True
```
（注意：`read_metadata` 返回的 table dict 要含 `id`——见 Step 3。）

- [ ] **Step 2: 跑红** —— `pytest tests/test_routes_admin_metadata.py -v` → FAIL

- [ ] **Step 3: 改路由** —— `src/web/routes/admin_metadata.py`：
  - `read_metadata` 的 table dict 加 `"id": t.id` 和 `"enabled": t.enabled`
  - 新增端点：
```python
    @router.put("/api/admin/metadata/tables/{table_id}")
    async def set_table_enabled(table_id: int, req: dict) -> dict:
        """勾选/取消表的参与问数开关。"""
        enabled = req.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(400, "enabled 必须是 bool")
        async with AsyncSessionFactory() as s:
            row = await s.get(MetadataTable, table_id)
            if row is None:
                raise HTTPException(404, "表不存在")
            row.enabled = enabled
            await s.commit()
            return {"ok": True}
```

- [ ] **Step 4: 跑绿** —— `pytest tests/test_routes_admin_metadata.py -v` 全过

- [ ] **Step 5: 同步不碰 enabled 自检** —— 确认 `metadata_sync.sync_metadata` 新表 INSERT 用 ORM default（enabled=False），已有表 UPDATE 只碰 comment/type（不碰 enabled）。跑 `pytest tests/test_metadata_sync.py -v` 确认无回归（已有表 enabled 不被重置——可在 test_sync_inserts 里断言新表 enabled=False）。

- [ ] **Step 6: Commit** ——
```bash
git add src/web/routes/admin_metadata.py tests/test_routes_admin_metadata.py
git commit -m "feat(config): 元数据 API 返回 enabled + 改参与开关端点"
```

---

## Task 3: index.html 配置页 UI（Tab + 数据源管理 + 表勾选）

**Files:** Modify `static/index.html`（前端，手动验证，无 TDD）

- [ ] **Step 1: 加 Tab 切换** —— 改 `<header>`，在 `<h1>` 后加两个 tab 按钮 + 切换 JS。把现有 `#messages`/`footer`（对话区）包进 `#chat-panel`，新增 `#config-panel`（默认隐藏）：
```html
<header>
  <h1>AI 问数</h1>
  <nav>
    <button id="tab-chat" class="tab active">对话</button>
    <button id="tab-config" class="tab">配置</button>
  </nav>
  <select id="mode">...</select>
</header>
<div id="chat-panel">  <!-- 包现有 #messages + footer -->
  <div id="messages"></div>
  <footer>...</footer>
</div>
<div id="config-panel" style="display:none">
  <!-- Step 2 填 -->
</div>
```
JS：
```javascript
const chatPanel = document.getElementById("chat-panel");
const configPanel = document.getElementById("config-panel");
function showTab(which) {
  const isChat = which === "chat";
  chatPanel.style.display = isChat ? "flex" : "none";
  configPanel.style.display = isChat ? "none" : "block";
  document.getElementById("tab-chat").classList.toggle("active", isChat);
  document.getElementById("tab-config").classList.toggle("active", !isChat);
  if (!isChat) loadDatasources();
}
document.getElementById("tab-chat").onclick = () => showTab("chat");
document.getElementById("tab-config").onclick = () => showTab("config");
```
（#app 是 flex column，#chat-panel 要 flex:1 flex column 才不破坏现有布局——调 CSS。）

- [ ] **Step 2: 配置面板结构 + 数据源管理** —— `#config-panel` 内：
```html
<div id="config-panel" style="display:none">
  <div class="cfg-layout">
    <div class="cfg-left">
      <button id="btn-new-ds">+ 新建数据源</button>
      <div id="ds-list"></div>
    </div>
    <div class="cfg-right">
      <div id="tables-area">点左侧选数据源</div>
    </div>
  </div>
</div>
<!-- 新建/编辑数据源弹层 -->
<div id="ds-modal" class="modal" style="display:none">
  <div class="modal-body">
    <h3>数据源</h3>
    <input id="ds-name" placeholder="名称">
    <input id="ds-type" placeholder="类型" value="starrocks">
    <input id="ds-host" placeholder="host">
    <input id="ds-port" placeholder="port" value="9030">
    <input id="ds-db" placeholder="库名">
    <input id="ds-user" placeholder="用户名">
    <input id="ds-pwd" type="password" placeholder="密码">
    <input id="ds-scope" placeholder="同步范围(前缀逗号分隔,空=全部)">
    <button id="ds-save">保存</button>
    <button id="ds-cancel">取消</button>
  </div>
</div>
```

- [ ] **Step 3: 数据源 JS（load/CRUD/测试/同步）** —— `<script>` 加：
```javascript
let selectedDsId = null;

async function loadDatasources() {
  const r = await fetch("/api/admin/datasources");
  const {datasources} = await r.json();
  const list = document.getElementById("ds-list");
  list.innerHTML = "";
  for (const ds of datasources) {
    const div = document.createElement("div");
    div.className = "ds-item" + (ds.id === selectedDsId ? " selected" : "");
    div.innerHTML = `<b>${ds.name}</b> <span>${ds.type}</span>
      <button data-act="test">测试</button>
      <button data-act="sync">同步</button>
      <button data-act="edit">编辑</button>
      <button data-act="del">删除</button>`;
    div.onclick = (e) => {
      if (e.target.tagName === "BUTTON") return;
      selectedDsId = ds.id; loadDatasources(); loadTables(ds.id);
    };
    div.querySelector('[data-act=test]').onclick = async () => {
      const rr = await fetch(`/api/admin/datasources/${ds.id}/test`, {method:"POST"});
      alert(rr.ok ? "连接成功" : "连接失败: " + (await rr.json()).detail);
    };
    div.querySelector('[data-act=sync]').onclick = async () => {
      const rr = await fetch(`/api/admin/datasources/${ds.id}/sync`, {method:"POST"});
      if (rr.ok) { alert("同步完成: " + JSON.stringify(await rr.json())); loadTables(ds.id); }
      else alert("同步失败");
    };
    div.querySelector('[data-act=edit]').onclick = () => openDsModal(ds);
    div.querySelector('[data-act=del]').onclick = async () => {
      if (!confirm("删除数据源 " + ds.name + "?")) return;
      await fetch(`/api/admin/datasources/${ds.id}`, {method:"DELETE"});
      if (selectedDsId === ds.id) { selectedDsId = null; document.getElementById("tables-area").innerHTML = "点左侧选数据源"; }
      loadDatasources();
    };
    list.appendChild(div);
  }
}
// openDsModal/saveDs：填表单 → POST(新建) 或 PUT(编辑)；密码框编辑时空=不改。省略细节，照 ds-* input 取值 fetch。
```

- [ ] **Step 4: 表勾选 JS** —— 加：
```javascript
async function loadTables(dsId) {
  const r = await fetch(`/api/admin/metadata?datasource_id=${dsId}`);
  const {tables} = await r.json();
  const area = document.getElementById("tables-area");
  if (!tables.length) { area.innerHTML = "无元数据，点「同步」拉取"; return; }
  area.innerHTML = '<button onclick="syncDs()">同步元数据</button>';
  for (const t of tables) {
    const row = document.createElement("div");
    row.className = "table-row";
    row.innerHTML = `<label><input type="checkbox" ${t.enabled?"checked":""}> ${t.table_name}</label>
      <span class="cmt">${t.table_comment||""}</span>
      <button class="expand">字段</button>`;
    const cb = row.querySelector("input");
    cb.onchange = async () => {
      const rr = await fetch(`/api/admin/metadata/tables/${t.id}`,
        {method:"PUT", headers:{"Content-Type":"application/json"},
         body: JSON.stringify({enabled: cb.checked})});
      if (!rr.ok) { cb.checked = !cb.checked; alert("保存失败"); }
    };
    // expand 字段：t.columns 展开显示（只读）
    row.querySelector(".expand").onclick = () => {
      alert(t.columns.map(c => `${c.column_name}(${c.data_type}) ${c.column_comment||""}`).join("\n"));
    };
    area.appendChild(row);
  }
}
async function syncDs() {
  if (!selectedDsId) return;
  await fetch(`/api/admin/datasources/${selectedDsId}/sync`, {method:"POST"});
  loadTables(selectedDsId);
}
```

- [ ] **Step 5: CSS** —— 加 `.tab`/`.tab.active`/`.cfg-layout`(flex)/`.cfg-left`/`.cfg-right`/`.ds-item`/`.ds-item.selected`/`.table-row`/`.modal` 样式（简洁即可，参照现有配色 #4caf50 等）。

- [ ] **Step 6: 手动验证** —— 启动（`export NL2SQL_DS_KEY=...; ./run.sh`），浏览器开 `http://127.0.0.1:8000`：
  - 点「配置」tab → 配置面板显示，对话隐藏；点「对话」切回。
  - +新建数据源 → 填 → 保存 → 左侧出现。
  - 点测试/同步/编辑/删除都工作。
  - 选数据源 → 右侧表出现（同步后），勾选框可勾且保存。

- [ ] **Step 7: Commit** ——
```bash
git add static/index.html
git commit -m "feat(config): 配置页 Tab + 数据源管理 + 表参与勾选 UI"
```

---

## Task 4: 联调（手动，真 StarRocks）

**Files:** 无代码改动。

- [ ] **Step 1: 启动** —— `export NL2SQL_DS_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"); ./run.sh`

- [ ] **Step 2: 网页全流程** —— 浏览器：
  1. 配置 tab → +新建数据源（填真 StarRocks 连接）→ 保存
  2. 点测试连接 → 「连接成功」
  3. 点同步 → 右侧出现表清单（默认全未勾选）
  4. 勾选 fact_power、dim_station 等业务表 → 即时保存
  5. 点字段按钮 → 看字段名/类型/注释
  6. 切对话 tab → 确认对话功能没被破坏

- [ ] **Step 3: 全量回归** —— `pytest -q` 全绿。

- [ ] **Step 4: 更新进度记录** —— `current-rebuild.md` 记 P1a 完成（含配置页）。

---

## Self-Review

**1. Spec 覆盖**：
- Tab 集成 → Task 3 Step 1 ✓
- 数据源多源 CRUD/测试/同步 → Task 3 Step 2-3 ✓
- 表勾选（metadata_tables.enabled 白名单）→ Task 1 + Task 2 + Task 3 Step 4 ✓
- 同步不碰 enabled → Task 2 Step 5 自检 ✓
- metadata 读返回 enabled + PUT enabled → Task 2 ✓
- 不含规则/模板 UI → 未出现在任务 ✓

**2. 占位**：前端 Step 3 的 openDsModal 细节标了"照 ds-* input 取值 fetch"——implementer 按现有模式补全（POST/PUT）。其余有完整代码。

**3. 类型一致**：`MetadataTable.enabled`（Task 1）→ API 返回 enabled（Task 2）→ 前端勾选（Task 3）→ PUT enabled（Task 2）链路一致。`table_id` 从 GET metadata 的 `id` 取（Task 2 Step 3 加 id 返回）。
