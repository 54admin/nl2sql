#!/usr/bin/env python3
"""从现有 PG 库（旧表名）导出 4 张配置表，生成新表名的 INSERT。
用法: python3 scripts/export_configs.py
产出: db/migrate/configs_mysql.sql（只产 MySQL 目标；数据源是线上 PG）
（飞书 feishu_config 不导——用户明确说不用配。）
"""
import asyncio
import asyncpg
from pathlib import Path

CONF = dict(
    host="postgresql.internal.cn-north-9.postgresql.rds.myhuaweicloud.com",
    port=5432, database="nl2sql",
    user="ai_online", password="shkH^A*69sg!wdxjdWfR2s",
)

# 旧表名 → 新表名；列顺序与目标 schema（models.py）对齐
# llm_config 旧表有个废弃列 purpose（全 null），不搬
MAPPING = {
    "llm_config": {
        "new": "nl_cfg_llm",
        "cols": ["id", "purposes", "model", "base_url", "api_key", "temperature",
                 "timeout", "max_context", "protocol", "rpm_limit", "concurrency",
                 "enabled", "version", "updated_at"],
        "json_cols": {"purposes"},
    },
    "datasources": {
        "new": "nl_cfg_datasources",
        "cols": ["id", "name", "type", "host", "port", "db_name", "username",
                 "password_enc", "sync_scope", "enabled", "version",
                 "created_at", "updated_at"],
        "json_cols": set(),
    },
    "sql_templates": {
        "new": "nl_md_templates",
        "cols": ["id", "name", "sql_template", "usage", "enabled", "version",
                 "created_at", "updated_at"],
        "json_cols": set(),
    },
    "ragflow_config": {
        "new": "nl_cfg_ragflow",
        "cols": ["id", "base_url", "api_key", "dataset_ids", "top_k",
                 "similarity_threshold", "vector_similarity_weight", "enabled",
                 "version", "updated_at"],
        "json_cols": {"dataset_ids"},
    },
}


def sql_lit(val, is_json=False, dialect="mysql"):
    """把 Python 值转成 SQL 字面量。dialect: mysql|pg"""
    if val is None:
        return "NULL"
    if is_json:
        # asyncpg 把 json 列读成 str（已是合法 JSON 串）。直接当字符串字面量灌，
        # MySQL/PG 都会把它存进 JSON 列（MySQL JSON 列接受字符串字面量）。
        return esc_str(val)
    if isinstance(val, bool):
        return "1" if val else "0" if dialect == "mysql" else ("true" if val else "false")
    if isinstance(val, (int, float)):
        return repr(val)
    return esc_str(val)


def esc_str(s) -> str:
    """SQL 单引号字符串转义（MySQL/PG 通用：' 转成 ''）。"""
    return "'" + str(s).replace("'", "''") + "'"


def build_inserts(table_new, cols, rows, json_cols, dialect):
    out = []
    col_list = ", ".join(f"`{c}`" if dialect == "mysql" else f'"{c}"' for c in cols)
    for r in rows:
        vals = []
        for c in cols:
            v = r[c]
            # asyncpg 对 json 列默认返回 str；个别驱动可能返回已解析对象，兜底 json.dumps
            if c in json_cols and v is not None and not isinstance(v, str):
                import json as _j
                v = _j.dumps(v, ensure_ascii=False)
            vals.append(sql_lit(v, is_json=(c in json_cols), dialect=dialect))
        verb = "INSERT IGNORE" if dialect == "mysql" else "INSERT"
        conflict = "" if dialect == "mysql" else ' ON CONFLICT DO NOTHING'
        out.append(f"{verb} INTO {table_new} ({col_list}) VALUES ({', '.join(vals)});{conflict}")
    return out


async def main():
    conn = await asyncpg.connect(**CONF)
    lines = [
        "-- 从在线 PG 导出的环境配置（模型/数据源/模板/知识库）",
        "-- 源: nl2sql@online PG (旧表名) → 目标: MySQL 新表名（dev profile 连的华为云 RDS MySQL）",
        "-- 飞书 feishu_config 不含（用户指定不配）。生成器: scripts/export_configs.py",
        "",
    ]

    for old, spec in MAPPING.items():
        rows = await conn.fetch(f'SELECT {", ".join(spec["cols"])} FROM {old}')
        new = spec["new"]
        lines.append(f"-- {old} → {new}  ({len(rows)} 行)")
        lines.extend(build_inserts(new, spec["cols"], rows, spec["json_cols"], "mysql"))
        lines.append("")
    await conn.close()

    out_dir = Path("db/migrate")
    out_dir.mkdir(exist_ok=True)
    (out_dir / "configs_mysql.sql").write_text("\n".join(lines), encoding="utf-8")
    print(f"导出完成: {out_dir/'configs_mysql.sql'}")
    for old, spec in MAPPING.items():
        print(f"  {old:16s} → {spec['new']:20s}")


asyncio.run(main())
