"""从 ORM 生成 MySQL 建表 DDL（db/schema_mysql.sql），与 gen_schema.py（PG）并列。

ORM 是单一事实源，本脚本只换方言为 MySQL，并按 MySQL 合法顺序排属性
（COMMENT 必须在 DEFAULT/AUTO_INCREMENT 之后）、Text→MEDIUMTEXT（长 prompt/审计
防超 64KB）、补 ENGINE=InnoDB CHARSET=utf8mb4（中文注释）。

用法: python3 scripts/gen_schema_mysql.py
要求: MySQL ≥ 5.7（JSON）；DATETIME DEFAULT CURRENT_TIMESTAMP 需 ≥ 5.6.5。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import Integer, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import UniqueConstraint
from src.storage.models import Base

d = mysql.dialect()
HEADER = """-- ============================================================
-- nl2sql 数据库 schema (MySQL) —— 从 ORM 编译生成
-- ------------------------------------------------------------
-- 生成: python3 scripts/gen_schema_mysql.py   单一事实源: src/storage/models.py
-- 要求: MySQL >= 5.7 (JSON); DATETIME DEFAULT CURRENT_TIMESTAMP 需 >= 5.6.5
-- 建库: CREATE DATABASE nl2sql DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
-- ============================================================

"""


def esc(s): return (s or "").replace("\\", "\\\\").replace("'", "''")


def col_line(col):
    t = "MEDIUMTEXT" if isinstance(col.type, Text) else col.type.compile(dialect=d)
    parts = [f"  `{col.name}` {t}"]
    if not col.nullable:
        parts.append("NOT NULL")
    if isinstance(col.type, Integer) and col.primary_key:
        parts.append("AUTO_INCREMENT")
    if col.server_default is not None:        # 唯一一种 server_default = func.now()
        parts.append("DEFAULT CURRENT_TIMESTAMP")
    if col.comment:
        parts.append(f"COMMENT '{esc(col.comment)}'")
    return " ".join(parts)


def table_ddl(table):
    cols = [col_line(c) for c in table.columns]
    cons = []
    if (pk := [c.name for c in table.primary_key.columns]):
        cons.append(f"  PRIMARY KEY ({', '.join(f'`{c}`' for c in pk)})")
    for uc in table.constraints:
        if isinstance(uc, UniqueConstraint) and uc.columns:
            cs = ", ".join(f"`{c.name}`" for c in uc.columns)
            cons.append(f"  UNIQUE KEY `{uc.name}` ({cs})" if uc.name else f"  UNIQUE ({cs})")
    for fk in table.foreign_keys:
        rt, rc = fk.target_fullname.split(".")
        cons.append(f"  CONSTRAINT `{table.name}_fk_{fk.parent.name}` "
                    f"FOREIGN KEY (`{fk.parent.name}`) REFERENCES `{rt}` (`{rc}`)")
    tc = f" COMMENT='{esc(table.comment)}'" if table.comment else ""
    return (f"CREATE TABLE `{table.name}` (\n{',\n'.join(cols + cons)}\n) "
            f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci{tc};")


def generate():
    parts = []
    for table in Base.metadata.sorted_tables:
        parts.append(table_ddl(table))
        for idx in sorted(table.indexes, key=lambda i: i.name):
            cs = ", ".join(f"`{c.name}`" for c in idx.columns)
            parts.append(f"CREATE INDEX `{idx.name}` ON `{table.name}` ({cs});")
    return HEADER + "\n\n".join(parts) + "\n"


if __name__ == "__main__":
    out = Path("db/schema_mysql.sql")
    out.parent.mkdir(exist_ok=True)
    out.write_text(generate(), encoding="utf-8")
    print(f"wrote {out} ({len(Base.metadata.sorted_tables)} tables)")
