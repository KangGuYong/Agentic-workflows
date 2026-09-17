"""Header rules as pure functions (2b design §5.4) -- no sockets, no client, no event loop."""
import pytest

from engine.http.headers import (
    CONTENT_HEADERS,
    CREDENTIAL_HEADERS,
    UNSAFE_HEADERS,
    HeaderRejected,
    bodyless_headers,
    headers_for_redirect,
    sanitize_headers,
)

# Hardcoded, not sorted(UNSAFE_HEADERS): parametrizing over the very set under test would let a mutant
# that *removes* an entry from UNSAFE_HEADERS shrink the parametrize list instead of failing a row --
# the case would silently stop being collected rather than fail. A literal list here means removing an
# entry from the implementation's set makes sanitize_headers stop stripping it, which is what actually
# fails the still-collected row for that name.
_EXPECTED_UNSAFE_HEADERS = (
    "host", "content-length", "transfer-encoding", "connection", "keep-alive", "expect", "upgrade", "te",
    "trailer", "proxy-authorization", "proxy-authenticate", "accept-encoding",
)
_EXPECTED_CREDENTIAL_HEADERS = ("authorization", "cookie", "x-api-key")
# Same reasoning as the two tuples above, for bodyless_headers' strip set.
_EXPECTED_CONTENT_HEADERS = ("content-type", "content-encoding", "content-language", "content-md5")

# ---------------------------------------------------------------------------------------------------
# sanitize_headers: the strip set, one entry at a time
# ---------------------------------------------------------------------------------------------------


def test_the_unsafe_header_set_has_not_silently_changed_shape():
    """A companion to the per-entry test below: that test can only fail a row for a name still IN the
    set. If a mutant removes a name from UNSAFE_HEADERS entirely, this is what notices the set itself
    no longer matches, rather than the per-entry test silently collecting one row fewer."""
    assert frozenset(_EXPECTED_UNSAFE_HEADERS) == UNSAFE_HEADERS


@pytest.mark.parametrize("name", _EXPECTED_UNSAFE_HEADERS)
def test_every_unsafe_header_is_individually_stripped(name):
    """Table-driven so removing any single entry from UNSAFE_HEADERS fails exactly its own row --
    see _EXPECTED_UNSAFE_HEADERS above for why this list is not sorted(UNSAFE_HEADERS) itself."""
    result = sanitize_headers({name: "x", name.upper(): "y", "X-Foo": "bar"})
    assert result == {"X-Foo": "bar"}


def test_a_safe_header_survives_untouched():
    headers = {"X-Foo": "bar", "Authorization": "Bearer t", "Cookie": "a=b", "X-API-Key": "k"}
    assert sanitize_headers(headers) == headers


def test_an_empty_headers_dict_is_fine():
    assert sanitize_headers({}) == {}


# ---------------------------------------------------------------------------------------------------
# sanitize_headers: header name grammar (RFC 7230 §3.2.6 token)
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "name"), [
    ("empty name", ""),
    ("space in name", "X A"),
    ("colon in name", "X:A"),
    ("CRLF in name", "X-A\r\nX-Inj"),
    ("non-ascii name (Korean)", "X-가"),
    ("Turkish dotless I folds away from 'transfer-encoding', but is still non-ASCII",
     "Transfer-Encodİng"),
])
def test_a_malformed_header_name_is_rejected(label, name):
    with pytest.raises(HeaderRejected):
        sanitize_headers({name: "ok"})


def test_a_kelvin_sign_name_that_casefolds_into_the_strip_set_is_simply_stripped():
    """U+212A KELVIN SIGN lowercases to plain 'k', so "Keep-Alive" casefolds to exactly
    "keep-alive" and is caught by the same membership check as the real header -- over-blocking, not a
    bypass. This documents that outcome rather than assuming it."""
    assert sanitize_headers({"Keep-Alive": "x", "X-Foo": "bar"}) == {"X-Foo": "bar"}


# ---------------------------------------------------------------------------------------------------
# sanitize_headers: value grammar -- RFC 7230 §3.2 field-value is VCHAR / SP / HTAB, nothing else.
# Everything in the first table used to reach the wire (or reach h11, which raised its own
# LocalProtocolError -- a different, unmapped exception) before the value check was tightened.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "value"), [
    ("lone CR", "b\rc"),
    ("lone LF", "b\nc"),
    ("CRLF plus an injected header line", "b\r\nX-Injected: evil"),
    ("NUL", "b\x00c"),
    ("DEL 0x7f", "b\x7fc"),
    ("VT 0x0b", "b\x0bc"),
    ("FF 0x0c", "b\x0cc"),
    ("SOH 0x01", "b\x01c"),
    ("C1 control 0x85", "b\x85c"),
    ("non-ascii value (Korean)", "가나다"),
])
def test_a_malformed_header_value_is_rejected(label, value):
    with pytest.raises(HeaderRejected):
        sanitize_headers({"X-A": value})


