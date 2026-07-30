"""PG 引擎 + 会话工厂。生产用 asyncpg，测试可传 sqlite。"""
from urllib.parse import quote_plus

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine)
from sqlalchemy.pool import StaticPool

from src.config import PostgresConfig
from src.logging import get_logger
from src.storage.models import Base

log = get_logger(__name__)

_engine = None
_AsyncSessionFactory: async_sessionmaker[AsyncSession] | None = None


def _pg_url(config: PostgresConfig) -> str:
    # 用户名/密码 quote_plus 编码，防止 @:/ 等字符破坏 URL 解析
    user = quote_plus(config.username)
    pwd = quote_plus(config.password)
    return (f"postgresql+asyncpg://{user}:{pwd}"
            f"@{config.host}:{config.port}/{config.database}")


# 幂等迁移：DBeaver 层级范式（db_name 可空 + metadata_tables 加 schema_name + 唯一约束升级）。
# 仅对 PG 生效，SQLite 测试每次内存库重建不走这里（新模型直接生效）。
# ponytail: 不引 Alembic——一条 ALTER 一个 IF NOT EXISTS，幂等可重复跑。
_PG_MIGRATIONS = [
    "ALTER TABLE datasources ALTER COLUMN db_name DROP NOT NULL",
    "ALTER TABLE metadata_tables ADD COLUMN IF NOT EXISTS schema_name VARCHAR(128)",
    "ALTER TABLE metadata_tables DROP CONSTRAINT IF EXISTS uq_ds_table",
    "ALTER TABLE metadata_tables DROP CONSTRAINT IF EXISTS uq_ds_schema_table",
    """ALTER TABLE metadata_tables
       ADD CONSTRAINT uq_ds_schema_table UNIQUE (datasource_id, schema_name, table_name)""",
    "CREATE INDEX IF NOT EXISTS ix_metadata_tables_schema_name ON metadata_tables(schema_name)",
    # 会话标题 + 逻辑删除（create_all 不给已存在表加列，这里幂等 ALTER）
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title VARCHAR(128)",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
    # LLM 配置加上下文窗口列 + 协议列（压缩阈值/多协议用）
    "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS max_context INTEGER DEFAULT 32000",
    "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS protocol VARCHAR(16) DEFAULT 'openai'",
    # 限流列（P2 主动节流）：None=不限
    "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS rpm_limit INTEGER",
    "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS concurrency INTEGER",
    "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(128)",
    "UPDATE llm_config SET embedding_model = 'Qwen3-Embedding-4B' WHERE embedding_model IS NULL",
    # 多用途重构（analysis/embedding/attribution 多行 + 启停）：embedding 从 default.embedding_model 拆独立行
    "INSERT INTO llm_config (id, model, base_url, api_key, temperature, timeout, max_context, protocol, rpm_limit, concurrency, enabled, version) SELECT 'embedding', embedding_model, base_url, api_key, temperature, timeout, max_context, protocol, rpm_limit, concurrency, true, 1 FROM llm_config WHERE id='default' AND embedding_model IS NOT NULL ON CONFLICT (id) DO NOTHING",
    "INSERT INTO llm_config (id, model, base_url, api_key, temperature, timeout, max_context, protocol, rpm_limit, concurrency, enabled, version) SELECT 'attribution', model, base_url, api_key, temperature, timeout, max_context, protocol, rpm_limit, concurrency, true, 1 FROM llm_config WHERE id='default' ON CONFLICT (id) DO NOTHING",
    "UPDATE llm_config SET id='analysis' WHERE id='default'",
    "ALTER TABLE llm_config DROP COLUMN IF EXISTS embedding_model",
    "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS purpose VARCHAR(32)",
    # 原 "UPDATE llm_config SET purpose=id" 已删：model 级后 id=host__model 变长，塞 purpose varchar(32) 超长报错；purpose 列已废弃（改用 purposes）
    "CREATE INDEX IF NOT EXISTS ix_llm_config_purpose ON llm_config(purpose)",
    # audit_traces 补成败标记+最终答案列（细粒度统计用）；audit_events 是新表 create_all 建。
    "ALTER TABLE audit_traces ADD COLUMN IF NOT EXISTS success BOOLEAN",
    "ALTER TABLE audit_traces ADD COLUMN IF NOT EXISTS final_answer TEXT",
    "DROP TABLE IF EXISTS app_config",
    # 业务规则表级化（通用规则已挪进系统提示词，本表只存表级规则，关联 table_name）
    "ALTER TABLE business_rules ADD COLUMN IF NOT EXISTS table_name VARCHAR(128)",
    "ALTER TABLE business_rules ALTER COLUMN table_name SET NOT NULL",
    # SQL 模板：加 usage 存使用说明（拼进 system_prompt【SQL 样板】段给 LLM）
    "ALTER TABLE sql_templates ADD COLUMN IF NOT EXISTS usage TEXT",
    # llm_config.id 扩容：网关__模型 的 id 较长，老表 varchar(32) 装不下
    "ALTER TABLE llm_config ALTER COLUMN id TYPE VARCHAR(128)",
    # 模型级重构：加 purposes（JSON 多选）；旧 purpose 单值先转进 purposes，聚合同模型多行→一行 由迁移脚本做
    "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS purposes JSON",
    "UPDATE llm_config SET purposes = json_build_array(purpose) WHERE purposes IS NULL AND purpose IS NOT NULL",
]


