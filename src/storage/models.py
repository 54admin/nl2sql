"""ORM 模型。对应 spec 第 12 章核心表。

列/表级 comment= 会进 DDL（create_all 建表时发 COMMENT ON TABLE/COLUMN）；
已存在的表靠 pg_client.apply_table_comments() 把模型注释刷进 PG，单一事实源=本文件，不维护第二份 SQL。"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import (JSON, String, Text, DateTime, Integer, Boolean,
                        ForeignKey, TypeDecorator, UniqueConstraint, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
# 注意：pgvector 的 Vector 不在顶层 import——pgvector 0.3 顶层 import 会注册 sqlite
# event listener（load_extension('vector')），让 sqlite 测试连接卡死。延迟到 Embedding.load_dialect_impl
# 的 PG 分支内 import（sqlite 测试根本不进那分支），sqlite 测试才不 hang。


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
    __table_args__ = {"comment": "审计追溯：输入→工具→SQL→结果→归因全链路"}
    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="追踪ID")
    session_id: Mapped[str] = mapped_column(String(64), index=True, comment="会话ID")
    user_id: Mapped[str] = mapped_column(String(64), index=True, comment="用户ID")
    raw_input: Mapped[str] = mapped_column(Text, comment="原始输入")
    normalized_input: Mapped[str | None] = mapped_column(Text, nullable=True, comment="纠错后输入")
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
    turn_start / answer_delta(合并) / tool_call / tool_result /
    user_input(多轮澄清每条都记) / clarification / error / cancelled / done。"""
    __tablename__ = "audit_events"
    __table_args__ = {"comment": "审计事件流：一次trace每步一行（细粒度复盘）"}
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: uuid4().hex,
                                     comment="主键UUID（应用端生成，不用PG序列，免序列权限）")
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


class LlmConfigRow(Base):
    """动态 LLM 配置（admin 后台可改，热更新）。按用途 id 多行 + 启停：
    analysis（对话查询 chat）/ embedding（知识库向量）/ attribution（归因推理）。
    LLMService 按 id=用途 取 enabled 配置。"""
    __tablename__ = "llm_config"
    __table_args__ = {"comment": "动态LLM配置（按用途多行analysis/embedding/attribution+启停）"}
    id: Mapped[str] = mapped_column(String(128), primary_key=True,
                                     comment="配置ID（自定义名，如 qwen-chat）")
    purposes: Mapped[list] = mapped_column(JSON, default=list,
                                           comment="用途列表（analysis/embedding/attribution 子集，可多选）")
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
    # 限流（P2）：主动节流防撞网关限流。None=该维度不限（只重试不限速）；admin 按集团实际填。
    rpm_limit: Mapped[int | None] = mapped_column(default=None, nullable=True,
                                                   comment="每分钟请求上限（None=不限）")
    concurrency: Mapped[int | None] = mapped_column(default=None, nullable=True,
                                                     comment="并发上限（None=不限）")
    enabled: Mapped[bool] = mapped_column(default=True, comment="是否启用")
    version: Mapped[int] = mapped_column(default=1, comment="版本号")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")


class FeishuConfigRow(Base):
    """飞书机器人通道动态配置（admin 后台改，热重连）。单行有效（id=default）：
    enabled=true 且凭证齐全时 adapter 从库读并启动 WsClient；改完调 adapter.reload()
    热重连，不重启服务。凭证明文存沿用内网工具惯例。"""
    __tablename__ = "feishu_config"
    __table_args__ = {"comment": "飞书机器人通道动态配置（admin改+热重连，凭证明文存）"}
    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="配置ID（default）")
    app_id: Mapped[str] = mapped_column(String(128), default="", comment="飞书App ID（cli_xxx）")
    app_secret: Mapped[str] = mapped_column(String(256), default="", comment="飞书App Secret")
    whitelist: Mapped[list] = mapped_column(JSON, default=list, comment="open_id白名单（空=不限）")
    card_throttle_ms: Mapped[int] = mapped_column(default=300, comment="卡片流式节流间隔（ms）")
    enabled: Mapped[bool] = mapped_column(default=False, comment="是否启用")
    version: Mapped[int] = mapped_column(default=1, comment="版本号")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")