@pytest.mark.parametrize(("label", "value"), [
    ("empty value", ""),
    ("leading space", " folded"),
    ("leading tab", "\tfolded"),
    ("internal tab", "a\tb"),
    ("internal space", "a b c"),
    ("every VCHAR", "".join(chr(c) for c in range(0x21, 0x7f))),
    ("full VCHAR+SP range", "".join(chr(c) for c in range(0x20, 0x7f))),
])
def test_a_well_formed_header_value_is_accepted(label, value):
    assert sanitize_headers({"X-A": value}) == {"X-A": value}


# ---------------------------------------------------------------------------------------------------
# sanitize_headers: inputs that are not strings at all
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [
    {123: "x"},
    {"X-A": 123},
    {"X-A": None},
    {b"X-A": "x"},
    {"X-A": b"x"},
])
def test_a_non_string_header_is_rejected(headers):
    with pytest.raises(HeaderRejected):
        sanitize_headers(headers)


# ---------------------------------------------------------------------------------------------------
# headers_for_redirect
# ---------------------------------------------------------------------------------------------------


def test_same_origin_redirect_keeps_every_header():
    headers = {"Authorization": "Bearer t", "Cookie": "s=1", "X-API-Key": "k", "X-Foo": "bar"}
    result = headers_for_redirect(headers, "https://api.example.com/a", "https://api.example.com/b")
    assert result == headers


def test_cross_origin_redirect_drops_credential_headers_but_keeps_others():
    headers = {"Authorization": "Bearer t", "Cookie": "s=1", "X-API-Key": "k", "X-Foo": "bar"}
    result = headers_for_redirect(headers, "https://api.example.com/a", "https://other.example.com/b")
    assert result == {"X-Foo": "bar"}


def test_an_http_to_https_upgrade_on_the_same_host_keeps_credentials():
    headers = {"Authorization": "Bearer t", "Cookie": "s=1"}
    result = headers_for_redirect(headers, "http://api.example.com/a", "https://api.example.com/b")
    assert result == headers


def test_a_downgrade_shaped_pair_is_not_treated_as_an_upgrade():
    """is_https_upgrade must check both directions -- an https-to-http pair (which _follow_redirects
    blocks before this function is ever reached, but this function has no opinion on that by itself)
    must not accidentally satisfy the upgrade carve-out and keep credentials it should drop."""
    headers = {"Authorization": "Bearer t"}
    result = headers_for_redirect(headers, "https://api.example.com/a", "http://api.example.com/b")
    assert result == {}


def test_origin_comparison_uses_host_and_port_not_host_alone():
    headers = {"Authorization": "Bearer t"}
    result = headers_for_redirect(headers, "https://api.example.com:8443/a", "https://api.example.com/b")
    assert result == {}  # different port -> different origin, even though the host string matches


def test_the_credential_header_set_has_not_silently_changed_shape():
    assert frozenset(_EXPECTED_CREDENTIAL_HEADERS) == CREDENTIAL_HEADERS


@pytest.mark.parametrize("name", _EXPECTED_CREDENTIAL_HEADERS)
def test_every_credential_header_is_individually_dropped_cross_origin(name):
    """Hardcoded list, not sorted(CREDENTIAL_HEADERS) -- same reasoning as the unsafe-header table."""
    headers = {name: "secret", "X-Foo": "bar"}
    result = headers_for_redirect(headers, "https://api.example.com/a", "https://other.example.com/b")
    assert result == {"X-Foo": "bar"}


# ---------------------------------------------------------------------------------------------------
# bodyless_headers
# ---------------------------------------------------------------------------------------------------


def test_the_content_header_set_has_not_silently_changed_shape():
    """A companion to the per-entry test below, same reasoning as the unsafe/credential shape tests:
    this is what notices a mutant that removes a name from CONTENT_HEADERS entirely, rather than the
    per-entry test silently collecting one row fewer."""
    assert frozenset(_EXPECTED_CONTENT_HEADERS) == CONTENT_HEADERS


@pytest.mark.parametrize("name", _EXPECTED_CONTENT_HEADERS)
def test_every_content_header_is_individually_dropped(name):
    """Hardcoded list, not sorted(CONTENT_HEADERS) -- same reasoning as the unsafe-header table."""
    headers = {name: "x", name.upper(): "y", "X-Foo": "bar"}
    assert bodyless_headers(headers) == {"X-Foo": "bar"}


def test_a_header_outside_the_content_set_survives():
    headers = {"Authorization": "Bearer t", "X-Foo": "bar"}
    assert bodyless_headers(headers) == headers
