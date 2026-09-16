import pytest

from engine.config import ConfigError, load_config


def test_config_reads_the_environment(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("ENGINE_REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("ENGINE_API_TOKEN", "t0ken")
    monkeypatch.setenv("WORKER_MAX_RUNS", "3")

    config = load_config()

    assert (config.database_url, config.api_token, config.worker_max_runs) == (
        "postgresql://u:p@h/db", "t0ken", 3
    )
    assert config.lease_sec == 30 and config.heartbeat_sec == 10


def test_missing_encryption_key_is_refused_unless_dev_insecure(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("ENGINE_REDIS_URL", "redis://h:6379/0")
    monkeypatch.delenv("LANGGRAPH_AES_KEY", raising=False)

    with pytest.raises(ConfigError):
        load_config()

    monkeypatch.setenv("ENGINE_DEV_INSECURE", "1")
    assert load_config().encrypt_checkpoints is False


def test_a_missing_database_url_is_an_error(monkeypatch):
    monkeypatch.delenv("ENGINE_DATABASE_URL", raising=False)
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    with pytest.raises(ConfigError):
        load_config()
