-- ============================================================
-- nl2sql 数据库 schema (MySQL) —— 从 ORM 编译生成
-- ------------------------------------------------------------
-- 生成: python3 scripts/gen_schema_mysql.py   单一事实源: src/storage/models.py
-- 要求: MySQL >= 5.7 (JSON); DATETIME DEFAULT CURRENT_TIMESTAMP 需 >= 5.6.5
-- 建库: CREATE DATABASE nl2sql DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
-- ============================================================

CREATE TABLE `nl_cfg_datasources` (
  `id` INTEGER NOT NULL AUTO_INCREMENT COMMENT '数据源ID',
  `name` VARCHAR(64) NOT NULL COMMENT '名称',
  `type` VARCHAR(32) NOT NULL COMMENT '类型（starrocks/mysql/pg）',
  `host` VARCHAR(128) NOT NULL COMMENT '主机',
  `port` INTEGER NOT NULL COMMENT '端口',
  `db_name` VARCHAR(128) COMMENT '默认库（空=连实例，多库导航）',
  `username` VARCHAR(128) NOT NULL COMMENT '用户名',
  `password_enc` MEDIUMTEXT NOT NULL COMMENT '密码（明文存，内网工具）',
  `sync_scope` VARCHAR(256) COMMENT '同步范围（表名前缀，空=全要）',
  `enabled` BOOL NOT NULL COMMENT '是否启用',
  `version` INTEGER NOT NULL COMMENT '版本号',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='业务数据源连接配置（密码明文存，内网工具）';

CREATE TABLE `nl_cfg_feishu` (
  `id` VARCHAR(64) NOT NULL COMMENT '配置ID（default）',
  `app_id` VARCHAR(128) NOT NULL COMMENT '飞书App ID（cli_xxx）',
  `app_secret` VARCHAR(256) NOT NULL COMMENT '飞书App Secret',
  `whitelist` JSON NOT NULL COMMENT 'open_id白名单（空=不限）',
  `card_throttle_ms` INTEGER NOT NULL COMMENT '卡片流式节流间隔（ms）',
  `enabled` BOOL NOT NULL COMMENT '是否启用',
  `version` INTEGER NOT NULL COMMENT '版本号',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='飞书机器人通道动态配置（admin改+热重连，凭证明文存）';

CREATE TABLE `nl_cfg_limits` (
  `id` VARCHAR(64) NOT NULL COMMENT '配置ID（default）',
  `max_turns` INTEGER NOT NULL COMMENT 'agent 最大循环轮数',
  `max_ask_user` INTEGER NOT NULL COMMENT '向用户澄清次数上限',
  `max_sql` INTEGER NOT NULL COMMENT '单次对话 execute_sql 硬上限',
  `max_sql_fail_streak` INTEGER NOT NULL COMMENT '连续空/错几次提示收手',
  `max_meta_per_run` INTEGER NOT NULL COMMENT 'query_metadata 每轮最多次数',
  `version` INTEGER NOT NULL COMMENT '版本号',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AgentLoop 查询上限动态配置（admin改，重启生效）';

CREATE TABLE `nl_cfg_llm` (
  `id` VARCHAR(128) NOT NULL COMMENT '配置ID（自定义名，如 qwen-chat）',
  `purposes` JSON NOT NULL COMMENT '用途列表（analysis/attribution 子集，可多选）',
  `model` VARCHAR(128) NOT NULL COMMENT '模型名',
  `base_url` VARCHAR(256) NOT NULL COMMENT 'API地址',
  `api_key` VARCHAR(256) NOT NULL COMMENT '密钥',
  `temperature` FLOAT NOT NULL COMMENT '温度',
  `timeout` INTEGER NOT NULL COMMENT '超时（秒）',
  `max_context` INTEGER NOT NULL COMMENT '模型上下文窗口（token，压缩阈值用）',
  `protocol` VARCHAR(16) NOT NULL COMMENT '协议（openai/anthropic，按网关额度桶选）',
  `rpm_limit` INTEGER COMMENT '每分钟请求上限（None=不限）',
  `concurrency` INTEGER COMMENT '并发上限（None=不限）',
  `enabled` BOOL NOT NULL COMMENT '是否启用',
  `version` INTEGER NOT NULL COMMENT '版本号',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='动态LLM配置（按用途多行analysis/attribution+启停）';

CREATE TABLE `nl_cfg_ragflow` (
  `id` VARCHAR(64) NOT NULL COMMENT '配置ID（default）',
  `base_url` VARCHAR(256) NOT NULL COMMENT 'RAGFlow 地址（如 http://10.x.x.x:9380，不带/api/v1）',
  `api_key` VARCHAR(256) NOT NULL COMMENT 'RAGFlow API Key（平台头像→API Key 生成）',
  `dataset_ids` JSON NOT NULL COMMENT '参与检索的知识库ID列表（RAGFlow dataset_id，多选）',
  `top_k` INTEGER NOT NULL COMMENT '检索返回片段数',
  `similarity_threshold` FLOAT NOT NULL COMMENT '相似度门槛（低于不计）',
  `vector_similarity_weight` FLOAT NOT NULL COMMENT '向量相似度权重（1-x=关键词权重）',
  `enabled` BOOL NOT NULL COMMENT '是否启用 RAGFlow 知识库',
  `version` INTEGER NOT NULL COMMENT '版本号',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='RAGFlow外部知识库配置（base_url+api_key+参与检索的dataset_ids，热更新）';

CREATE TABLE `nl_cfg_skills` (
  `scene` VARCHAR(32) NOT NULL COMMENT 'skill名，主键',
  `content` MEDIUMTEXT NOT NULL COMMENT '提示词内容（admin在线改，热生效）',
  `tools` JSON NOT NULL COMMENT '依赖工具名数组，与enabled联动装配（关skill→工具摘）',
  `mode` VARCHAR(16) NOT NULL COMMENT 'always_on=启动自动注入；on_demand=按需加载（保留位）',
  `order` INTEGER NOT NULL COMMENT '注入顺序（小在前）',
  `version` INTEGER NOT NULL COMMENT '版本号',
  `enabled` BOOL NOT NULL COMMENT 'skill总开关：false=整skill关（提示词不注入+工具摘除）',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`scene`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='skill单一真相源（DB权威，seed由gen_seed灌入）';

CREATE TABLE `nl_hi_events` (
  `id` VARCHAR(36) NOT NULL COMMENT '主键UUID（应用端生成，不用PG序列，免序列权限）',
  `trace_id` VARCHAR(64) NOT NULL COMMENT '追踪ID',
  `seq` INTEGER NOT NULL COMMENT '事件序号（同trace内递增，排序用）',
  `event_type` VARCHAR(32) NOT NULL COMMENT '事件类型（turn_start/tool_call/...）',
  `turn` INTEGER COMMENT '第几轮（loop内）',
  `content_json` MEDIUMTEXT COMMENT '事件内容（JSON：参数/结果/思考等）',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='审计事件流：一次trace每步一行（细粒度复盘）';

CREATE INDEX `ix_nl_hi_events_trace_id` ON `nl_hi_events` (`trace_id`);

CREATE TABLE `nl_hi_results` (
  `result_id` VARCHAR(64) NOT NULL COMMENT '结果ID',
  `session_id` VARCHAR(64) NOT NULL COMMENT '会话ID',
  `columns_json` MEDIUMTEXT NOT NULL COMMENT '列定义（JSON）',
  `rows_json` MEDIUMTEXT NOT NULL COMMENT '全量行（JSON）',
  `total` INTEGER NOT NULL COMMENT '总行数',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`result_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='execute_sql全量结果旁路（审计/持久+前端按result_id取）';

CREATE INDEX `ix_nl_hi_results_session_id` ON `nl_hi_results` (`session_id`);

CREATE TABLE `nl_hi_traces` (
  `trace_id` VARCHAR(64) NOT NULL COMMENT '追踪ID',
  `session_id` VARCHAR(64) NOT NULL COMMENT '会话ID',
  `user_id` VARCHAR(64) NOT NULL COMMENT '用户ID',
  `raw_input` MEDIUMTEXT NOT NULL COMMENT '原始输入',
  `tool_calls_json` MEDIUMTEXT COMMENT '工具调用记录（JSON）',
  `sql_text` MEDIUMTEXT COMMENT '生成的SQL',
  `result_id` VARCHAR(64) COMMENT '结果旁路ID',
  `elapsed_ms` INTEGER COMMENT '耗时（毫秒）',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `success` BOOL COMMENT '是否成功（done=True，否则False）',
  `final_answer` MEDIUMTEXT COMMENT '最终答案/错误文案（切会话/统计用）',
  PRIMARY KEY (`trace_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='审计追溯：一次问数从输入到答案的全链路（汇总行+事件流）';

CREATE INDEX `ix_nl_hi_traces_session_id` ON `nl_hi_traces` (`session_id`);

CREATE INDEX `ix_nl_hi_traces_user_id` ON `nl_hi_traces` (`user_id`);

CREATE TABLE `nl_md_rules` (
  `id` INTEGER NOT NULL AUTO_INCREMENT COMMENT '规则ID',
  `table_name` VARCHAR(128) NOT NULL COMMENT '规则关联的表全限定名（表级规则必填）',
  `key` VARCHAR(128) NOT NULL COMMENT '键名',
  `value_json` MEDIUMTEXT NOT NULL COMMENT '规则值（JSON）',
  `enabled` BOOL NOT NULL COMMENT '是否启用',
  `version` INTEGER NOT NULL COMMENT '版本号',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='业务规则（人工录入，表级；query_metadata 按表附给 LLM）';

CREATE TABLE `nl_md_templates` (
  `id` INTEGER NOT NULL AUTO_INCREMENT COMMENT '模板ID',
  `name` VARCHAR(128) NOT NULL COMMENT '模板名',
  `sql_template` MEDIUMTEXT NOT NULL COMMENT 'SQL模板（含:param占位）',
  `usage` MEDIUMTEXT COMMENT '使用说明：适用场景/参数/改造指引',
  `enabled` BOOL NOT NULL COMMENT '是否启用',
  `version` INTEGER NOT NULL COMMENT '版本号',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='SQL模板（人工录入，拼进 get_sql_template 工具 description，LLM 按需调工具取）';

CREATE TABLE `nl_ru_checkpoints` (
  `id` VARCHAR(64) NOT NULL COMMENT '检查点ID',
  `session_id` VARCHAR(64) NOT NULL COMMENT '会话ID',
  `messages_json` MEDIUMTEXT NOT NULL COMMENT '挂起时的消息快照（JSON）',
  `pending_tool` VARCHAR(64) COMMENT '挂起的工具调用ID',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='ask_user挂起时的loop上下文快照（断点恢复用）';

CREATE INDEX `ix_nl_ru_checkpoints_session_id` ON `nl_ru_checkpoints` (`session_id`);

CREATE TABLE `nl_ru_messages` (
  `id` VARCHAR(64) NOT NULL COMMENT '消息ID',
  `session_id` VARCHAR(64) NOT NULL COMMENT '会话ID',
  `role` VARCHAR(16) NOT NULL COMMENT '角色（system/user/assistant/tool）',
  `content` MEDIUMTEXT NOT NULL COMMENT '消息内容',
  `trace_id` VARCHAR(64) NOT NULL COMMENT '追踪ID',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会话消息流水';

CREATE INDEX `ix_nl_ru_messages_session_id` ON `nl_ru_messages` (`session_id`);

CREATE INDEX `ix_nl_ru_messages_trace_id` ON `nl_ru_messages` (`trace_id`);

CREATE TABLE `nl_ru_sessions` (
  `id` VARCHAR(64) NOT NULL COMMENT '会话ID',
  `user_id` VARCHAR(64) NOT NULL COMMENT '用户ID',
  `channel` VARCHAR(32) NOT NULL COMMENT '接入渠道（web/feishu）',
  `status` VARCHAR(32) NOT NULL COMMENT '会话状态（idle/running/awaiting_clarification/done/error）',
  `title` VARCHAR(128) COMMENT '会话标题（首问取前若干字，可改名）',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  `ttl_at` DATETIME COMMENT '过期时间（超时清空）',
  `deleted_at` DATETIME COMMENT '逻辑删除时间（非空=已删，列表/读取过滤）',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='问数会话';

CREATE INDEX `ix_nl_ru_sessions_user_id` ON `nl_ru_sessions` (`user_id`);

CREATE TABLE `nl_md_relations` (
  `id` INTEGER NOT NULL AUTO_INCREMENT COMMENT '关系ID',
  `datasource_id` INTEGER NOT NULL COMMENT '数据源ID',
  `main_table` VARCHAR(128) NOT NULL COMMENT '主表',
  `rel_table` VARCHAR(128) NOT NULL COMMENT '关联表',
  `join_keys_json` MEDIUMTEXT NOT NULL COMMENT '关联键（JSON：[{main,rel}]）',
  `join_type` VARCHAR(16) NOT NULL COMMENT '关联类型（inner/left）',
  `business_note` MEDIUMTEXT COMMENT '业务说明',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  CONSTRAINT `nl_md_relations_fk_datasource_id` FOREIGN KEY (`datasource_id`) REFERENCES `nl_cfg_datasources` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='逻辑主外键关系（人工录入）；query_metadata 返回，跨表 JOIN 按 this 口径';

CREATE INDEX `ix_nl_md_relations_datasource_id` ON `nl_md_relations` (`datasource_id`);

CREATE TABLE `nl_md_tables` (
  `id` INTEGER NOT NULL AUTO_INCREMENT COMMENT '表元数据ID',
  `datasource_id` INTEGER NOT NULL COMMENT '数据源ID',
  `schema_name` VARCHAR(128) COMMENT '库名（空=兼容老数据）',
  `table_name` VARCHAR(128) NOT NULL COMMENT '表名',
  `table_comment` MEDIUMTEXT COMMENT '表注释（手写manual优先）',
  `source` VARCHAR(16) NOT NULL COMMENT '来源（synced同步/manual手写）',
  `kind` VARCHAR(16) NOT NULL COMMENT '类型（table/view）',
  `enabled` BOOL NOT NULL COMMENT '是否参与问数（白名单，默认不参与）',
  `display_columns_json` MEDIUMTEXT COMMENT '展示字段（JSON）',
  `hidden_columns_json` MEDIUMTEXT COMMENT '隐藏字段（JSON）',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ds_schema_table` (`datasource_id`, `schema_name`, `table_name`),
  CONSTRAINT `nl_md_tables_fk_datasource_id` FOREIGN KEY (`datasource_id`) REFERENCES `nl_cfg_datasources` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='元数据·表（反向同步+手写覆盖，白名单enabled=true参与问数）';

CREATE INDEX `ix_nl_md_tables_datasource_id` ON `nl_md_tables` (`datasource_id`);

CREATE INDEX `ix_nl_md_tables_schema_name` ON `nl_md_tables` (`schema_name`);

CREATE TABLE `nl_md_columns` (
  `id` INTEGER NOT NULL AUTO_INCREMENT COMMENT '字段元数据ID',
  `table_id` INTEGER NOT NULL COMMENT '表元数据ID',
  `column_name` VARCHAR(128) NOT NULL COMMENT '字段名',
  `column_comment` MEDIUMTEXT COMMENT '字段注释（手写manual优先）',
  `data_type` VARCHAR(64) COMMENT '数据类型',
  `is_primary` BOOL NOT NULL COMMENT '是否主键',
  `role_tag` VARCHAR(16) COMMENT '角色标签（sensitive等）',
  `source` VARCHAR(16) NOT NULL COMMENT '来源（synced/manual）',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_table_col` (`table_id`, `column_name`),
  CONSTRAINT `nl_md_columns_fk_table_id` FOREIGN KEY (`table_id`) REFERENCES `nl_md_tables` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='元数据·字段（反向同步+手写覆盖，role_tag=sensitive过滤回灌LLM）';

CREATE INDEX `ix_nl_md_columns_table_id` ON `nl_md_columns` (`table_id`);
