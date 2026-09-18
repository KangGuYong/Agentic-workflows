import pytest

from engine.config import ConfigError, load_config


@pytest.fixture(autouse=True)
def _required_secrets(monkeypatch):
    """Every valid config needs ENGINE_SECRET_KEY and ENGINE_API_TOKEN, so they are supplied here and each
    test below stays about the one setting its name mentions. The tests that are about these two override
    them themselves."""
    monkeypatch.setenv("ENGINE_SECRET_KEY", "1" * 32)
    monkeypatch.setenv("ENGINE_API_TOKEN", "dev-token-0123456789")


def test_config_reads_the_environment(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("ENGINE_REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("ENGINE_API_TOKEN", "t0ken-0123456789")
    monkeypatch.setenv("WORKER_MAX_RUNS", "3")

    config = load_config()

    assert (config.database_url, config.api_token, config.worker_max_runs) == (
        "postgresql://u:p@h/db", "t0ken-0123456789", 3
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


def test_a_missing_secret_key_is_refused_unless_dev_insecure(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.delenv("ENGINE_SECRET_KEY", raising=False)

    with pytest.raises(ConfigError):
        load_config()

    monkeypatch.setenv("ENGINE_DEV_INSECURE", "1")
    assert load_config().secret_key is None


@pytest.mark.parametrize("value", ["too-short", "0" * 31, "0" * 33])
def test_a_secret_key_of_the_wrong_length_is_refused(monkeypatch, value):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("ENGINE_SECRET_KEY", value)

    with pytest.raises(ConfigError):
        load_config()


@pytest.mark.parametrize("length", [16, 24, 32])
def test_every_aes_key_length_is_accepted(monkeypatch, length):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("ENGINE_SECRET_KEY", "0" * length)

    assert load_config().secret_key == b"0" * length


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


def test_a_missing_api_token_is_refused_unless_dev_insecure(monkeypatch):
    """An engine with no token serves every workflow, run and secret name to anyone who can reach the
    port. Until now an unset token was only a warning."""
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.delenv("ENGINE_API_TOKEN", raising=False)
    monkeypatch.delenv("ENGINE_DEV_INSECURE", raising=False)

    with pytest.raises(ConfigError):
        load_config()

    monkeypatch.setenv("ENGINE_DEV_INSECURE", "1")
    assert load_config().api_token is None


def test_an_empty_api_token_is_the_same_as_none(monkeypatch):
    """`ENGINE_API_TOKEN=` in a compose file is a common way to "unset" it, and it must not read as a
    configured token of zero length that every request then has to match."""
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("ENGINE_API_TOKEN", "")
    monkeypatch.delenv("ENGINE_DEV_INSECURE", raising=False)

    with pytest.raises(ConfigError):
        load_config()

    # And in development it is "no token", not a zero-length one that fails the length rule: an empty
    # variable in a compose file has to behave exactly like an absent one.
    monkeypatch.setenv("ENGINE_DEV_INSECURE", "1")
    assert load_config().api_token is None


@pytest.mark.parametrize("token", ["short", "x" * 15])
def test_a_token_that_is_too_short_is_refused(monkeypatch, token):
    """A 'token' someone can guess is the same as no token."""
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("ENGINE_API_TOKEN", token)

    with pytest.raises(ConfigError):
        load_config()


def test_a_token_at_the_minimum_length_is_accepted(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("ENGINE_API_TOKEN", "x" * 16)

    assert load_config().api_token == "x" * 16


def test_a_short_token_is_refused_even_in_development(monkeypatch):
    """Dev mode is permission to run *without* a token, not permission to run with a guessable one: a
    weak token left in a compose file is what gets promoted to production."""
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("ENGINE_API_TOKEN", "short")
    monkeypatch.setenv("ENGINE_DEV_INSECURE", "1")

    with pytest.raises(ConfigError):
        load_config()