async def init_db(url: str | None = None, config: PostgresConfig | None = None,
                  auto_migrate: bool = False):
    """初始化引擎并建表。url 优先（测试用 sqlite），否则用 config 拼 pg。
    auto_migrate=true 才跑建扩展/迁移/刷注释（权限敏感，生产普通账号设 false 跳过）。"""
    global _engine, _AsyncSessionFactory
    target = url or _pg_url(config)
    kwargs = {}
    is_sqlite = "sqlite" in target
    if is_sqlite and ":memory:" in target:
        # 内存 sqlite 必须单连接复用，否则跨 AsyncSession 看不到表
        kwargs = {"poolclass": StaticPool,
                  "connect_args": {"check_same_thread": False}}
    _engine = create_async_engine(target, echo=False, **kwargs)
    _AsyncSessionFactory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        if not is_sqlite and auto_migrate:
            # pgvector 扩展（auto_migrate=true 才试，savepoint 兜底）
            try:
                async with conn.begin_nested():
                    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception as e:
                log.warning("pgvector 扩展创建失败（知识库不可用，联系DBA预装/授权）: %s", e)
        # create_all：sqlite 测试每次建（内存库）；PG 仅 auto_migrate=true 时建/检查
        # 生产 auto_migrate=false 表已建好，跳过避免逐表 IF NOT EXISTS 连库检查卡顿
        if is_sqlite or auto_migrate:
            await conn.run_sync(Base.metadata.create_all)
        if not is_sqlite and auto_migrate:
            # 迁移每条 savepoint 隔离：已应用/非 owner 权限不足跳过，不阻塞启动
            # （华为云等托管库表可能非当前账号所建，ALTER/COMMENT 要 owner 会失败；
            #  create_all 已建最新结构，这些 ALTER 多为冗余，跳了不影响）
            for stmt in _PG_MIGRATIONS:
                try:
                    async with conn.begin_nested():
                        await conn.execute(text(stmt))
                except Exception as e:
                    log.warning("迁移跳过（权限/已应用）[%s]", stmt[:60])
            # 模型注释刷 PG（COMMENT 也要 owner，失败跳）
            try:
                await conn.run_sync(_apply_model_comments)
            except Exception as e:
                log.warning("模型注释刷新跳过（权限）: %s", e)
    log.info("PG 已初始化: %s", "sqlite" if is_sqlite else "postgres")


def _apply_model_comments(sync_conn, *args, **kwargs):
    """把所有 ORM 表/列 comment 刷进 PG。run_sync 回调，在同步连接上发 COMMENT 语句。
    幂等：重复跑只是覆盖，无副作用。用字面量拼注释（COMMENT 语法不支持参数绑定），
    注释是模型里写死的中文常量、非用户输入，不存在注入风险。"""
    from src.storage.models import Base
    def _q(s: str) -> str:
        # PG 字符串字面量：单引号转义为两个单引号
        return "'" + s.replace("'", "''") + "'"
    for table in Base.metadata.sorted_tables:
        if table.comment:
            sync_conn.execute(text(
                f"COMMENT ON TABLE {table.name} IS {_q(table.comment)}"))
        for col in table.columns:
            if col.comment:
                sync_conn.execute(text(
                    f'COMMENT ON COLUMN {table.name}."{col.name}" IS {_q(col.comment)}'))


def AsyncSessionFactory() -> AsyncSession:
    """用法: async with AsyncSessionFactory() as s: ..."""
    if _AsyncSessionFactory is None:
        raise RuntimeError("PG 未初始化，请先调用 init_db()")
    return _AsyncSessionFactory()
