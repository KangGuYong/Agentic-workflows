"""The http_request node (2b design §3, §5.6, §6 layer 2).

This is the only place a secret exists in plaintext, so most of what is pinned here is about what does
*not* come back out: markers become values immediately before sending, and the values are scrubbed from
everything the node returns, including the message of every error it raises.
"""
import json

import pytest

from engine.errors import EngineFault, ErrorCode, NodeError
from engine.http.client import EgressBlocked, ResponseTooLarge, TransportFailed, UnsupportedMedia
from engine.nodes.base import HttpResponse, NodeContext
from engine.nodes.http_request import HttpRequestNode
from engine.secrets.markers import marker_for

NONCE = "0123456789abcdef"
SECRET = "hunter2-secret-value"


class FakeHttp:
    def __init__(self, response=None, error=None) -> None:
        self.response = response or HttpResponse(200, {"content-type": "application/json"}, {"ok": True})
        self.error = error
        self.calls: list[dict] = []

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeSecrets:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.asked: list[set[str]] = []

    async def resolve(self, names: set[str]) -> dict[str, str]:
        self.asked.append(set(names))
        return {name: self.values[name] for name in names if name in self.values}


def _ctx(http=None, secrets=None, nonce: str | None = NONCE) -> NodeContext:
    return NodeContext(run_id="run-1", node_id="http_1", exec_index=1, attempt=1, inputs={}, outputs={},
                       pred_ids=[], llm=None, http=http, secrets=secrets, secret_nonce=nonce,
                       timeout_sec=30)


def _config(**overrides):
    raw = {"method": "GET", "url": "https://api.example.com/x", "headers": {}}
    raw.update(overrides)
    return HttpRequestNode().parse_config(raw)


async def _run(config, rendered, *, http=None, secrets=None, nonce=NONCE):
    return await HttpRequestNode().execute(_ctx(http, secrets, nonce), config, rendered)


async def test_a_plain_request_returns_status_headers_and_body():
    http = FakeHttp()

    result = await _run(_config(), {"url": "https://api.example.com/x"}, http=http)

    assert result.output == {"status": 200, "headers": {"content-type": "application/json"},
                             "body": {"ok": True}}
    assert http.calls[0]["method"] == "GET"


async def test_markers_are_replaced_only_at_the_moment_of_sending():
    http = FakeHttp()
    secrets = FakeSecrets({"API_TOKEN": SECRET})
    rendered = {"url": "https://api.example.com/x",
                "headers.Authorization": f"Bearer {marker_for('API_TOKEN', NONCE)}"}

    await _run(_config(headers={"Authorization": "Bearer {{ secret.API_TOKEN }}"}), rendered,
               http=http, secrets=secrets)

    assert secrets.asked == [{"API_TOKEN"}]
    assert http.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert marker_for("API_TOKEN", NONCE) in rendered["headers.Authorization"]  # the record is untouched


async def test_no_secret_reference_means_the_resolver_is_never_asked():
    """The common case must not cost a database round trip per request."""
    secrets = FakeSecrets({"API_TOKEN": SECRET})

    await _run(_config(), {"url": "https://api.example.com/x"}, http=FakeHttp(), secrets=secrets)

    assert secrets.asked == []


async def test_a_marker_from_another_run_is_neither_resolved_nor_substituted():
    """Tenant text, or text carried over from another run, must not make this node fetch a secret."""
    http = FakeHttp()
    secrets = FakeSecrets({"API_TOKEN": SECRET})
    foreign = marker_for("API_TOKEN", "f" * 16)

    await _run(_config(), {"url": f"https://api.example.com/{foreign}"}, http=http, secrets=secrets)

    assert secrets.asked == []
    assert http.calls[0]["url"] == f"https://api.example.com/{foreign}"


async def test_a_secret_echoed_back_by_the_server_is_redacted():
    http = FakeHttp(HttpResponse(200, {"content-type": "application/json"},
                                 {"echo": f"token={SECRET}", "nested": [{"v": f"xx{SECRET}yy"}]}))
    secrets = FakeSecrets({"API_TOKEN": SECRET})
    rendered = {"url": f"https://api.example.com/{marker_for('API_TOKEN', NONCE)}"}

    result = await _run(_config(url="https://api.example.com/{{ secret.API_TOKEN }}"), rendered,
                        http=http, secrets=secrets)

    assert SECRET not in json.dumps(result.output, ensure_ascii=False)
    assert result.output["body"]["echo"] == "token=[REDACTED]"
    assert result.output["body"]["nested"][0]["v"] == "xx[REDACTED]yy"


