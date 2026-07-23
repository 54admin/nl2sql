"""ORM 模型。对应 spec 第 12 章核心表。

列/表级 comment= 会进 DDL（create_all 建表时发 COMMENT ON TABLE/COLUMN）；
已存在的表靠 pg_client.apply_table_comments() 把模型注释刷进 PG，单一事实源=本文件，不维护第二份 SQL。"""
from datetime import datetime

from sqlalchemy import String, Text, DateTime, Integer, Boolean, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Session(Base):
    """问数会话。"""
    __tablename__ = "sessions"
    __table_args__ = {"comment": "问数会话"}
    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="会话ID")
    user_id: Mapped[str] = mapped_column(String(64), index=True, comment="用户ID")
    channel: Mapped[str] = mapped_column(String(32), comment="接入渠道（web/feishu）")
    status: Mapped[str] = mapped_column(String(32), default="idle",
                                        comment="会话状态（idle/running/awaiting_clarification/done/error）")
    title: Mapped[str | None] = mapped_column(String(128), nullable=True,
                                              comment="会话标题（首问取前若干字，可改名）")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")
    ttl_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="过期时间（超时清空）")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True,
                                                        comment="逻辑删除时间（非空=已删，列表/读取过滤）")


class Message(Base):
    """会话消息流水。"""
    __tablename__ = "messages"
    __table_args__ = {"comment": "会话消息流水"}
    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="消息ID")
    session_id: Mapped[str] = mapped_column(String(64), index=True, comment="会话ID")
    role: Mapped[str] = mapped_column(String(16), comment="角色（system/user/assistant/tool）")
    content: Mapped[str] = mapped_column(Text, comment="消息内容")
    trace_id: Mapped[str] = mapped_column(String(64), index=True, comment="追踪ID")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")


class AuditTrace(Base):
    """审计追溯：全链路记录。"""
    __tablename__ = "audit_traces"
    __table_args__ = {"comment": "审计追溯：输入→纠错→工具→SQL→结果→归因全链路"}
    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="追踪ID")
    session_id: Mapped[str] = mapped_column(String(64), index=True, comment="会话ID")
    user_id: Mapped[str] = mapped_column(String(64), index=True, comment="用户ID")
    raw_input: Mapped[str] = mapped_column(Text, comment="原始输入")
    normalized_input: Mapped[str | None] = mapped_column(Text, nullable=True, comment="纠错后输入")
    corrections_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="名称纠错记录（JSON）")
    tool_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="工具调用记录（JSON）")
    sql_text: Mapped[str | None] = mapped_column(Text, nullable=True, comment="生成的SQL")
    result_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="结果旁路ID")
    knowledge_hits_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="知识库命中（JSON）")
    attribution_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="归因结论（JSON）")
    sse_log_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="SSE流式日志（JSON）")
    elapsed_ms: Mapped[int | None] = mapped_column(nullable=True, comment="耗时（毫秒）")
    cost_tokens: Mapped[int | None] = mapped_column(nullable=True, comment="token消耗")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    # 成败标记（细粒度统计用）：done=True，error/cancelled/max_turns=False
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True,
                                                 comment="是否成功（done=True，否则False）")
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True,
                                                     comment="最终答案/错误文案（切会话/统计用）")


class AuditEvent(Base):
    """审计事件流：一次 trace 的每一步一行（细粒度复盘用）。
    turn_start / answer_delta(合并) / tool_call / tool_result / correction /
    user_input(多轮澄清每条都记) / clarification / error / cancelled / done。"""
    __tablename__ = "audit_events"
    __table_args__ = {"comment": "审计事件流：一次trace每步一行（细粒度复盘）"}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True, comment="追踪ID")
    seq: Mapped[int] = mapped_column(Integer, comment="事件序号（同trace内递增，排序用）")
    event_type: Mapped[str] = mapped_column(String(32), comment="事件类型（turn_start/tool_call/...）")
    turn: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="第几轮（loop内）")
    content_json: Mapped[str | None] = mapped_column(Text, nullable=True,
                                                     comment="事件内容（JSON：参数/结果/思考等）")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                  comment="创建时间")


class LoopCheckpoint(Base):
    """ask_user 挂起时的 loop 上下文快照。P0b 用。"""
    __tablename__ = "loop_checkpoints"
    __table_args__ = {"comment": "ask_user挂起时的loop上下文快照（断点恢复用）"}
    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="检查点ID")
    session_id: Mapped[str] = mapped_column(String(64), index=True, comment="会话ID")
    messages_json: Mapped[str] = mapped_column(Text, comment="挂起时的消息快照（JSON）")
    pending_tool: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="挂起的工具调用ID")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")


