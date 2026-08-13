import sys
sys.path.insert(0, ".")
from sqlalchemy import Integer, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import UniqueConstraint
from src.storage.models import Base

d = mysql.dialect()

def esc(s):
    return (s or "").replace("\\", "\\\\").replace("'", "''")

def col_line(col):
    # 类型：Text → MEDIUMTEXT（长 prompt/审计 JSON 防超 64KB），其余走 SQLAlchemy 编译
    if isinstance(col.type, Text):
        t = "MEDIUMTEXT"
    else:
        t = col.type.compile(dialect=d)
    parts = [f"  `{col.name}` {t}"]
    if not col.nullable:
        parts.append("NOT NULL")
    if isinstance(col.type, Integer) and col.primary_key:
        parts.append("AUTO_INCREMENT")
    if col.server_default is not None:  # 唯一一种 server_default 是 func.now()
        parts.append("DEFAULT CURRENT_TIMESTAMP")
    if col.comment:
        parts.append(f"COMMENT '{esc(col.comment)}'")
    return " ".join(parts)

def table_ddl(table):
    cols = [col_line(c) for c in table.columns]
    cons = []
    pk = [c.name for c in table.primary_key.columns]
    if pk:
        cons.append(f"  PRIMARY KEY ({', '.join(f'`{c}`' for c in pk)})")
    for uc in table.constraints:
        if isinstance(uc, UniqueConstraint) and uc.columns:
            cs = ", ".join(f"`{c.name}`" for c in uc.columns)
            cons.append(f"  UNIQUE KEY `{uc.name}` ({cs})" if uc.name else f"  UNIQUE ({cs})")
    for fk in table.foreign_keys:
        rt, rc = fk.target_fullname.split(".")
        cons.append(f"  CONSTRAINT `{table.name}_fk_{fk.parent.name}` "
                    f"FOREIGN KEY (`{fk.parent.name}`) REFERENCES `{rt}` (`{rc}`)")
    inner = ",\n".join(cols + cons)
    tc = f" COMMENT='{esc(table.comment)}'" if table.comment else ""
    return (f"CREATE TABLE `{table.name}` (\n{inner}\n) "
            f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci{tc};")

def index_ddl(idx):
    cs = ", ".join(f"`{c.name}`" for c in idx.columns)
    return f"CREATE INDEX `{idx.name}` ON `{table.name}` ({cs});"

parts = []
for table in Base.metadata.sorted_tables:
    parts.append(table_ddl(table))
    for idx in sorted(table.indexes, key=lambda i: i.name):
        parts.append(f"CREATE INDEX `{idx.name}` ON `{table.name}` "
                     f"({', '.join(f'`{c.name}`' for c in idx.columns)});")
print("\n\n".join(parts))
print(f"\n-- {len(Base.metadata.sorted_tables)} tables", file=sys.stderr)
