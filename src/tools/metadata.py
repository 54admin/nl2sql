"""query_metadata 工具：返回某数据源里 enabled=True 的表清单（表名/注释 + 实时拉的字段）+ 已配逻辑关联，
供 LLM 选表 / 生成多表 JOIN。白名单衔接配置页：只有勾选参与的表才返回。

PG metadata_tables 只存勾选白名单（enabled=true）+ 手写注释（source=manual）。
这里读 enabled=true 的表，对每张实时连业务库 fetch_table_columns 拉字段（白名单表少，实时拉快）。"""
from __future__ import annotations

import asyncio
import json
import re
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.types import CancelToken, LoopContext, ToolDefinition, ToolResult
from src.datasource.metadata_sync import fetch_table_columns
from src.storage.models import (BusinessRule, MetadataColumn, MetadataTable,
                                TableRelation)
from src.storage.db_client import AsyncSessionFactory

# 业务库字段拉取缓存：key=(ds_id, schema, table) → (拉取时刻, 原始列清单)。
# manual/hidden 过滤在缓存外应用（后台改配置立即生效）；缓存只省 information_schema 往返。
_COL_CACHE: dict[tuple[int, "str | None", str], tuple[float, list[dict]]] = {}
_COL_CACHE_TTL = 300.0   # 对齐 sql_engine._COL_CACHE 的 300s


async def _cached_columns(datasource_id: int, engine: AsyncEngine,
                          table: str, schema: str | None) -> list[dict]:
    """字段拉取 + TTL 缓存（miss 才连业务库）。返回浅拷贝，防调用方改写污染缓存。"""
    key = (datasource_id, schema, table)
    now = time.monotonic()
    hit = _COL_CACHE.get(key)
    if hit and now - hit[0] < _COL_CACHE_TTL:
        return [dict(c) for c in hit[1]]
    cols = await fetch_table_columns(engine, table, schema)
    stale = [k for k, v in _COL_CACHE.items() if now - v[0] >= _COL_CACHE_TTL]
    for k in stale:
        _COL_CACHE.pop(k, None)
    _COL_CACHE[key] = (now, cols)
    return [dict(c) for c in cols]


async def _list_enabled_tables(datasource_id: int, engine: AsyncEngine) -> list[dict]:
    """读 enabled=True 的表（表名+注释），拉每张表的字段。
    schema_name 非空时：table_name 给 `schema.table` 全限定名（execute_sql 直接拿用），
    fetch_table_columns 也按该 schema 拉字段。空时老行为（裸表名）。
    表/字段的手写业务表述（source=manual，nl_md_columns）优先于库内注释；
    表行 hidden_columns_json 勾掉的字段不返回（Agent 看不到，宽表降噪）。
    表级 asyncio.gather 并发（含各自的维度抽样）——串行时 N 表 × M 列抽样最坏几十秒，
    是 query_metadata 的主要耗时。白名单表 ≤10，连接池（5+10）兜得住。"""
    async with AsyncSessionFactory() as s:
        tables = (await s.execute(MetadataTable.__table__.select().where(
            MetadataTable.datasource_id == datasource_id,
            MetadataTable.enabled.is_(True)))).all()
        manual_cols: dict[int, dict[str, str]] = {}
        if tables:
            col_rows = (await s.execute(MetadataColumn.__table__.select().where(
                MetadataColumn.table_id.in_([t.id for t in tables]),
                MetadataColumn.source == "manual"))).all()
            for c in col_rows:
                manual_cols.setdefault(c.table_id, {})[c.column_name] = c.column_comment or ""

    async def _one(t) -> dict:
        cols = await _cached_columns(datasource_id, engine, t.table_name, t.schema_name)
        manual = manual_cols.get(t.id, {})
        try:
            hidden = set(json.loads(t.hidden_columns_json or "[]"))
        except Exception:
            hidden = set()
        cols = [{**c, "comment": manual.get(c["name"]) or c.get("comment") or ""}
                for c in cols if c["name"] not in hidden]
        full_name = f"{t.schema_name}.{t.table_name}" if t.schema_name else t.table_name
        cols = await _enrich_columns(engine, t.schema_name, t.table_name, cols)
        return {
            "table_name": full_name,
            "table_comment": t.table_comment or "",
            "columns": cols,
        }

    return list(await asyncio.gather(*(_one(t) for t in tables)))


