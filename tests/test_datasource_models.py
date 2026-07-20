import pytest
from sqlalchemy.exc import IntegrityError

from src.storage.models import (Datasource, MetadataTable, MetadataColumn,
                                TableRelation, BusinessRule, SqlTemplate)
from src.storage.pg_client import AsyncSessionFactory, init_db


@pytest.fixture
async def db():
    await init_db("sqlite+aiosqlite:///:memory:")
    yield


@pytest.mark.asyncio
async def test_datasource_crud_roundtrip(db):
    """建表 + 基本 CRUD 可用，密码字段可存可取。"""
    async with AsyncSessionFactory() as s:
        ds = Datasource(name="风电数仓", type="starrocks", host="10.0.0.1",
                        port=9030, db_name="dw", username="root",
                        password_enc="cipher", sync_scope="fact_,dim_")
        s.add(ds)
        await s.commit()
        assert ds.id is not None
    async with AsyncSessionFactory() as s:
        row = await s.get(Datasource, ds.id)
        assert row.name == "风电数仓"
        assert row.password_enc == "cipher"
        assert row.version == 1


@pytest.mark.asyncio
async def test_metadata_tables_and_columns(db):
    async with AsyncSessionFactory() as s:
        ds = Datasource(name="d", type="starrocks", host="h", port=1,
                        db_name="db", username="u", password_enc="c")
        s.add(ds); await s.flush()
        mt = MetadataTable(datasource_id=ds.id, table_name="fact_power",
                           table_comment="发电量", source="synced")
        s.add(mt); await s.flush()
        s.add(MetadataColumn(table_id=mt.id, column_name="kwh",
                             column_comment="度数", data_type="BIGINT",
                             is_primary=False, source="synced"))
        await s.commit()
    async with AsyncSessionFactory() as s:
        cols = (await s.execute(MetadataColumn.__table__.select())).all()
        assert len(cols) == 1
        assert cols[0].column_name == "kwh"


@pytest.mark.asyncio
async def test_other_three_tables_persist(db):
    """table_relations / business_rules / sql_templates 可写入读取。"""
    async with AsyncSessionFactory() as s:
        ds = Datasource(name="d2", type="starrocks", host="h", port=1,
                        db_name="db", username="u", password_enc="c")
        s.add(ds); await s.flush()
        s.add(TableRelation(datasource_id=ds.id, main_table="a", rel_table="b",
                            join_keys_json='[{"main":"a.id","rel":"b.aid"}]',
                            join_type="left", business_note="工单关联场站"))
        s.add(BusinessRule(category="metric", key="发电量",
                           value_json='{"unit":"kWh"}', enabled=True))
        s.add(SqlTemplate(datasource_id=ds.id, name="月发电量",
                          sql_template="SELECT * FROM fact_power WHERE month=:m",
                          params_json='[{"name":"m","required":true}]'))
        await s.commit()
    async with AsyncSessionFactory() as s:
        tr = (await s.execute(TableRelation.__table__.select())).one()
        assert tr.join_type == "left"
        assert tr.join_keys_json == '[{"main":"a.id","rel":"b.aid"}]'
        br = (await s.execute(BusinessRule.__table__.select())).one()
        assert br.value_json == '{"unit":"kWh"}'
        st = (await s.execute(SqlTemplate.__table__.select())).one()
        assert st.params_json == '[{"name":"m","required":true}]'


@pytest.mark.asyncio
async def test_metadata_table_enabled_defaults_false(db):
    async with AsyncSessionFactory() as s:
        ds = Datasource(name="d3", type="starrocks", host="h", port=1,
                        db_name="db", username="u", password_enc="c")
        s.add(ds); await s.flush()
        mt = MetadataTable(datasource_id=ds.id, table_name="t", source="synced")
        s.add(mt); await s.commit()
        assert mt.enabled is False


@pytest.mark.asyncio
async def test_datasource_unique_name(db):
    """同名数据源触发 unique 约束。"""
    async with AsyncSessionFactory() as s:
        s.add(Datasource(name="dup", type="starrocks", host="h", port=1,
                         db_name="db", username="u", password_enc="c"))
        await s.commit()
    async with AsyncSessionFactory() as s:
        s.add(Datasource(name="dup", type="starrocks", host="h", port=1,
                         db_name="db", username="u", password_enc="c"))
        with pytest.raises(IntegrityError):
            await s.commit()
