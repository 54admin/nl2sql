import textwrap

from src.config import load_config


def test_load_config_merges_profile(tmp_path):
    (tmp_path / "application.yml").write_text(textwrap.dedent("""
        app:
          name: base
        profiles:
          active: dev
        llm:
          api_key: ""
          api_base: http://x/v1
          model: base-model
        redis:
          host: base-redis
        postgres:
          database: base-db
    """))
    (tmp_path / "application-dev.yml").write_text("app:\n  name: dev-name\n")

    cfg = load_config(str(tmp_path))

    assert cfg.app.name == "dev-name"          # profile 覆盖
    assert cfg.llm.model == "base-model"        # 基线保留
    assert cfg.redis.host == "base-redis"
    assert cfg.profiles == ["dev"]


def test_load_config_env_override(tmp_path, monkeypatch):
    (tmp_path / "application.yml").write_text(
        "llm:\n  api_key: \"\"\n  api_base: http://x/v1\n  model: m\n"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    cfg = load_config(str(tmp_path), profile=None)
    assert cfg.llm.api_key == "sk-from-env"


def test_load_config_nested_deep_merge(tmp_path):
    """dev profile 只覆盖 llm.api_key，base 的 llm.timeout 等应保留。"""
    (tmp_path / "application.yml").write_text(
        "profiles:\n  active: dev\n"
        "llm:\n  api_key: base-key\n  api_base: http://base/v1\n  timeout: 99\n"
    )
    (tmp_path / "application-dev.yml").write_text(
        "llm:\n  api_key: dev-key\n"
    )
    cfg = load_config(str(tmp_path))
    assert cfg.llm.api_key == "dev-key"          # 被覆盖
    assert cfg.llm.api_base == "http://base/v1"  # 保留
    assert cfg.llm.timeout == 99                 # 嵌套字段保留


def test_load_config_missing_profile_warns(tmp_path, caplog):
    """声明的 profile 文件不存在时应 warning 且不崩溃。"""
    import logging
    (tmp_path / "application.yml").write_text(
        "profiles:\n  active: prod\n"
        "llm:\n  api_key: k\n  api_base: http://x/v1\n  model: m\n"
    )
    with caplog.at_level(logging.WARNING, logger="nl2sql.src.config"):
        cfg = load_config(str(tmp_path))
    assert cfg.llm.model == "m"                  # 仍能加载 base
    assert any("profile 文件不存在" in r.message for r in caplog.records)