async def _list_relations(datasource_id: int) -> list[dict]:
    """读已配逻辑关联（table_relations）。join_keys_json 解成 list。
    ponytail: 本期只消费已配关联；无配置时 LLM 依字段注释自行推导 JOIN + 标 inferred 转正留后续。"""
    async with AsyncSessionFactory() as s:
        rels = (await s.execute(TableRelation.__table__.select().where(
            TableRelation.datasource_id == datasource_id))).all()
        return [{
            "main_table": r.main_table,
            "rel_table": r.rel_table,
            "join_keys": json.loads(r.join_keys_json),
            "join_type": r.join_type,
            "business_note": r.business_note or "",
        } for r in rels]


async def _list_table_rules() -> dict[str, list[str]]:
    """读表级业务规则（enabled），按 table_name 分组返回文本列表。
    供 query_metadata 附在对应表上——查该表时 LLM 才看到，不污染全局 prompt。
    本表只存表级规则（table_name 必填）；通用业务口径如需进提示词，直接在 admin 后台改对应 skill 提示词（DB prompts 表）。"""
    async with AsyncSessionFactory() as s:
        rows = (await s.execute(BusinessRule.__table__.select().where(
            BusinessRule.enabled.is_(True)))).all()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r.table_name, []).append(f"{r.key}: {r.value_json}")
    return out


# ---- 列分类（指标/维度）+ 维度抽样 ----
# 维度列抽样真实取值，让 LLM 不必再 SELECT DISTINCT 试探（审计实证：一次问数会因此多跑 3 次 SQL）。
_DIM_NUMERIC_HINT = re.compile(
    r"(year|month|quarter|week|period|rank|flag|category|kind|level|status|"
    r"类型|状态|级别|等级|排名|期|类别|分类|是否)", re.I)
_ID_HINT = re.compile(r"(^id$|_id$|_no$|^code$|guid$|uuid)", re.I)
_FREE_TEXT_LEN = 200     # varchar 长度>=此值视为自由文本，不抽样（基数太高无意义）
_SAMPLE_LIMIT = 10


def _base_type_and_len(type_str: str) -> tuple[str, int | None]:
    """varchar(50)->('varchar',50)；decimal(18,6)->('decimal',18)；datetime->('datetime',None)。"""
    m = re.match(r"\s*([A-Za-z]+)(?:\s*\((\d+))?", type_str or "")
    if not m:
        return (type_str or "").lower(), None
    return m.group(1).lower(), (int(m.group(2)) if m.group(2) else None)


def _classify(name: str, comment: str, type_str: str) -> tuple[str, bool]:
    """列 -> (role, 是否抽样)。role: metric(指标)/dimension(维度)。
    只有「短分类列」抽样：id/长文本/时间戳是维度但不抽样（高基数或无意义）。"""
    base, length = _base_type_and_len(type_str)
    nl = (name or "").lower()
    is_int = base in ("int", "bigint", "smallint", "tinyint", "mediumint")
    is_float = base in ("decimal", "numeric", "float", "double", "real", "number")
    is_str = base in ("varchar", "char", "text", "string", "enum", "set",
                      "longtext", "mediumtext", "tinytext")
    is_time = base in ("datetime", "timestamp", "date", "time", "year")
    if _ID_HINT.search(nl):
        return "dimension", False          # 标识符，抽样无意义
    if is_time:
        return "dimension", False          # 时间戳类（多为 ETL 审计列）
    if is_str:
        if base == "text" or (length and length >= _FREE_TEXT_LEN):
            return "dimension", False      # 自由文本，基数太高
        return "dimension", True           # 短分类列 -> 抽样
    if is_float:
        return "metric", False             # 小数=连续度量，恒为指标（不可能是分类维度）
    if is_int:
        if _DIM_NUMERIC_HINT.search(name) or _DIM_NUMERIC_HINT.search(comment or ""):
            return "dimension", True       # 整数型分类码：年/月/类型码/排名位次（如 rank=1..5）
        return "metric", False
    return "dimension", False


