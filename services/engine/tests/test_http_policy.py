import pytest

from engine.http.policy import AllowEntry, PolicyError, ip_category, match, parse_allowlist


def _one(text: str) -> AllowEntry:
    entries = parse_allowlist(text)
    assert len(entries) == 1
    return entries[0]


def test_an_entry_without_a_port_takes_the_schemes_default():
    assert _one("https://api.example.com") == AllowEntry("https", "api.example.com", 443, False)
    assert _one("http://api.example.com") == AllowEntry("http", "api.example.com", 80, False)


def test_an_explicit_port_is_the_only_one_allowed():
    entry = _one("https://api.example.com:8443")

    assert entry.port == 8443
    assert match((entry,), "https", "api.example.com", 8443) is entry
    assert match((entry,), "https", "api.example.com", 443) is None


def test_a_wildcard_matches_sub_labels_but_not_the_domain_itself():
    entries = parse_allowlist("https://*.example.com")

    assert match(entries, "https", "a.example.com", 443) is not None
    assert match(entries, "https", "a.b.example.com", 443) is not None
    assert match(entries, "https", "example.com", 443) is None
    assert match(entries, "https", "notexample.com", 443) is None
    assert match(entries, "https", "example.com.evil.test", 443) is None


@pytest.mark.parametrize("text", [
    "http://api.example.com:443",   # http with the https default port
    "https://api.example.com:80",   # and the other way round
    "ftp://files.example.com",
    "api.example.com",              # no scheme
    "https://",                     # no host
    "https://api.example.com:notaport",
    "https://api.example.com;allowPublic",
    "https://*example.com",         # a wildcard must be its own label
    "https://*.*.example.com",
])
def test_a_malformed_entry_refuses_to_parse(text):
    with pytest.raises(PolicyError):
        parse_allowlist(text)


def test_allow_private_is_per_entry():
    entries = parse_allowlist("http://10.0.0.7:8080;allowPrivate, https://api.example.com")

    internal = match(entries, "http", "10.0.0.7", 8080)
    public = match(entries, "https", "api.example.com", 443)

    assert internal is not None and internal.allow_private is True
    assert public is not None and public.allow_private is False


def test_an_empty_allowlist_matches_nothing():
    assert parse_allowlist("") == ()
    assert match((), "https", "api.example.com", 443) is None


@pytest.mark.parametrize(("address", "category"), [
    ("127.0.0.1", "loopback"),
    ("127.9.9.9", "loopback"),
    ("169.254.169.254", "link-local"),      # cloud metadata
    ("10.1.2.3", "private"),
    ("172.16.0.1", "private"),
    ("172.31.255.255", "private"),
    ("192.168.1.1", "private"),
    ("100.64.0.1", "cgnat"),
    ("0.0.0.0", "unspecified"),
    ("224.0.0.1", "multicast"),
    ("240.0.0.1", "reserved"),
    ("::1", "loopback"),
    ("fe80::1", "link-local"),
    ("fc00::1", "private"),
    ("fd12:3456::1", "private"),
    ("::", "unspecified"),
    ("ff02::1", "multicast"),
    ("::ffff:127.0.0.1", "loopback"),       # IPv4-mapped: the classic bypass
    ("::ffff:10.0.0.1", "private"),
    ("2002:7f00:1::", "tunneled"),          # 6to4
    ("2001::1", "tunneled"),                # Teredo
    ("64:ff9b::7f00:1", "tunneled"),        # NAT64
])
def test_blocked_addresses_are_classified(address, category):
    assert ip_category(address) == category


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946",
                                     "::ffff:93.184.216.34"])
def test_public_addresses_have_no_category(address):
    assert ip_category(address) is None


def test_an_unparseable_address_is_treated_as_blocked():
    assert ip_category("not-an-ip") == "invalid"
