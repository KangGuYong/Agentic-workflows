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


def test_the_http_limits_take_their_documented_defaults(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.delenv("HTTP_MAX_REDIRECTS", raising=False)
    monkeypatch.delenv("HTTP_MAX_REQUEST_BYTES", raising=False)
    monkeypatch.delenv("HTTP_MAX_RESPONSE_BYTES", raising=False)

    config = load_config()

    assert config.http_max_redirects == 3
    assert config.http_max_request_bytes == 1_000_000
    assert config.http_max_response_bytes == 5_000_000


@pytest.mark.parametrize(("name", "value"), [
    ("HTTP_MAX_REDIRECTS", "-1"),           # below the 0..10 range
    ("HTTP_MAX_REDIRECTS", "11"),           # above it
    ("HTTP_MAX_REQUEST_BYTES", "0"),        # a 0-byte cap makes every request fail, not "unbounded"
    ("HTTP_MAX_REQUEST_BYTES", "100000001"),  # above the 100 MB ceiling
    ("HTTP_MAX_RESPONSE_BYTES", "0"),
    ("HTTP_MAX_RESPONSE_BYTES", "100000001"),
])
def test_an_out_of_range_http_limit_refuses_to_start(monkeypatch, name, value):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError):
        load_config()


@pytest.mark.parametrize(("name", "value"), [
    ("HTTP_MAX_REDIRECTS", "0"),             # the deliberate choice: follow none -- not an error
    ("HTTP_MAX_REDIRECTS", "10"),
    ("HTTP_MAX_REQUEST_BYTES", "1"),
    ("HTTP_MAX_REQUEST_BYTES", "100000000"),
    ("HTTP_MAX_RESPONSE_BYTES", "1"),
    ("HTTP_MAX_RESPONSE_BYTES", "100000000"),
])
def test_a_boundary_http_limit_is_accepted(monkeypatch, name, value):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv(name, value)

    config = load_config()

    field = {"HTTP_MAX_REDIRECTS": "http_max_redirects", "HTTP_MAX_REQUEST_BYTES": "http_max_request_bytes",
              "HTTP_MAX_RESPONSE_BYTES": "http_max_response_bytes"}[name]
    assert getattr(config, field) == int(value)