class QueryResult(Base):
    """execute_sql 全量结果旁路。P1 用，P0a 先建表。"""
    __tablename__ = "query_results"
    __table_args__ = {"comment": "execute_sql全量结果旁路（审计/持久+前端按result_id取）"}
    result_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="结果ID")
    session_id: Mapped[str] = mapped_column(String(64), index=True, comment="会话ID")
    columns_json: Mapped[str] = mapped_column(Text, comment="列定义（JSON）")
    rows_json: Mapped[str] = mapped_column(Text, comment="全量行（JSON）")
    total: Mapped[int] = mapped_column(default=0, comment="总行数")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")


class AppConfigRow(Base):
    """通用动态配置 KV（页面配置模型基础）。
    llm_config / prompts 本 plan 选独立结构化表，此表作为通用 escape hatch：
    未来任意 key/value 配置（feature flag、阈值、开关）可走此表。"""
    __tablename__ = "app_config"
    __table_args__ = {"comment": "通用动态配置KV（feature flag/阈值/开关兜底）"}
    key: Mapped[str] = mapped_column(String(64), primary_key=True, comment="配置键")
    value_json: Mapped[str] = mapped_column(Text, comment="配置值（JSON）")
    version: Mapped[int] = mapped_column(default=1, comment="版本号")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")


class LlmConfigRow(Base):
    """动态 LLM 配置（admin 后台可改，热更新）。单行表 id='default'。
    LLMService 调用时优先读此表（enabled=True），无则 fallback yml。"""
    __tablename__ = "llm_config"
    __table_args__ = {"comment": "动态LLM配置（admin后台热更新，单行id=default）"}
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default", comment="配置ID（默认default）")
    model: Mapped[str] = mapped_column(String(128), comment="模型名")
    base_url: Mapped[str] = mapped_column(String(256), comment="API地址")
    api_key: Mapped[str] = mapped_column(String(256), comment="密钥")
    temperature: Mapped[float] = mapped_column(default=0.0, comment="温度")
    timeout: Mapped[int] = mapped_column(default=60, comment="超时（秒）")
    # 模型上下文窗口（token），会话压缩按总量占比触发用。默认 32000；大窗口模型按实际填。
    max_context: Mapped[int] = mapped_column(default=32000, comment="模型上下文窗口（token，压缩阈值用）")
    # 协议：openai（/v1/chat/completions + Bearer）或 anthropic（/v1/messages + x-api-key）。
    # 同网关常按协议分额度桶，按需选有额度的协议。
    protocol: Mapped[str] = mapped_column(String(16), default="openai",
                                           comment="协议（openai/anthropic，按网关额度桶选）")
    enabled: Mapped[bool] = mapped_column(default=True, comment="是否启用")
    version: Mapped[int] = mapped_column(default=1, comment="版本号")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")


class Prompt(Base):
    """场景化系统提示词（orchestrator 按 scene 读，组装 system message）。
    场景如 default / attribution / correction；default 是兜底。
    admin 后台 CRUD，热更新（PromptStore 缓存刷新）。"""
    __tablename__ = "prompts"
    __table_args__ = {"comment": "场景化系统提示词（按scene读，default兜底，admin热更新）"}
    scene: Mapped[str] = mapped_column(String(32), primary_key=True, comment="场景（default/attribution/correction）")
    content: Mapped[str] = mapped_column(Text, comment="提示词内容")
    version: Mapped[int] = mapped_column(default=1, comment="版本号")
    enabled: Mapped[bool] = mapped_column(default=True, comment="是否启用")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")


class Datasource(Base):
    """业务数据源连接配置（密码明文存，内网工具去加密）。P1a。

    DBeaver 式层级：一个数据源 = 一个连接（实例），db_name 改 nullable——
    建源只填连接信息，下挂多库（schema）。db_name 留空时连接串不带 /db（连实例）。
    """
    __tablename__ = "datasources"
    __table_args__ = {"comment": "业务数据源连接配置（密码明文存，内网工具）"}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="数据源ID")
    name: Mapped[str] = mapped_column(String(64), unique=True, comment="名称")
    type: Mapped[str] = mapped_column(String(32), comment="类型（starrocks/mysql/pg）")
    host: Mapped[str] = mapped_column(String(128), comment="主机")
    port: Mapped[int] = mapped_column(Integer, comment="端口")
    db_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="默认库（空=连实例，多库导航）")
    username: Mapped[str] = mapped_column(String(128), comment="用户名")
    password_enc: Mapped[str] = mapped_column(Text, comment="密码（明文存，内网工具）")
    sync_scope: Mapped[str | None] = mapped_column(String(256), nullable=True, comment="同步范围（表名前缀，空=全要）")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")
    version: Mapped[int] = mapped_column(Integer, default=1, comment="版本号")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")


