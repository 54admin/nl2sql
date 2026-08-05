"""从 ORM 模型(Base.metadata)编译生成 db/schema.sql —— 生产建表唯一事实源。

保证 schema.sql 与 ORM 模型一一对应（改表 = 改 ORM → 重跑本脚本）。
用法: python3 scripts/gen_schema.py
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent.parent))

from pathlib import Path

from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy.dialects import postgresql

from src.storage.models import Base

HEADER = """-- ============================================================
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

"""


def _pg_quote(s: str) -> str:
    """PG 字符串字面量：单引号转义为两个单引号。"""
    return "'" + str(s).replace("'", "''") + "'"


def _tidy(ddl: str) -> str:
    """规整 SQLAlchemy 编译输出：tab→4空格、去行尾空白。"""
    return "\n".join(line.replace("\t", "    ").rstrip() for line in ddl.splitlines())


def generate() -> str:
    dialect = postgresql.dialect()
    parts = []
    for table in Base.metadata.sorted_tables:
        parts.append(
            _tidy(str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip()) + ";")
        for idx in sorted(table.indexes, key=lambda i: i.name):
            parts.append(
                _tidy(str(CreateIndex(idx, if_not_exists=True).compile(dialect=dialect)).strip()) + ";")
        if table.comment:
            parts.append(f"COMMENT ON TABLE {table.name} IS {_pg_quote(table.comment)};")
        for col in table.columns:
            if col.comment:
                parts.append(f'COMMENT ON COLUMN {table.name}."{col.name}" IS {_pg_quote(col.comment)};')
    return HEADER + "\n\n".join(parts) + "\n"


if __name__ == "__main__":
    out = Path("db/schema.sql")
    out.parent.mkdir(exist_ok=True)
    out.write_text(generate(), encoding="utf-8")
    print(f"wrote {out} ({len(Base.metadata.sorted_tables)} tables)")