async def test_a_secret_echoed_in_a_response_header_is_redacted_too():
    """Header-name redaction only covers known credential names; a secret reflected into any other
    header (a request-id echo, a Location) is caught by value redaction or not at all."""
    http = FakeHttp(HttpResponse(200, {"content-type": "text/plain", "x-echo": f"got {SECRET}"}, "ok"))
    secrets = FakeSecrets({"API_TOKEN": SECRET})
    rendered = {"url": f"https://api.example.com/{marker_for('API_TOKEN', NONCE)}"}

    result = await _run(_config(url="{{ secret.API_TOKEN }}"), rendered, http=http, secrets=secrets)

    assert result.output["headers"]["x-echo"] == "got [REDACTED]"


async def test_credential_headers_are_redacted_by_name_even_without_a_secret():
    http = FakeHttp(HttpResponse(200, {"set-cookie": "session=abc", "content-type": "text/plain"}, "ok"))

    result = await _run(_config(), {"url": "https://api.example.com/x"}, http=http)

    assert result.output["headers"]["set-cookie"] == "[REDACTED]"


async def test_a_missing_secret_fails_the_attempt_without_retrying():
    secrets = FakeSecrets({})
    rendered = {"url": f"https://api.example.com/{marker_for('API_TOKEN', NONCE)}"}

    with pytest.raises(NodeError) as exc:
        await _run(_config(url="{{ secret.API_TOKEN }}"), rendered, http=FakeHttp(), secrets=secrets)

    assert exc.value.code == ErrorCode.SECRET_NOT_FOUND and exc.value.retryable is False


async def test_a_missing_secret_stops_the_request_from_being_sent():
    """Sending the marker itself would put a workflow's secret names on the wire."""
    http = FakeHttp()

    with pytest.raises(NodeError):
        await _run(_config(url="{{ secret.API_TOKEN }}"),
                   {"url": f"https://api.example.com/{marker_for('API_TOKEN', NONCE)}"},
                   http=http, secrets=FakeSecrets({}))

    assert http.calls == []


@pytest.mark.parametrize(("status", "retryable"), [(404, False), (401, False), (429, True), (500, True),
                                                   (503, True)])
async def test_a_non_2xx_response_is_a_node_error(status, retryable):
    http = FakeHttp(HttpResponse(status, {"content-type": "text/plain"}, "nope"))

    with pytest.raises(NodeError) as exc:
        await _run(_config(), {"url": "https://api.example.com/x"}, http=http)

    assert exc.value.code == ErrorCode.HTTP_ERROR and exc.value.retryable is retryable
    assert str(status) in exc.value.message


async def test_an_error_response_that_echoes_the_secret_does_not_leak_it():
    """The failure path is the one people forget: a 500 body quoting the credential it rejected."""
    http = FakeHttp(HttpResponse(500, {"content-type": "text/plain"}, f"bad token {SECRET}"))
    secrets = FakeSecrets({"API_TOKEN": SECRET})

    with pytest.raises(NodeError) as exc:
        await _run(_config(url="{{ secret.API_TOKEN }}"),
                   {"url": f"https://api.example.com/{marker_for('API_TOKEN', NONCE)}"},
                   http=http, secrets=secrets)

    assert SECRET not in exc.value.message


@pytest.mark.parametrize(("error", "code", "retryable"), [
    (EgressBlocked("private"), ErrorCode.HTTP_BLOCKED, False),
    (ResponseTooLarge("5000000"), ErrorCode.HTTP_RESPONSE_TOO_LARGE, False),
    (UnsupportedMedia("image/png"), ErrorCode.HTTP_UNSUPPORTED_MEDIA_TYPE, False),
    (TransportFailed("ConnectTimeout"), ErrorCode.HTTP_ERROR, True),
])
async def test_client_failures_become_node_errors(error, code, retryable):
    with pytest.raises(NodeError) as exc:
        await _run(_config(), {"url": "https://api.example.com/x"}, http=FakeHttp(error=error))

    assert (exc.value.code, exc.value.retryable) == (code, retryable)