class MetadataTable(Base):
    """元数据·表（反向同步 + 手写覆盖）。P1a。

    schema_name 标记表属于哪个库（DBeaver 层级：源>库>表）；
    老数据 schema_name 为空——兼容（按 datasource_id 读，前端按 schema 分组时空作为默认组）。
    """
    __tablename__ = "metadata_tables"
    __table_args__ = (
        UniqueConstraint("datasource_id", "schema_name", "table_name", name="uq_ds_schema_table"),
        {"comment": "元数据·表（反向同步+手写覆盖，白名单enabled=true参与问数）"},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="表元数据ID")
    datasource_id: Mapped[int] = mapped_column(ForeignKey("datasources.id"), index=True, comment="数据源ID")
    schema_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True, comment="库名（空=兼容老数据）")
    table_name: Mapped[str] = mapped_column(String(128), comment="表名")
    table_comment: Mapped[str | None] = mapped_column(Text, nullable=True, comment="表注释（手写manual优先）")
    source: Mapped[str] = mapped_column(String(16), default="synced", comment="来源（synced同步/manual手写）")
    kind: Mapped[str] = mapped_column(String(16), default="table", comment="类型（table/view）")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否参与问数（白名单，默认不参与）")
    display_columns_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="展示字段（JSON）")
    hidden_columns_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="隐藏字段（JSON）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")


class MetadataColumn(Base):
    """元数据·字段。P1a。"""
    __tablename__ = "metadata_columns"
    __table_args__ = (
        UniqueConstraint("table_id", "column_name", name="uq_table_col"),
        {"comment": "元数据·字段（反向同步+手写覆盖，role_tag=sensitive过滤回灌LLM）"},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="字段元数据ID")
    table_id: Mapped[int] = mapped_column(ForeignKey("metadata_tables.id"), index=True, comment="表元数据ID")
    column_name: Mapped[str] = mapped_column(String(128), comment="字段名")
    column_comment: Mapped[str | None] = mapped_column(Text, nullable=True, comment="字段注释（手写manual优先）")
    data_type: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="数据类型")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否主键")
    role_tag: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="角色标签（sensitive等）")
    source: Mapped[str] = mapped_column(String(16), default="synced", comment="来源（synced/manual）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")


class TableRelation(Base):
    """逻辑主外键关系（人工录入）。P1a 建口径，P1c JOIN 消费。"""
    __tablename__ = "table_relations"
    __table_args__ = {"comment": "逻辑主外键关系（人工录入，P1c JOIN消费）"}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="关系ID")
    datasource_id: Mapped[int] = mapped_column(ForeignKey("datasources.id"), index=True, comment="数据源ID")
    main_table: Mapped[str] = mapped_column(String(128), comment="主表")
    rel_table: Mapped[str] = mapped_column(String(128), comment="关联表")
    join_keys_json: Mapped[str] = mapped_column(Text, comment="关联键（JSON：[{main,rel}]）")
    join_type: Mapped[str] = mapped_column(String(16), default="inner", comment="关联类型（inner/left）")
    business_note: Mapped[str | None] = mapped_column(Text, nullable=True, comment="业务说明")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")


class BusinessRule(Base):
    """业务规则（人工录入）。P1a 建口径，后续阶段消费。"""
    __tablename__ = "business_rules"
    __table_args__ = {"comment": "业务规则（人工录入口径，后续阶段消费）"}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="规则ID")
    category: Mapped[str] = mapped_column(String(32), index=True, comment="分类（metric/constraint/interaction/attribution）")
    key: Mapped[str] = mapped_column(String(128), comment="键名")
    value_json: Mapped[str] = mapped_column(Text, comment="规则值（JSON）")
    enabled: Mapped[bool] = mapped_column(default=True, comment="是否启用")
    version: Mapped[int] = mapped_column(default=1, comment="版本号")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")


class SqlTemplate(Base):
    """SQL 模板（人工录入）。P1a 建口径，P1b 应用。"""
    __tablename__ = "sql_templates"
    __table_args__ = {"comment": "SQL模板（人工录入，P1b按关键词/语义命中应用）"}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="模板ID")
    datasource_id: Mapped[int] = mapped_column(ForeignKey("datasources.id"), index=True, comment="数据源ID")
    name: Mapped[str] = mapped_column(String(128), comment="模板名")
    trigger_keywords: Mapped[str | None] = mapped_column(Text, nullable=True, comment="触发关键词（逗号分隔）")
    trigger_semantics: Mapped[str | None] = mapped_column(Text, nullable=True, comment="触发语义（自然语言描述）")
    sql_template: Mapped[str] = mapped_column(Text, comment="SQL模板（含:param占位）")
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="参数定义（JSON）")
    formatters_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="格式化器（JSON）")
    enabled: Mapped[bool] = mapped_column(default=True, comment="是否启用")
    version: Mapped[int] = mapped_column(default=1, comment="版本号")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")
