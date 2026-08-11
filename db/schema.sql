-- ============================================================
-- nl2sql 数据库 schema —— 生产建表唯一事实源
-- ------------------------------------------------------------
-- 说明：
--   * 本文件由 src/storage/models.py 的 ORM(Base.metadata)编译生成，
--     与 ORM 模型一一对应。改表结构 = 改 ORM 模型 → 重新生成本文件。
--   * 生产用表 owner 账号(liuxiangwu)或 superuser 执行一次（全部幂等）。
--     应用账号 ai_online 无 DDL 权限，不能建表/改表。
--   * 应用启动时不再碰任何 DDL（init_db 只建连接，不建表）。
--   * 应用账号永不碰 DDL；只读连接由 init_db 建立。
--
-- 重新生成：python3 scripts/gen_schema.py
-- 防漂移校验：scripts/check_schema.py（ORM↔schema.sql 不一致即 fail）
-- ============================================================

CREATE TABLE IF NOT EXISTS nl_cfg_datasources (
    id SERIAL NOT NULL,
    name VARCHAR(64) NOT NULL,
    type VARCHAR(32) NOT NULL,
    host VARCHAR(128) NOT NULL,
    port INTEGER NOT NULL,
    db_name VARCHAR(128),
    username VARCHAR(128) NOT NULL,
    password_enc TEXT NOT NULL,
    sync_scope VARCHAR(256),
    enabled BOOLEAN NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (name)
);

COMMENT ON TABLE nl_cfg_datasources IS '业务数据源连接配置（密码明文存，内网工具）';

COMMENT ON COLUMN nl_cfg_datasources."id" IS '数据源ID';

COMMENT ON COLUMN nl_cfg_datasources."name" IS '名称';

COMMENT ON COLUMN nl_cfg_datasources."type" IS '类型（starrocks/mysql/pg）';

COMMENT ON COLUMN nl_cfg_datasources."host" IS '主机';

COMMENT ON COLUMN nl_cfg_datasources."port" IS '端口';

COMMENT ON COLUMN nl_cfg_datasources."db_name" IS '默认库（空=连实例，多库导航）';

COMMENT ON COLUMN nl_cfg_datasources."username" IS '用户名';

COMMENT ON COLUMN nl_cfg_datasources."password_enc" IS '密码（明文存，内网工具）';

COMMENT ON COLUMN nl_cfg_datasources."sync_scope" IS '同步范围（表名前缀，空=全要）';

COMMENT ON COLUMN nl_cfg_datasources."enabled" IS '是否启用';

COMMENT ON COLUMN nl_cfg_datasources."version" IS '版本号';

COMMENT ON COLUMN nl_cfg_datasources."created_at" IS '创建时间';

COMMENT ON COLUMN nl_cfg_datasources."updated_at" IS '更新时间';

