"""查询结果旁路：全量结果存 PG（审计/持久）+ Redis（TTL 快速取），分配 result_id。
execute_sql 执行后调 save_result 存全量，只把摘要回灌 LLM；前端按 result_id 取全量。"""
import json
from uuid import uuid4

from src.config import load_config
from src.logging import get_logger
from src.storage.models import QueryResult
from src.storage.pg_client import AsyncSessionFactory
from src.storage.redis_client import RedisClient

log = get_logger(__name__)

RESULT_TTL = 3600  # Redis TTL，1 小时

# redis key 加项目根前缀，与会话侧统一（树形工具按 : 分层显示成 nl2sql/ 根下多域）。
REDIS_ROOT = "nl2sql"
RESULT_KEY = f"{REDIS_ROOT}:result:{{rid}}"

# 模块级懒加载单例。connect() 内部失败会自动降级到 _InMemory 后端。
# ponytail: 全进程一个 client 够用；多实例/连接池等吞吐瓶颈再换。
_redis: RedisClient | None = None


async def _get_redis() -> RedisClient | None:
    """返回 Redis 客户端单例；初始化失败返回 None（主链路走 PG 兜底）。
    RedisClient.connect 内部已吞连接异常降级内存后端，这里只兜其他意外。"""
    global _redis
    if _redis is None:
        try:
            cfg = load_config()
            client = RedisClient(cfg.redis)
            await client.connect()
            _redis = client
        except Exception as e:
            log.warning("Redis 初始化失败，result 旁路走 PG: %s", e)
            return None
    return _redis


async def save_result(session_id: str, columns: list, rows: list,
                      datasource_id: int | None = None) -> str:
    """全量结果存 PG query_results + Redis，返回 result_id。

    datasource_id 当前不持久化（QueryResult 暂无此字段），参数留位，
    P1b-5 集成 execute_sql 时按需给 ORM 加字段再补写。
    """
    result_id = uuid4().hex
    # default=str：StarRocks 数值列返 Decimal、时间列返 datetime，json.dumps 默认不认。
    # 兜底转 str 保住值不丢精度（全量旁路用于展示/审计，str 足够）。
    columns_json = json.dumps(columns, ensure_ascii=False, default=str)
    rows_json = json.dumps(rows, ensure_ascii=False, default=str)

    # PG：审计/持久，必成功（失败直接抛，主链路该挂就挂）
    async with AsyncSessionFactory() as s:
        s.add(QueryResult(result_id=result_id, session_id=session_id,
                          columns_json=columns_json, rows_json=rows_json,
                          total=len(rows)))
        await s.commit()

    # Redis：TTL 快速取，旁路非关键，挂了不影响主链路（PG 兜底）
    payload = json.dumps({"columns": columns, "rows": rows,
                          "total": len(rows)}, ensure_ascii=False, default=str)
    try:
        r = await _get_redis()
        if r is not None:
            await r.set(RESULT_KEY.format(rid=result_id), payload, ttl=RESULT_TTL)
    except Exception as e:
        log.warning("result 写 Redis 失败（result_id=%s），PG 已兜底: %s",
                    result_id, e)

    return result_id


async def get_result(result_id: str) -> dict | None:
    """取全量结果：Redis 优先，miss/异常回 PG。返回 {columns, rows, total} 或 None。"""
    # 1. Redis 优先（任何异常跳过走 PG）
    try:
        r = await _get_redis()
        if r is not None:
            raw = await r.get(RESULT_KEY.format(rid=result_id))
            if raw:
                return json.loads(raw)
    except Exception as e:
        log.warning("result 读 Redis 失败，回退 PG: %s", e)

    # 2. miss 或 Redis 异常 → PG
    async with AsyncSessionFactory() as s:
        qr = await s.get(QueryResult, result_id)
        if qr is None:
            return None
        return {"columns": json.loads(qr.columns_json),
                "rows": json.loads(qr.rows_json),
                "total": qr.total}