class AgentLimitsRow(Base):
    """AgentLoop 查询上限动态配置（admin 后台改，重启生效）。单行（id=default）。
    5 个上限对应 AgentLoop.__init__ 的 max_* 参数；lifespan 启动读、构造时传入。"""
    __tablename__ = "agent_limits"
    __table_args__ = {"comment": "AgentLoop 查询上限动态配置（admin改，重启生效）"}
    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="配置ID（default）")
    max_turns: Mapped[int] = mapped_column(default=10, comment="agent 最大循环轮数")
    max_ask_user: Mapped[int] = mapped_column(default=2, comment="向用户澄清次数上限")
    max_sql: Mapped[int] = mapped_column(default=4, comment="单次对话 execute_sql 硬上限")
    max_sql_fail_streak: Mapped[int] = mapped_column(default=2, comment="连续空/错几次提示收手")
    max_meta_per_run: Mapped[int] = mapped_column(default=1, comment="query_metadata 每轮最多次数")
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
    is_active: Mapped[bool] = mapped_column(default=False, comment="是否当前生效（多版本选一，orchestrator 读 is_active=true 的）")
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
    table_name: Mapped[str] = mapped_column(String(128), nullable=False,
                                            comment="规则关联的表全限定名（表级规则必填）")
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
    __table_args__ = {"comment": "SQL模板（人工录入，拼进 system_prompt【SQL 样板】段给 LLM）"}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="模板ID")
    name: Mapped[str] = mapped_column(String(128), comment="模板名")
    sql_template: Mapped[str] = mapped_column(Text, comment="SQL模板（含:param占位）")
    usage: Mapped[str | None] = mapped_column(Text, nullable=True, comment="使用说明：适用场景/参数/改造指引")
    enabled: Mapped[bool] = mapped_column(default=True, comment="是否启用")
    version: Mapped[int] = mapped_column(default=1, comment="版本号")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")


# Qwen3-Embedding-4B 输出维度（建表固定；换 embedding 模型需同步改 + 重建 knowledge_chunks）
EMBEDDING_DIM = 2560


class Embedding(TypeDecorator):
    """向量列：PG 用 pgvector Vector（cosine 距离检索）；sqlite 降级 JSON 存 list（测试兼容，
    否则 create_all 因 pgvector sqlite 扩展缺失而 hang）。
    检索走原生 SQL（PG: embedding <=> :q 余弦距离；sqlite: Python cosine），不依赖列 comparator。"""
    impl = JSON
    cache_ok = True

    def __init__(self, dim: int = EMBEDDING_DIM):
        self.dim = dim
        super().__init__()

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(JSON())
        # 延迟 import（见模块顶部注释）：PG 用真 pgvector Vector
        from pgvector.sqlalchemy import Vector
        return dialect.type_descriptor(Vector(self.dim))


class KnowledgeDoc(Base):
    """知识库文档（P3b）：上传的 TXT/MD，分段后逐 chunk embedding 入 knowledge_chunks。
    admin CRUD + 启停；归因/答疑检索 enabled 文档的 chunks。"""
    __tablename__ = "knowledge_docs"
    __table_args__ = {"comment": "知识库文档（上传TXT/MD，分段embedding入库，归因/答疑检索）"}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="文档ID")
    name: Mapped[str] = mapped_column(String(256), comment="文档名（含扩展名）")
    category: Mapped[str] = mapped_column(String(64), default="general", comment="分类（manual/policy/case/general）")
    enabled: Mapped[bool] = mapped_column(default=True, comment="是否启用")
    chunk_count: Mapped[int] = mapped_column(default=0, comment="分段数")
    version: Mapped[int] = mapped_column(default=1, comment="版本号")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")


class KnowledgeChunk(Base):
    """知识库分段（P3b）：chunk 文本 + embedding 向量。
    embedding 列 PG 用 pgvector Vector(EMBEDDING_DIM)，sqlite 降级 JSON（测试）。
    检索按 enabled doc 过滤 + 向量近邻（PG: embedding <=> :q；sqlite: Python cosine）。"""
    __tablename__ = "knowledge_chunks"
    __table_args__ = {"comment": "知识库分段（embedding向量，PG用pgvector/sqlite降级JSON）"}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="分段ID")
    doc_id: Mapped[int] = mapped_column(ForeignKey("knowledge_docs.id"), index=True, comment="所属文档ID")
    chunk_index: Mapped[int] = mapped_column(comment="段内序号")
    content: Mapped[str] = mapped_column(Text, comment="分段文本")
    embedding: Mapped[list[float]] = mapped_column(Embedding(EMBEDDING_DIM), comment="向量（cosine检索）")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), comment="更新时间")
