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
    # The degenerate host equal to the suffix itself (leading dot and all) must not match either --
    # this is the one case `len(host) > len(suffix)` exists to exclude.
    assert match(entries, "https", ".example.com", 443) is None


@pytest.mark.parametrize("text", [
    "http://api.example.com:443",   # http with the https default port
    "https://api.example.com:80",   # and the other way round
    "ftp://files.example.com",
    "api.example.com",              # no scheme
    "https://",                     # no host
    "https://api.example.com:notaport",
    "https://api.example.com:0",        # port 0 is not a usable port
    "https://api.example.com:65536",    # one past the top of the port range
    "https://api.example.com;allowPublic",
    "https://api.example.com;",         # a bare/empty flag is a typo, not "no flag"
    "https://*example.com",         # a wildcard must be its own label
    "https://*.*.example.com",
    "https://[::1]x8080",           # junk between "]" and the port must not be silently dropped
])
def test_a_malformed_entry_refuses_to_parse(text):
    with pytest.raises(PolicyError):
        parse_allowlist(text)


def test_an_exact_entry_rejects_a_different_host_at_the_same_scheme_and_port():
    entries = parse_allowlist("https://api.example.com")
    assert match(entries, "https", "other.example.com", 443) is None


def test_a_scheme_mismatch_is_rejected_even_at_a_matching_port():
    # Both requests share the same (non-default) port, so a scheme-blind `matches` would wrongly
    # accept this: the port alone is not enough to prove the request means the same thing.
    entries = parse_allowlist("https://api.example.com:8080")
    assert match(entries, "http", "api.example.com", 8080) is None


def test_the_request_host_is_matched_case_insensitively():
    entries = parse_allowlist("https://api.example.com")
    assert match(entries, "https", "API.EXAMPLE.COM", 443) is not None


def test_a_non_ascii_request_host_is_refused_rather_than_casefolded():
    # U+212A KELVIN SIGN casefolds to "k", which would otherwise let a request host containing it
    # alias a plain-ASCII allowlist entry it does not actually name. Task 2's client is expected to
    # hand match() an already IDNA-encoded ASCII host; anything else is refused outright.
    entries = parse_allowlist("https://ok.example.com")
    assert match(entries, "https", "oK.example.com", 443) is None


def test_the_most_specific_entry_wins_regardless_of_list_order():
    # A broad `;allowPrivate` wildcard must never upgrade a narrower, unprivileged entry just because
    # it happens to be listed first -- allow_private belongs to whichever entry is the real match.
    broad_first = parse_allowlist("https://*.example.com;allowPrivate, https://api.example.com")
    narrow_first = parse_allowlist("https://api.example.com, https://*.example.com;allowPrivate")

    for entries in (broad_first, narrow_first):
        entry = match(entries, "https", "api.example.com", 443)
        assert entry is not None
        assert entry.host == "api.example.com"
        assert entry.allow_private is False


def test_among_wildcards_the_longer_more_specific_pattern_wins():
    entries = parse_allowlist("https://*.example.com;allowPrivate, https://*.eu.example.com")

    entry = match(entries, "https", "svc.eu.example.com", 443)

    assert entry is not None
    assert entry.host == "*.eu.example.com"
    assert entry.allow_private is False


def test_an_ipv6_literal_host_is_normalised_so_it_actually_matches():
    entries = parse_allowlist("https://[::0001]")
    assert entries[0].host == "::1"
    assert match(entries, "https", "::1", 443) is not None


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
    ("64:ff9b::7f00:1", "tunneled"),        # NAT64, well-known prefix
    ("::169.254.169.254", "link-local"),           # IPv4-compatible (deprecated): cloud metadata again
    ("::ffff:0:169.254.169.254", "link-local"),    # IPv4-translated (SIIT): and again
    ("64:ff9b:1::a9fe:a9fe", "tunneled"),          # NAT64, local-use prefix (RFC 8215): blanket-blocked
    ("192.0.0.170", "reserved"),            # NAT64/DNS64 discovery address
    ("198.18.0.1", "reserved"),             # benchmarking
    ("192.88.99.1", "tunneled"),            # 6to4 relay anycast
    ("fec0::1", "private"),                 # site-local, deprecated
])
def test_blocked_addresses_are_classified(address, category):
    assert ip_category(address) == category


@pytest.mark.parametrize(("address", "category"), [
    # First and last address of every range whose prefix length a mutation could narrow or widen
    # without any single mid-range sample noticing.
    ("169.254.0.0", "link-local"), ("169.254.255.255", "link-local"),
    ("100.64.0.0", "cgnat"), ("100.127.255.255", "cgnat"),
    ("0.0.0.0", "unspecified"), ("0.255.255.255", "unspecified"),
    ("224.0.0.0", "multicast"), ("239.255.255.255", "multicast"),
    ("240.0.0.0", "reserved"), ("255.255.255.255", "reserved"),
    ("fe80::", "link-local"), ("febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "link-local"),
    ("ff00::", "multicast"), ("ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "multicast"),
])
def test_the_full_extent_of_each_range_is_blocked(address, category):
    assert ip_category(address) == category


@pytest.mark.parametrize("address", [
    # Immediately outside each of those same ranges: public, or (where the neighbouring range picks
    # up without a gap) the neighbour's own category -- never the inside range's category.
    "169.253.255.255", "169.255.0.0",
    "100.63.255.255", "100.128.0.0",
    "1.0.0.0",
    "223.255.255.255",
    "fe7f:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
])
def test_the_address_just_outside_a_range_is_not_in_it(address):
    assert ip_category(address) is None


def test_adjacent_v6_ranges_meet_with_no_gap_and_no_overlap():
    # fe80::/10 (link-local), fec0::/10 (private/site-local) and ff00::/8 (multicast) are back to
    # back; a boundary mutation in any one of them would show up as a wrong category here, not as None.
    assert ip_category("fec0::") == "private"                                            # fe80::/10 ends
    assert ip_category("feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff") == "private"            # fec0::/10 ends


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946",
                                     "::ffff:93.184.216.34"])
def test_public_addresses_have_no_category(address):
    assert ip_category(address) is None


@pytest.mark.parametrize("address", [
    "192.0.2.1",        # TEST-NET-1
    "198.51.100.1",     # TEST-NET-2
    "203.0.113.1",      # TEST-NET-3
    "2001:db8::1",      # IPv6 documentation
    "100::1",           # IPv6 discard-only
    "2001:2::1",        # IPv6 benchmarking
])
def test_default_deny_blocks_ranges_absent_from_either_table(address):
    # None of these ranges is named in _V4/_V6 -- this pins that the fallback itself blocks by default,
    # under its own distinct name, rather than only the ranges someone remembered to enumerate. Using
    # the specific name (not just "is not None") is what makes a table entry's prefix length observable:
    # if the fallback returned the same name as some table category, narrowing or deleting that entry
    # would be silently absorbed by the fallback instead of changing the output.
    assert ip_category(address) == "non-global"


def test_a_named_ranges_own_category_survives_next_to_the_fallback():
    # 240.0.0.0/4 is named "reserved" in the table; the fallback for everything else is "non-global".
    # The two must stay distinct, or narrowing/deleting the table entry would be invisible.
    assert ip_category("240.0.0.1") == "reserved"
    assert ip_category("192.0.2.1") == "non-global"


def test_an_unparseable_address_is_treated_as_blocked():
    assert ip_category("not-an-ip") == "invalid"
