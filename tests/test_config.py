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
