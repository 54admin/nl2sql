"""数据源管理：连接池（每 datasource 一个 AsyncEngine，懒建缓存）+ datasource CRUD。

双库边界：AsyncSessionFactory 连系统 PG（存元数据/配置）；
_engines 里的 engine 连业务库（StarRocks，查数用）。"""
from __future__ import annotations

from urllib.parse import quote_plus

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.logging import get_logger
from src.storage.models import Datasource
from src.storage.pg_client import AsyncSessionFactory

log = get_logger(__name__)

# update 可写字段白名单（trust boundary）。password_enc 不在内——
# 防请求体直接传 {"password_enc":"..."} 改密码；密码只走 password。
# 注：密码明文存 password_enc（内网工具，去加密简化，不再依赖 NL2SQL_DS_KEY）。
_DS_WRITABLE = {"name", "type", "host", "port", "db_name",
                "username", "sync_scope", "enabled"}


class DataSourceManager:
    def __init__(self) -> None:
        self._engines: dict[int, AsyncEngine] = {}

    # ---- 连接池 ----
    def _build_engine(self, row: Datasource) -> AsyncEngine:
        pwd = row.password_enc   # 明文存（内网工具去加密）
        # 用户名/密码 quote_plus，防 @:/ 等字符破坏 URL 解析（同 pg_client 范式）
        url = (f"mysql+aiomysql://{quote_plus(row.username)}:{quote_plus(pwd)}"
               f"@{row.host}:{row.port}/{row.db_name}")
        return create_async_engine(url, pool_pre_ping=True)

    async def get_engine(self, ds_id: int) -> AsyncEngine:
        """懒建 + 缓存。miss 则读表解密建 engine。"""
        if ds_id in self._engines:
            return self._engines[ds_id]
        async with AsyncSessionFactory() as s:
            row = await s.get(Datasource, ds_id)
        if row is None or not row.enabled:
            raise KeyError(f"数据源不存在或未启用: {ds_id}")
        eng = self._build_engine(row)
        self._engines[ds_id] = eng
        return eng

    async def test_connection(self, ds_id: int) -> None:
        """SELECT 1 探活。失败抛异常（路由层 catch）。"""
        from sqlalchemy import text
        eng = await self.get_engine(ds_id)
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def get_sync_scope(self, ds_id: int) -> str | None:
        """读数据源的 sync_scope。不存在抛 KeyError（与 get_engine 语义一致）。
        注意 sync_scope 合法可为 None（=全要），不能用 None 哨兵表示不存在。"""
        async with AsyncSessionFactory() as s:
            row = await s.get(Datasource, ds_id)
        if row is None:
            raise KeyError(f"数据源不存在: {ds_id}")
        return row.sync_scope

    # ---- CRUD（只操作系统 PG，不碰业务库）----
    async def list_datasources(self) -> list[dict]:
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(Datasource.__table__.select())).all()
        return [{"id": r.id, "name": r.name, "type": r.type, "host": r.host,
                 "port": r.port, "db_name": r.db_name, "username": r.username,
                 "sync_scope": r.sync_scope, "enabled": r.enabled} for r in rows]

    async def create_datasource(self, data: dict) -> int:
        ds = Datasource(password_enc=data.pop("password"), **data)
        async with AsyncSessionFactory() as s:
            s.add(ds)
            await s.commit()
        log.info("数据源创建 id=%s name=%s", ds.id, ds.name)
        return ds.id

    async def update_datasource(self, ds_id: int, data: dict) -> bool:
        # 合法改密走这条；password_enc 永远不能由调用方直接传（trust boundary）
        new_pwd = data.pop("password") if "password" in data else None
        async with AsyncSessionFactory() as s:
            row = await s.get(Datasource, ds_id)
            if row is None:
                return False
            for k, v in data.items():
                if k in _DS_WRITABLE:
                    setattr(row, k, v)
            if new_pwd is not None:
                row.password_enc = new_pwd   # 明文存
            row.version += 1
            await s.commit()
        log.info("数据源更新 id=%s fields=%s", ds_id, sorted(_DS_WRITABLE & data.keys()))
        # 连接信息可能变了，旧 engine 失效
        old = self._engines.pop(ds_id, None)
        if old is not None:
            await old.dispose()
        return True

    async def delete_datasource(self, ds_id: int) -> bool:
        async with AsyncSessionFactory() as s:
            row = await s.get(Datasource, ds_id)
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
        log.info("数据源删除 id=%s", ds_id)
        old = self._engines.pop(ds_id, None)
        if old is not None:
            await old.dispose()
        return True
