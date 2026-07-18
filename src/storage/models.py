"""ORM 模型。对应 spec 第 12 章核心表。"""
from datetime import datetime

from sqlalchemy import String, Text, DateTime, Integer, Boolean, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="idle")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())
    ttl_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))     # system/user/assistant/tool
    content: Mapped[str] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditTrace(Base):
    __tablename__ = "audit_traces"
    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    raw_input: Mapped[str] = mapped_column(Text)
    normalized_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrections_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    knowledge_hits_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attribution_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sse_log_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(nullable=True)
    cost_tokens: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LoopCheckpoint(Base):
    """ask_user 挂起时的 loop 上下文快照。P0b 用。"""
    __tablename__ = "loop_checkpoints"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    messages_json: Mapped[str] = mapped_column(Text)
    pending_tool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class QueryResult(Base):
    """execute_sql 全量结果旁路。P1 用，P0a 先建表。"""
    __tablename__ = "query_results"
    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    columns_json: Mapped[str] = mapped_column(Text)
    rows_json: Mapped[str] = mapped_column(Text)
    total: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AppConfigRow(Base):
    """通用动态配置 KV（页面配置模型基础）。
    llm_config / prompts 本 plan 选独立结构化表，此表作为通用 escape hatch：
    未来任意 key/value 配置（feature flag、阈值、开关）可走此表。"""
    __tablename__ = "app_config"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class LlmConfigRow(Base):
    """动态 LLM 配置（admin 后台可改，热更新）。单行表 id='default'。
    LLMService 调用时优先读此表（enabled=True），无则 fallback yml。"""
    __tablename__ = "llm_config"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    model: Mapped[str] = mapped_column(String(128))
    base_url: Mapped[str] = mapped_column(String(256))
    api_key: Mapped[str] = mapped_column(String(256))
    temperature: Mapped[float] = mapped_column(default=0.0)
    timeout: Mapped[int] = mapped_column(default=60)
    enabled: Mapped[bool] = mapped_column(default=True)
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class Prompt(Base):
    """场景化系统提示词（orchestrator 按 scene 读，组装 system message）。
    场景如 default / attribution / correction；default 是兜底。
    admin 后台 CRUD，热更新（PromptStore 缓存刷新）。"""
    __tablename__ = "prompts"
    scene: Mapped[str] = mapped_column(String(32), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class Datasource(Base):
    """业务数据源连接配置（密码加密存）。P1a。"""
    __tablename__ = "datasources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(32))           # starrocks/mysql/pg
    host: Mapped[str] = mapped_column(String(128))
    port: Mapped[int] = mapped_column(Integer)
    db_name: Mapped[str] = mapped_column(String(128))
    username: Mapped[str] = mapped_column(String(128))
    password_enc: Mapped[str] = mapped_column(Text)         # Fernet 密文
    sync_scope: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class MetadataTable(Base):
    """元数据·表（反向同步 + 手写覆盖）。P1a。"""
    __tablename__ = "metadata_tables"
    __table_args__ = (UniqueConstraint("datasource_id", "table_name", name="uq_ds_table"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    datasource_id: Mapped[int] = mapped_column(ForeignKey("datasources.id"), index=True)
    table_name: Mapped[str] = mapped_column(String(128))
    table_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="synced")  # synced/manual
    display_columns_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    hidden_columns_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class MetadataColumn(Base):
    """元数据·字段。P1a。"""
    __tablename__ = "metadata_columns"
    __table_args__ = (UniqueConstraint("table_id", "column_name", name="uq_table_col"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("metadata_tables.id"), index=True)
    column_name: Mapped[str] = mapped_column(String(128))
    column_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    role_tag: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="synced")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class TableRelation(Base):
    """逻辑主外键关系（人工录入）。P1a 建口径，P1c JOIN 消费。"""
    __tablename__ = "table_relations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    datasource_id: Mapped[int] = mapped_column(ForeignKey("datasources.id"), index=True)
    main_table: Mapped[str] = mapped_column(String(128))
    rel_table: Mapped[str] = mapped_column(String(128))
    join_keys_json: Mapped[str] = mapped_column(Text)       # [{"main":"a.id","rel":"b.a_id"}]
    join_type: Mapped[str] = mapped_column(String(16), default="inner")
    business_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class BusinessRule(Base):
    """业务规则（人工录入）。P1a 建口径，后续阶段消费。"""
    __tablename__ = "business_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(32), index=True)  # metric/constraint/interaction/attribution
    key: Mapped[str] = mapped_column(String(128))
    value_json: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class SqlTemplate(Base):
    """SQL 模板（人工录入）。P1a 建口径，P1b 应用。"""
    __tablename__ = "sql_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    datasource_id: Mapped[int] = mapped_column(ForeignKey("datasources.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    trigger_keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_semantics: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql_template: Mapped[str] = mapped_column(Text)
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    formatters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())