async def test_a_blocked_request_reports_only_the_category():
    with pytest.raises(NodeError) as exc:
        await _run(_config(), {"url": "https://api.example.com/x"},
                   http=FakeHttp(error=EgressBlocked("link-local")))

    assert "link-local" in exc.value.message
    assert "169.254" not in exc.value.message and "api.example.com" not in exc.value.message


async def test_a_transport_failure_never_carries_the_url_or_a_secret():
    """The client raises TransportFailed with the exception type name only, but the node must not depend
    on that: anything it puts in a NodeError message goes through redaction first."""
    secrets = FakeSecrets({"API_TOKEN": SECRET})
    rendered = {"url": f"https://api.example.com/x?key={marker_for('API_TOKEN', NONCE)}"}

    with pytest.raises(NodeError) as exc:
        await _run(_config(url="https://api.example.com/x?key={{ secret.API_TOKEN }}"), rendered,
                   http=FakeHttp(error=TransportFailed(f"ConnectError https://api.example.com/x?key={SECRET}")),
                   secrets=secrets)

    assert SECRET not in exc.value.message


async def test_an_unsupported_media_error_never_carries_a_secret():
    secrets = FakeSecrets({"API_TOKEN": SECRET})

    with pytest.raises(NodeError) as exc:
        await _run(_config(url="{{ secret.API_TOKEN }}"),
                   {"url": f"https://api.example.com/{marker_for('API_TOKEN', NONCE)}"},
                   http=FakeHttp(error=UnsupportedMedia(f"weird/{SECRET}")), secrets=secrets)

    assert SECRET not in exc.value.message


async def test_a_json_body_is_serialized_before_sending():
    http = FakeHttp()

    await _run(_config(method="POST", body='{"a": {{ start.n }}}'),
               {"url": "https://api.example.com/x", "body": {"a": 1}}, http=http)

    assert json.loads(http.calls[0]["body"]) == {"a": 1}


async def test_a_text_body_is_sent_as_is():
    http = FakeHttp()

    await _run(_config(method="POST", body="hello", bodyFormat="text"),
               {"url": "https://api.example.com/x", "body": "hello"}, http=http)

    assert http.calls[0]["body"] == "hello"


async def test_a_secret_in_the_body_is_substituted_and_redacted():
    """A JSON body is serialized before substitution, so the marker has to survive json.dumps intact."""
    http = FakeHttp(HttpResponse(200, {"content-type": "text/plain"}, f"saw {SECRET}"))
    secrets = FakeSecrets({"API_TOKEN": SECRET})
    rendered = {"url": "https://api.example.com/x", "body": {"key": marker_for("API_TOKEN", NONCE)}}

    result = await _run(_config(method="POST", body='{"key": "{{ secret.API_TOKEN }}"}'), rendered,
                        http=http, secrets=secrets)

    assert json.loads(http.calls[0]["body"]) == {"key": SECRET}
    assert result.output["body"] == "saw [REDACTED]"


async def test_the_idempotency_key_is_stable_across_attempts_but_not_executions():
    http = FakeHttp()
    node = HttpRequestNode()
    config = _config(method="POST", sendIdempotencyKey=True)

    first = NodeContext(run_id="run-1", node_id="http_1", exec_index=1, attempt=1, inputs={}, outputs={},
                        pred_ids=[], llm=None, http=http, secrets=None, secret_nonce=NONCE, timeout_sec=30)
    second = NodeContext(run_id="run-1", node_id="http_1", exec_index=1, attempt=7, inputs={}, outputs={},
                         pred_ids=[], llm=None, http=http, secrets=None, secret_nonce=NONCE, timeout_sec=30)
    third = NodeContext(run_id="run-1", node_id="http_1", exec_index=2, attempt=1, inputs={}, outputs={},
                        pred_ids=[], llm=None, http=http, secrets=None, secret_nonce=NONCE, timeout_sec=30)
    for ctx in (first, second, third):
        await node.execute(ctx, config, {"url": "https://api.example.com/x"})

    keys = [call["headers"]["Idempotency-Key"] for call in http.calls]
    assert keys[0] == keys[1] != keys[2]