CREATE TABLE IF NOT EXISTS nl_cfg_feishu (
    id VARCHAR(64) NOT NULL,
    app_id VARCHAR(128) NOT NULL,
    app_secret VARCHAR(256) NOT NULL,
    whitelist JSON NOT NULL,
    card_throttle_ms INTEGER NOT NULL,
    enabled BOOLEAN NOT NULL,
    version INTEGER NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

COMMENT ON TABLE nl_cfg_feishu IS '飞书机器人通道动态配置（admin改+热重连，凭证明文存）';

COMMENT ON COLUMN nl_cfg_feishu."id" IS '配置ID（default）';

COMMENT ON COLUMN nl_cfg_feishu."app_id" IS '飞书App ID（cli_xxx）';

COMMENT ON COLUMN nl_cfg_feishu."app_secret" IS '飞书App Secret';

COMMENT ON COLUMN nl_cfg_feishu."whitelist" IS 'open_id白名单（空=不限）';

COMMENT ON COLUMN nl_cfg_feishu."card_throttle_ms" IS '卡片流式节流间隔（ms）';

COMMENT ON COLUMN nl_cfg_feishu."enabled" IS '是否启用';

COMMENT ON COLUMN nl_cfg_feishu."version" IS '版本号';

COMMENT ON COLUMN nl_cfg_feishu."updated_at" IS '更新时间';

CREATE TABLE IF NOT EXISTS nl_cfg_limits (
    id VARCHAR(64) NOT NULL,
    max_turns INTEGER NOT NULL,
    max_ask_user INTEGER NOT NULL,
    max_sql INTEGER NOT NULL,
    max_sql_fail_streak INTEGER NOT NULL,
    max_meta_per_run INTEGER NOT NULL,
    version INTEGER NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

COMMENT ON TABLE nl_cfg_limits IS 'AgentLoop 查询上限动态配置（admin改，重启生效）';

COMMENT ON COLUMN nl_cfg_limits."id" IS '配置ID（default）';

COMMENT ON COLUMN nl_cfg_limits."max_turns" IS 'agent 最大循环轮数';

COMMENT ON COLUMN nl_cfg_limits."max_ask_user" IS '向用户澄清次数上限';

COMMENT ON COLUMN nl_cfg_limits."max_sql" IS '单次对话 execute_sql 硬上限';

COMMENT ON COLUMN nl_cfg_limits."max_sql_fail_streak" IS '连续空/错几次提示收手';

COMMENT ON COLUMN nl_cfg_limits."max_meta_per_run" IS 'query_metadata 每轮最多次数';

COMMENT ON COLUMN nl_cfg_limits."version" IS '版本号';

COMMENT ON COLUMN nl_cfg_limits."updated_at" IS '更新时间';

CREATE TABLE IF NOT EXISTS nl_cfg_llm (
    id VARCHAR(128) NOT NULL,
    purposes JSON NOT NULL,
    model VARCHAR(128) NOT NULL,
    base_url VARCHAR(256) NOT NULL,
    api_key VARCHAR(256) NOT NULL,
    temperature FLOAT NOT NULL,
    timeout INTEGER NOT NULL,
    max_context INTEGER NOT NULL,
    protocol VARCHAR(16) NOT NULL,
    rpm_limit INTEGER,
    concurrency INTEGER,
    enabled BOOLEAN NOT NULL,
    version INTEGER NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

COMMENT ON TABLE nl_cfg_llm IS '动态LLM配置（按用途多行analysis/attribution+启停）';

COMMENT ON COLUMN nl_cfg_llm."id" IS '配置ID（自定义名，如 qwen-chat）';

COMMENT ON COLUMN nl_cfg_llm."purposes" IS '用途列表（analysis/attribution 子集，可多选）';

COMMENT ON COLUMN nl_cfg_llm."model" IS '模型名';

COMMENT ON COLUMN nl_cfg_llm."base_url" IS 'API地址';

COMMENT ON COLUMN nl_cfg_llm."api_key" IS '密钥';

COMMENT ON COLUMN nl_cfg_llm."temperature" IS '温度';

COMMENT ON COLUMN nl_cfg_llm."timeout" IS '超时（秒）';

COMMENT ON COLUMN nl_cfg_llm."max_context" IS '模型上下文窗口（token，压缩阈值用）';

COMMENT ON COLUMN nl_cfg_llm."protocol" IS '协议（openai/anthropic，按网关额度桶选）';

COMMENT ON COLUMN nl_cfg_llm."rpm_limit" IS '每分钟请求上限（None=不限）';

COMMENT ON COLUMN nl_cfg_llm."concurrency" IS '并发上限（None=不限）';

COMMENT ON COLUMN nl_cfg_llm."enabled" IS '是否启用';

COMMENT ON COLUMN nl_cfg_llm."version" IS '版本号';

COMMENT ON COLUMN nl_cfg_llm."updated_at" IS '更新时间';

CREATE TABLE IF NOT EXISTS nl_cfg_ragflow (
    id VARCHAR(64) NOT NULL,
    base_url VARCHAR(256) NOT NULL,
    api_key VARCHAR(256) NOT NULL,
    dataset_ids JSON NOT NULL,
    top_k INTEGER NOT NULL,
    similarity_threshold FLOAT NOT NULL,
    vector_similarity_weight FLOAT NOT NULL,
    enabled BOOLEAN NOT NULL,
    version INTEGER NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

COMMENT ON TABLE nl_cfg_ragflow IS 'RAGFlow外部知识库配置（base_url+api_key+参与检索的dataset_ids，热更新）';

COMMENT ON COLUMN nl_cfg_ragflow."id" IS '配置ID（default）';

COMMENT ON COLUMN nl_cfg_ragflow."base_url" IS 'RAGFlow 地址（如 http://10.x.x.x:9380，不带/api/v1）';

COMMENT ON COLUMN nl_cfg_ragflow."api_key" IS 'RAGFlow API Key（平台头像→API Key 生成）';

COMMENT ON COLUMN nl_cfg_ragflow."dataset_ids" IS '参与检索的知识库ID列表（RAGFlow dataset_id，多选）';

COMMENT ON COLUMN nl_cfg_ragflow."top_k" IS '检索返回片段数';

COMMENT ON COLUMN nl_cfg_ragflow."similarity_threshold" IS '相似度门槛（低于不计）';

COMMENT ON COLUMN nl_cfg_ragflow."vector_similarity_weight" IS '向量相似度权重（1-x=关键词权重）';

COMMENT ON COLUMN nl_cfg_ragflow."enabled" IS '是否启用 RAGFlow 知识库';

COMMENT ON COLUMN nl_cfg_ragflow."version" IS '版本号';

COMMENT ON COLUMN nl_cfg_ragflow."updated_at" IS '更新时间';

CREATE TABLE IF NOT EXISTS nl_cfg_skills (
    scene VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    tools JSON NOT NULL,
    mode VARCHAR(16) NOT NULL,
    "order" INTEGER NOT NULL,
    version INTEGER NOT NULL,
    enabled BOOLEAN NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (scene)
);

COMMENT ON TABLE nl_cfg_skills IS 'skill单一真相源（DB权威，seed由gen_seed灌入）';

COMMENT ON COLUMN nl_cfg_skills."scene" IS 'skill名，主键';

COMMENT ON COLUMN nl_cfg_skills."content" IS '提示词内容（admin在线改，热生效）';

COMMENT ON COLUMN nl_cfg_skills."tools" IS '依赖工具名数组，与enabled联动装配（关skill→工具摘）';

COMMENT ON COLUMN nl_cfg_skills."mode" IS 'always_on=启动自动注入；on_demand=按需加载（保留位）';

COMMENT ON COLUMN nl_cfg_skills."order" IS '注入顺序（小在前）';

COMMENT ON COLUMN nl_cfg_skills."version" IS '版本号';

COMMENT ON COLUMN nl_cfg_skills."enabled" IS 'skill总开关：false=整skill关（提示词不注入+工具摘除）';

COMMENT ON COLUMN nl_cfg_skills."updated_at" IS '更新时间';

CREATE TABLE IF NOT EXISTS nl_hi_events (
    id VARCHAR(36) NOT NULL,
    trace_id VARCHAR(64) NOT NULL,
    seq INTEGER NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    turn INTEGER,
    content_json TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_nl_hi_events_trace_id ON nl_hi_events (trace_id);

COMMENT ON TABLE nl_hi_events IS '审计事件流：一次trace每步一行（细粒度复盘）';

COMMENT ON COLUMN nl_hi_events."id" IS '主键UUID（应用端生成，不用PG序列，免序列权限）';

COMMENT ON COLUMN nl_hi_events."trace_id" IS '追踪ID';

COMMENT ON COLUMN nl_hi_events."seq" IS '事件序号（同trace内递增，排序用）';

COMMENT ON COLUMN nl_hi_events."event_type" IS '事件类型（turn_start/tool_call/...）';

COMMENT ON COLUMN nl_hi_events."turn" IS '第几轮（loop内）';

COMMENT ON COLUMN nl_hi_events."content_json" IS '事件内容（JSON：参数/结果/思考等）';

COMMENT ON COLUMN nl_hi_events."created_at" IS '创建时间';

CREATE TABLE IF NOT EXISTS nl_hi_results (
    result_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    columns_json TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    total INTEGER NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (result_id)
);

CREATE INDEX IF NOT EXISTS ix_nl_hi_results_session_id ON nl_hi_results (session_id);

COMMENT ON TABLE nl_hi_results IS 'execute_sql全量结果旁路（审计/持久+前端按result_id取）';

COMMENT ON COLUMN nl_hi_results."result_id" IS '结果ID';

COMMENT ON COLUMN nl_hi_results."session_id" IS '会话ID';

COMMENT ON COLUMN nl_hi_results."columns_json" IS '列定义（JSON）';

COMMENT ON COLUMN nl_hi_results."rows_json" IS '全量行（JSON）';

COMMENT ON COLUMN nl_hi_results."total" IS '总行数';

COMMENT ON COLUMN nl_hi_results."created_at" IS '创建时间';

CREATE TABLE IF NOT EXISTS nl_hi_traces (
    trace_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    raw_input TEXT NOT NULL,
    tool_calls_json TEXT,
    sql_text TEXT,
    result_id VARCHAR(64),
    elapsed_ms INTEGER,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    success BOOLEAN,
    final_answer TEXT,
    PRIMARY KEY (trace_id)
);

CREATE INDEX IF NOT EXISTS ix_nl_hi_traces_session_id ON nl_hi_traces (session_id);

CREATE INDEX IF NOT EXISTS ix_nl_hi_traces_user_id ON nl_hi_traces (user_id);

COMMENT ON TABLE nl_hi_traces IS '审计追溯：一次问数从输入到答案的全链路（汇总行+事件流）';

COMMENT ON COLUMN nl_hi_traces."trace_id" IS '追踪ID';

COMMENT ON COLUMN nl_hi_traces."session_id" IS '会话ID';

COMMENT ON COLUMN nl_hi_traces."user_id" IS '用户ID';

COMMENT ON COLUMN nl_hi_traces."raw_input" IS '原始输入';

COMMENT ON COLUMN nl_hi_traces."tool_calls_json" IS '工具调用记录（JSON）';

COMMENT ON COLUMN nl_hi_traces."sql_text" IS '生成的SQL';

COMMENT ON COLUMN nl_hi_traces."result_id" IS '结果旁路ID';

COMMENT ON COLUMN nl_hi_traces."elapsed_ms" IS '耗时（毫秒）';

COMMENT ON COLUMN nl_hi_traces."created_at" IS '创建时间';

COMMENT ON COLUMN nl_hi_traces."success" IS '是否成功（done=True，否则False）';

COMMENT ON COLUMN nl_hi_traces."final_answer" IS '最终答案/错误文案（切会话/统计用）';

CREATE TABLE IF NOT EXISTS nl_md_rules (
    id SERIAL NOT NULL,
    table_name VARCHAR(128) NOT NULL,
    key VARCHAR(128) NOT NULL,
    value_json TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

COMMENT ON TABLE nl_md_rules IS '业务规则（人工录入，表级；query_metadata 按表附给 LLM）';

COMMENT ON COLUMN nl_md_rules."id" IS '规则ID';

COMMENT ON COLUMN nl_md_rules."table_name" IS '规则关联的表全限定名（表级规则必填）';

COMMENT ON COLUMN nl_md_rules."key" IS '键名';

COMMENT ON COLUMN nl_md_rules."value_json" IS '规则值（JSON）';

COMMENT ON COLUMN nl_md_rules."enabled" IS '是否启用';

COMMENT ON COLUMN nl_md_rules."version" IS '版本号';

COMMENT ON COLUMN nl_md_rules."created_at" IS '创建时间';

COMMENT ON COLUMN nl_md_rules."updated_at" IS '更新时间';

CREATE TABLE IF NOT EXISTS nl_md_templates (
    id SERIAL NOT NULL,
    name VARCHAR(128) NOT NULL,
    sql_template TEXT NOT NULL,
    usage TEXT,
    enabled BOOLEAN NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

COMMENT ON TABLE nl_md_templates IS 'SQL模板（人工录入，拼进 get_sql_template 工具 description，LLM 按需调工具取）';

COMMENT ON COLUMN nl_md_templates."id" IS '模板ID';

COMMENT ON COLUMN nl_md_templates."name" IS '模板名';

COMMENT ON COLUMN nl_md_templates."sql_template" IS 'SQL模板（含:param占位）';

COMMENT ON COLUMN nl_md_templates."usage" IS '使用说明：适用场景/参数/改造指引';

COMMENT ON COLUMN nl_md_templates."enabled" IS '是否启用';

COMMENT ON COLUMN nl_md_templates."version" IS '版本号';

COMMENT ON COLUMN nl_md_templates."created_at" IS '创建时间';

COMMENT ON COLUMN nl_md_templates."updated_at" IS '更新时间';

CREATE TABLE IF NOT EXISTS nl_ru_checkpoints (
    id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    messages_json TEXT NOT NULL,
    pending_tool VARCHAR(64),
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_nl_ru_checkpoints_session_id ON nl_ru_checkpoints (session_id);

COMMENT ON TABLE nl_ru_checkpoints IS 'ask_user挂起时的loop上下文快照（断点恢复用）';

COMMENT ON COLUMN nl_ru_checkpoints."id" IS '检查点ID';

COMMENT ON COLUMN nl_ru_checkpoints."session_id" IS '会话ID';

COMMENT ON COLUMN nl_ru_checkpoints."messages_json" IS '挂起时的消息快照（JSON）';

COMMENT ON COLUMN nl_ru_checkpoints."pending_tool" IS '挂起的工具调用ID';

COMMENT ON COLUMN nl_ru_checkpoints."created_at" IS '创建时间';

CREATE TABLE IF NOT EXISTS nl_ru_messages (
    id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    trace_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_nl_ru_messages_session_id ON nl_ru_messages (session_id);

CREATE INDEX IF NOT EXISTS ix_nl_ru_messages_trace_id ON nl_ru_messages (trace_id);

COMMENT ON TABLE nl_ru_messages IS '会话消息流水';

COMMENT ON COLUMN nl_ru_messages."id" IS '消息ID';

COMMENT ON COLUMN nl_ru_messages."session_id" IS '会话ID';

COMMENT ON COLUMN nl_ru_messages."role" IS '角色（system/user/assistant/tool）';

COMMENT ON COLUMN nl_ru_messages."content" IS '消息内容';

COMMENT ON COLUMN nl_ru_messages."trace_id" IS '追踪ID';

COMMENT ON COLUMN nl_ru_messages."created_at" IS '创建时间';

CREATE TABLE IF NOT EXISTS nl_ru_sessions (
    id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    channel VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    title VARCHAR(128),
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    ttl_at TIMESTAMP WITHOUT TIME ZONE,
    deleted_at TIMESTAMP WITHOUT TIME ZONE,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_nl_ru_sessions_user_id ON nl_ru_sessions (user_id);

COMMENT ON TABLE nl_ru_sessions IS '问数会话';

COMMENT ON COLUMN nl_ru_sessions."id" IS '会话ID';

COMMENT ON COLUMN nl_ru_sessions."user_id" IS '用户ID';

COMMENT ON COLUMN nl_ru_sessions."channel" IS '接入渠道（web/feishu）';

COMMENT ON COLUMN nl_ru_sessions."status" IS '会话状态（idle/running/awaiting_clarification/done/error）';

COMMENT ON COLUMN nl_ru_sessions."title" IS '会话标题（首问取前若干字，可改名）';

COMMENT ON COLUMN nl_ru_sessions."created_at" IS '创建时间';

COMMENT ON COLUMN nl_ru_sessions."updated_at" IS '更新时间';

COMMENT ON COLUMN nl_ru_sessions."ttl_at" IS '过期时间（超时清空）';

COMMENT ON COLUMN nl_ru_sessions."deleted_at" IS '逻辑删除时间（非空=已删，列表/读取过滤）';

CREATE TABLE IF NOT EXISTS nl_md_relations (
    id SERIAL NOT NULL,
    datasource_id INTEGER NOT NULL,
    main_table VARCHAR(128) NOT NULL,
    rel_table VARCHAR(128) NOT NULL,
    join_keys_json TEXT NOT NULL,
    join_type VARCHAR(16) NOT NULL,
    business_note TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(datasource_id) REFERENCES nl_cfg_datasources (id)
);

CREATE INDEX IF NOT EXISTS ix_nl_md_relations_datasource_id ON nl_md_relations (datasource_id);

COMMENT ON TABLE nl_md_relations IS '逻辑主外键关系（人工录入）；query_metadata 返回，跨表 JOIN 按 this 口径';

COMMENT ON COLUMN nl_md_relations."id" IS '关系ID';

COMMENT ON COLUMN nl_md_relations."datasource_id" IS '数据源ID';

COMMENT ON COLUMN nl_md_relations."main_table" IS '主表';

COMMENT ON COLUMN nl_md_relations."rel_table" IS '关联表';

COMMENT ON COLUMN nl_md_relations."join_keys_json" IS '关联键（JSON：[{main,rel}]）';

COMMENT ON COLUMN nl_md_relations."join_type" IS '关联类型（inner/left）';

COMMENT ON COLUMN nl_md_relations."business_note" IS '业务说明';

COMMENT ON COLUMN nl_md_relations."created_at" IS '创建时间';

COMMENT ON COLUMN nl_md_relations."updated_at" IS '更新时间';

CREATE TABLE IF NOT EXISTS nl_md_tables (
    id SERIAL NOT NULL,
    datasource_id INTEGER NOT NULL,
    schema_name VARCHAR(128),
    table_name VARCHAR(128) NOT NULL,
    table_comment TEXT,
    source VARCHAR(16) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    enabled BOOLEAN NOT NULL,
    display_columns_json TEXT,
    hidden_columns_json TEXT,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_ds_schema_table UNIQUE (datasource_id, schema_name, table_name),
    FOREIGN KEY(datasource_id) REFERENCES nl_cfg_datasources (id)
);

CREATE INDEX IF NOT EXISTS ix_nl_md_tables_datasource_id ON nl_md_tables (datasource_id);

CREATE INDEX IF NOT EXISTS ix_nl_md_tables_schema_name ON nl_md_tables (schema_name);

COMMENT ON TABLE nl_md_tables IS '元数据·表（反向同步+手写覆盖，白名单enabled=true参与问数）';

COMMENT ON COLUMN nl_md_tables."id" IS '表元数据ID';

COMMENT ON COLUMN nl_md_tables."datasource_id" IS '数据源ID';

COMMENT ON COLUMN nl_md_tables."schema_name" IS '库名（空=兼容老数据）';

COMMENT ON COLUMN nl_md_tables."table_name" IS '表名';

COMMENT ON COLUMN nl_md_tables."table_comment" IS '表注释（手写manual优先）';

COMMENT ON COLUMN nl_md_tables."source" IS '来源（synced同步/manual手写）';

COMMENT ON COLUMN nl_md_tables."kind" IS '类型（table/view）';

COMMENT ON COLUMN nl_md_tables."enabled" IS '是否参与问数（白名单，默认不参与）';

COMMENT ON COLUMN nl_md_tables."display_columns_json" IS '展示字段（JSON）';

COMMENT ON COLUMN nl_md_tables."hidden_columns_json" IS '隐藏字段（JSON）';

COMMENT ON COLUMN nl_md_tables."updated_at" IS '更新时间';

CREATE TABLE IF NOT EXISTS nl_md_columns (
    id SERIAL NOT NULL,
    table_id INTEGER NOT NULL,
    column_name VARCHAR(128) NOT NULL,
    column_comment TEXT,
    data_type VARCHAR(64),
    is_primary BOOLEAN NOT NULL,
    role_tag VARCHAR(16),
    source VARCHAR(16) NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_table_col UNIQUE (table_id, column_name),
    FOREIGN KEY(table_id) REFERENCES nl_md_tables (id)
);

CREATE INDEX IF NOT EXISTS ix_nl_md_columns_table_id ON nl_md_columns (table_id);

COMMENT ON TABLE nl_md_columns IS '元数据·字段（反向同步+手写覆盖，role_tag=sensitive过滤回灌LLM）';

COMMENT ON COLUMN nl_md_columns."id" IS '字段元数据ID';

COMMENT ON COLUMN nl_md_columns."table_id" IS '表元数据ID';

COMMENT ON COLUMN nl_md_columns."column_name" IS '字段名';

COMMENT ON COLUMN nl_md_columns."column_comment" IS '字段注释（手写manual优先）';

COMMENT ON COLUMN nl_md_columns."data_type" IS '数据类型';

COMMENT ON COLUMN nl_md_columns."is_primary" IS '是否主键';

COMMENT ON COLUMN nl_md_columns."role_tag" IS '角色标签（sensitive等）';

COMMENT ON COLUMN nl_md_columns."source" IS '来源（synced/manual）';

COMMENT ON COLUMN nl_md_columns."updated_at" IS '更新时间';