async def _enrich_columns(engine: AsyncEngine, schema: str | None, table: str,
                          cols: list[dict]) -> list[dict]:
    """给每列打 role(metric/dimension)，并对维度短分类列抽样真实取值(samples)。
    抽样失败的单列不影响整体。标识符按 dialect 正确引用（mysql 反引号/pg 双引号）。"""
    preparer = engine.dialect.identifier_preparer
    tq = preparer.quote_identifier(table)
    if schema:
        tq = preparer.quote_identifier(schema) + "." + tq
    enriched: list[dict] = []
    to_sample: list[str] = []
    for c in cols:
        role, sample_it = _classify(c["name"], c.get("comment", ""), c.get("type", ""))
        enriched.append({"name": c["name"], "comment": c.get("comment", ""),
                         "type": c.get("type", ""), "role": role})
        if sample_it:
            to_sample.append(c["name"])
    if to_sample:
        async with engine.connect() as conn:
            for col in to_sample:
                try:
                    r = await conn.execute(text(
                        f"SELECT DISTINCT {preparer.quote_identifier(col)} FROM {tq} "
                        f"WHERE {preparer.quote_identifier(col)} IS NOT NULL "
                        f"ORDER BY {preparer.quote_identifier(col)} LIMIT {_SAMPLE_LIMIT}"))
                    # 过滤 None/空串（NULL 已排除，空串兜底），转 str 统一类型
                    vals = [str(v) for v in (row[0] for row in r.fetchall()) if v not in (None, "")]

                except Exception:
                    vals = None     # 抽样失败就略过，不阻塞元数据返回
                if vals is not None:
                    next(e for e in enriched if e["name"] == col)["samples"] = vals
    return enriched


async def query_metadata(args: dict, ctx: LoopContext,
                         cancel_token: CancelToken) -> ToolResult:
    """工具 handler。args 可带 datasource_id；缺省取第一个数据源（单源场景；多源选择留后续）。
    返回 {tables:[...], relations:[...]}：tables=白名单表（字段实时拉），relations=已配 JOIN 口径。"""
    from src.datasource.manager import get_manager
    ds_id = args.get("datasource_id")
    mgr = get_manager()
    if ds_id is None:
        rows = await mgr.list_datasources()
        if not rows:
            return ToolResult(summary="无可用数据源，请先在配置页添加。")
        ds_id = rows[0]["id"]
    engine = await mgr.get_engine(int(ds_id))
    tables = await _list_enabled_tables(int(ds_id), engine)
    if not tables:
        return ToolResult(summary="该数据源没有勾选参与问数的表，请在配置页勾选表后再问。")
    relations = await _list_relations(int(ds_id))
    table_rules = await _list_table_rules()
    for t in tables:
        t["rules"] = table_rules.get(t["table_name"], [])
    return ToolResult(summary=json.dumps({"tables": tables, "relations": relations},
                                         ensure_ascii=False, default=str))


QUERY_METADATA = ToolDefinition(
    name="query_metadata",
    description=("查看当前数据源里可以查询的表清单（表名/中文注释/字段）、已配表间关联。"
                 "先调它了解有哪些表，再决定查哪张表。无需参数。"
                 "每列带 role(metric指标/dimension维度)；维度短分类列已附 samples 真实取值，"
                 "直接用、别再 SELECT DISTINCT 试探。"
                 "只服务于 execute_sql 查业务数据前的结构准备；查知识库文档不需要调本工具。"),
    parameters={"type": "object", "properties": {}, "required": []},
    handler=query_metadata,
)
