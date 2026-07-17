import textwrap

from src.config import load_config


def test_load_config_merges_profile(tmp_path):
    (tmp_path / "application.yml").write_text(textwrap.dedent("""
        app:
          name: base
        profiles:
          active: dev
        redis:
          host: base-redis
        postgres:
          database: base-db
    """))
    (tmp_path / "application-dev.yml").write_text("app:\n  name: dev-name\n")

    cfg = load_config(str(tmp_path))

    assert cfg.app.name == "dev-name"          # profile 覆盖
    assert cfg.redis.host == "base-redis"       # 基线保留
    assert cfg.profiles == ["dev"]


def test_load_config_nested_deep_merge(tmp_path):
    """dev profile 只覆盖 redis.host，base 的 redis.port 应保留。"""
    (tmp_path / "application.yml").write_text(
        "profiles:\n  active: dev\n"
        "redis:\n  host: base-host\n  port: 6379\n"
    )
    (tmp_path / "application-dev.yml").write_text(
        "redis:\n  host: dev-host\n"
    )
    cfg = load_config(str(tmp_path))
    assert cfg.redis.host == "dev-host"     # 被覆盖
    assert cfg.redis.port == 6379           # 嵌套字段保留


def test_load_config_missing_profile_warns(tmp_path, caplog):
    """声明的 profile 文件不存在时应 warning 且不崩溃。"""
    import logging
    (tmp_path / "application.yml").write_text(
        "profiles:\n  active: prod\n"
        "postgres:\n  database: db\n"
    )
    with caplog.at_level(logging.WARNING, logger="nl2sql.src.config"):
        cfg = load_config(str(tmp_path))
    assert cfg.postgres.database == "db"
    assert any("profile 文件不存在" in r.message for r in caplog.records)