async def test_the_idempotency_key_differs_between_runs():
    """Two runs of the same workflow are different work; sharing a key would make the far side drop the
    second one as a duplicate."""
    http = FakeHttp()
    config = _config(method="POST", sendIdempotencyKey=True)
    for run_id in ("run-1", "run-2"):
        ctx = NodeContext(run_id=run_id, node_id="http_1", exec_index=1, attempt=1, inputs={}, outputs={},
                          pred_ids=[], llm=None, http=http, secrets=None, secret_nonce=NONCE,
                          timeout_sec=30)
        await HttpRequestNode().execute(ctx, config, {"url": "https://api.example.com/x"})

    assert http.calls[0]["headers"]["Idempotency-Key"] != http.calls[1]["headers"]["Idempotency-Key"]


async def test_without_the_flag_no_idempotency_header_is_sent():
    http = FakeHttp()

    await _run(_config(method="POST"), {"url": "https://api.example.com/x"}, http=http)

    assert "Idempotency-Key" not in http.calls[0]["headers"]


async def test_a_missing_port_is_an_engine_fault_not_a_node_error():
    """No client wired up is a deployment mistake, not something a tenant did."""
    with pytest.raises(EngineFault):
        await _run(_config(), {"url": "https://api.example.com/x"}, http=None)


async def test_a_secret_reference_without_a_resolver_is_an_engine_fault():
    with pytest.raises(EngineFault):
        await _run(_config(url="{{ secret.API_TOKEN }}"),
                   {"url": f"https://api.example.com/{marker_for('API_TOKEN', NONCE)}"},
                   http=FakeHttp(), secrets=None)


def test_the_output_schema_describes_status_headers_and_body():
    schema = HttpRequestNode().output_schema(_config(), {})

    assert set(schema["properties"]) == {"status", "headers", "body"}
    assert schema["properties"]["status"]["type"] == "integer"


def test_the_node_declares_side_effects():
    assert HttpRequestNode().side_effects is True


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE"])
def test_idempotent_methods_keep_three_attempts(method):
    policy = HttpRequestNode().policy_for(HttpRequestNode().parse_config(
        {"method": method, "url": "https://api.example.com/x", "headers": {}}))

    assert policy.retry.maxAttempts == 3


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_non_idempotent_methods_are_tried_once(method):
    policy = HttpRequestNode().policy_for(HttpRequestNode().parse_config(
        {"method": method, "url": "https://api.example.com/x", "headers": {}}))

    assert policy.retry.maxAttempts == 1


@pytest.mark.parametrize("name", ["Host", "host", "Content-Length", "content-length"])
def test_the_framing_headers_cannot_be_set_from_the_dsl(name):
    """The client pins Host from the URL; a tenant-set one would point the request at one host while
    presenting another, and Content-Length is how a body gets desynced from its framing."""
    with pytest.raises(ValueError):
        _config(headers={name: "x"})


def test_a_header_name_that_is_not_a_token_is_refused():
    with pytest.raises(ValueError):
        _config(headers={"Bad Name": "x"})


def test_more_headers_than_the_limit_are_refused():
    with pytest.raises(ValueError):
        _config(headers={f"X-H{i}": "v" for i in range(21)})


async def test_a_secret_carrying_crlf_cannot_inject_a_header():
    """Ordering, across the node/client boundary: the node substitutes *before* calling the client, so a
    stored secret containing CRLF meets the client's RFC 7230 value check and is refused. Substituting
    after that check — or the client trusting values it was handed — would smuggle a second header line
    onto the wire. No socket is involved: the header check runs before DNS.
    """
    from engine.http.client import GuardedClient
    from engine.http.policy import parse_allowlist

    class DeadResolver:
        async def resolve(self, host):
            raise AssertionError("the header must be rejected before anything is resolved")

    client = GuardedClient(parse_allowlist("https://api.example.com"), resolver=DeadResolver())
    secrets = FakeSecrets({"API_TOKEN": "abc\r\nX-Injected: evil"})
    rendered = {"url": "https://api.example.com/x",
                "headers.Authorization": f"Bearer {marker_for('API_TOKEN', NONCE)}"}

    try:
        with pytest.raises(NodeError) as exc:
            await _run(_config(headers={"Authorization": "Bearer {{ secret.API_TOKEN }}"}), rendered,
                       http=client, secrets=secrets)
    finally:
        await client.aclose()

    # The category, not just "blocked somehow": an allowlist or DNS refusal would also be HTTP_BLOCKED
    # and would let this test pass while the header check was gone.
    assert exc.value.code == ErrorCode.HTTP_BLOCKED and "header" in exc.value.message
    assert "evil" not in exc.value.message and "abc" not in exc.value.message
