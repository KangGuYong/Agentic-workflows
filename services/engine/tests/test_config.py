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


def test_the_allowlist_is_parsed_at_startup(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("HTTP_ALLOWLIST", "https://api.example.com, http://10.0.0.7:8080;allowPrivate")

    config = load_config()

    assert [entry.host for entry in config.http_allowlist] == ["api.example.com", "10.0.0.7"]


def test_a_malformed_allowlist_refuses_to_start(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("HTTP_ALLOWLIST", "http://api.example.com:443")

    with pytest.raises(ConfigError):
        load_config()


def test_no_allowlist_means_everything_is_blocked(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.delenv("HTTP_ALLOWLIST", raising=False)
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)

    assert load_config().http_allowlist == ()
