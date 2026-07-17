"""ORM 模型。对应 spec 第 12 章核心表。"""
from datetime import datetime

from sqlalchemy import String, Text, DateTime, func
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
