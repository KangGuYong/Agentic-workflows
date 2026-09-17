import pytest

from engine.http.policy import AllowEntry, PolicyError, find_entry, ip_category, parse_allowlist


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
    assert find_entry((entry,), "https", "api.example.com", 8443) is entry
    assert find_entry((entry,), "https", "api.example.com", 443) is None


def test_a_wildcard_matches_sub_labels_but_not_the_domain_itself():
    entries = parse_allowlist("https://*.example.com")

    assert find_entry(entries, "https", "a.example.com", 443) is not None
    assert find_entry(entries, "https", "a.b.example.com", 443) is not None
    assert find_entry(entries, "https", "example.com", 443) is None
    assert find_entry(entries, "https", "notexample.com", 443) is None
    assert find_entry(entries, "https", "example.com.evil.test", 443) is None
    # The degenerate host equal to the suffix itself (leading dot and all) must not match either --
    # this is the one case `len(host) > len(suffix)` exists to exclude.
    assert find_entry(entries, "https", ".example.com", 443) is None


@pytest.mark.parametrize("text", [
    "http://api.example.com:443",
    "https://api.example.com:80",
    "ftp://files.example.com",
    "api.example.com",
    "https://",
    "https://api.example.com:notaport",
    "https://api.example.com:0",
    "https://api.example.com:65536",
    "https://api.example.com;allowPublic",
    "https://api.example.com;",
    "https://*example.com",
    "https://*.*.example.com",
    "https://[::1]x8080",
    "https://api.example.com\n:8443",
    "https://127.000.000.001",
    "https://1.2.3",
    "https://999.1.1.1",
    "https://[fe80::1%eth0]",
], ids=[
    "http-with-https-port", "https-with-http-port", "unsupported-scheme", "no-scheme", "no-host",
    "non-numeric-port", "port-zero", "port-too-high", "unknown-flag", "empty-flag",
    "wildcard-not-own-label", "double-wildcard", "bracket-tail-junk", "trailing-newline",
    "leading-zero-ip", "too-few-octets", "octet-out-of-range", "scope-id",
])
def test_a_malformed_entry_refuses_to_parse(text):
    with pytest.raises(PolicyError):
        parse_allowlist(text)


def test_an_exact_entry_rejects_a_different_host_at_the_same_scheme_and_port():
    entries = parse_allowlist("https://api.example.com")
    assert find_entry(entries, "https", "other.example.com", 443) is None


def test_a_scheme_mismatch_is_rejected_even_at_a_matching_port():
    # Both requests share the same (non-default) port, so a scheme-blind `matches` would wrongly
    # accept this: the port alone is not enough to prove the request means the same thing.
    entries = parse_allowlist("https://api.example.com:8080")
    assert find_entry(entries, "http", "api.example.com", 8080) is None


def test_the_request_host_is_matched_case_insensitively():
    entries = parse_allowlist("https://api.example.com")
    assert find_entry(entries, "https", "API.EXAMPLE.COM", 443) is not None


def test_a_non_ascii_request_host_is_refused_rather_than_casefolded():
    # U+212A KELVIN SIGN casefolds to "k", which would otherwise let a request host containing it
    # alias a plain-ASCII allowlist entry it does not actually name. Task 2's client is expected to
    # hand find_entry() an already IDNA-encoded ASCII host; anything else is refused outright.
    entries = parse_allowlist("https://ok.example.com")
    assert find_entry(entries, "https", "oK.example.com", 443) is None


def test_the_most_specific_entry_wins_regardless_of_list_order():
    # A broad `;allowPrivate` wildcard must never upgrade a narrower, unprivileged entry just because
    # it happens to be listed first -- allow_private belongs to whichever entry is the real match.
    broad_first = parse_allowlist("https://*.example.com;allowPrivate, https://api.example.com")
    narrow_first = parse_allowlist("https://api.example.com, https://*.example.com;allowPrivate")

    for entries in (broad_first, narrow_first):
        entry = find_entry(entries, "https", "api.example.com", 443)
        assert entry is not None
        assert entry.host == "api.example.com"
        assert entry.allow_private is False


def test_among_wildcards_the_longer_more_specific_pattern_wins():
    entries = parse_allowlist("https://*.example.com;allowPrivate, https://*.eu.example.com")

    entry = find_entry(entries, "https", "svc.eu.example.com", 443)

    assert entry is not None
    assert entry.host == "*.eu.example.com"
    assert entry.allow_private is False


def test_exact_host_beats_a_same_length_wildcard_pattern():
    # "a.example.com" and "*.example.com" are both 13 characters, so a specificity metric based on
    # pattern length alone (with the is_exact flag dropped) cannot tell them apart -- this is what
    # actually distinguishes "an exact host always wins" from "the longer string wins".
    entries = parse_allowlist("https://*.example.com;allowPrivate, https://a.example.com")

    entry = find_entry(entries, "https", "a.example.com", 443)

    assert entry is not None
    assert entry.host == "a.example.com"
    assert entry.allow_private is False


def test_a_tie_in_specificity_fails_closed_regardless_of_order():
    # The same host listed twice -- plausibly from merging two allowlists -- is a tie in specificity.
    # `>` alone resolves that tie by list position, which is a silent privilege upgrade if the
    # privileged copy happens to come first. The unprivileged entry must win either way.
    privileged_first = parse_allowlist("https://api.example.com;allowPrivate, https://api.example.com")
    unprivileged_first = parse_allowlist("https://api.example.com, https://api.example.com;allowPrivate")

    for entries in (privileged_first, unprivileged_first):
        entry = find_entry(entries, "https", "api.example.com", 443)
        assert entry is not None
        assert entry.allow_private is False


def test_an_ipv6_literal_host_is_normalised_so_it_actually_matches():
    entries = parse_allowlist("https://[::0001]")
    assert entries[0].host == "::1"
    assert find_entry(entries, "https", "::1", 443) is not None


def test_allow_private_is_per_entry():
    entries = parse_allowlist("http://10.0.0.7:8080;allowPrivate, https://api.example.com")

    internal = find_entry(entries, "http", "10.0.0.7", 8080)
    public = find_entry(entries, "https", "api.example.com", 443)

    assert internal is not None and internal.allow_private is True
    assert public is not None and public.allow_private is False


def test_an_empty_allowlist_matches_nothing():
    assert parse_allowlist("") == ()
    assert find_entry((), "https", "api.example.com", 443) is None


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
    ("fe80::1%eth0", "link-local"),         # a scope id does not change the classification
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
    # First and last address of every row of _V4 and _V6 whose prefix length a mutation could narrow
    # or widen without any single mid-range sample noticing -- excluding only the single-address
    # ::1/128 and ::/128 entries, which have no "narrowing" to detect. If you add a row there, add its
    # first/last pair here too.
    ("127.0.0.0", "loopback"), ("127.255.255.255", "loopback"),
    ("169.254.0.0", "link-local"), ("169.254.255.255", "link-local"),
    ("10.0.0.0", "private"), ("10.255.255.255", "private"),
    ("172.16.0.0", "private"), ("172.31.255.255", "private"),
    ("192.168.0.0", "private"), ("192.168.255.255", "private"),
    ("100.64.0.0", "cgnat"), ("100.127.255.255", "cgnat"),
    ("0.0.0.0", "unspecified"), ("0.255.255.255", "unspecified"),
    ("224.0.0.0", "multicast"), ("239.255.255.255", "multicast"),
    ("240.0.0.0", "reserved"), ("255.255.255.255", "reserved"),
    ("192.0.0.0", "reserved"), ("192.0.0.255", "reserved"),
    ("198.18.0.0", "reserved"), ("198.19.255.255", "reserved"),
    ("192.88.99.0", "tunneled"), ("192.88.99.255", "tunneled"),
    ("fe80::", "link-local"), ("febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "link-local"),
    ("fc00::", "private"), ("fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "private"),
    ("fec0::", "private"), ("feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "private"),
    ("ff00::", "multicast"), ("ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "multicast"),
    ("2002::", "tunneled"), ("2002:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "tunneled"),
    ("2001::", "tunneled"), ("2001:0:ffff:ffff:ffff:ffff:ffff:ffff", "tunneled"),
    ("64:ff9b::", "tunneled"), ("64:ff9b::ffff:ffff", "tunneled"),
    ("64:ff9b:1::", "tunneled"), ("64:ff9b:1:ffff:ffff:ffff:ffff:ffff", "tunneled"),
])
def test_the_full_extent_of_each_range_is_blocked(address, category):
    assert ip_category(address) == category


@pytest.mark.parametrize("address", [
    "169.253.255.255", "169.255.0.0",
    "100.63.255.255", "100.128.0.0",
    "1.0.0.0",
    "223.255.255.255",
    "fe7f:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
])
def test_the_address_just_outside_a_range_is_public(address):
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
    # None of these ranges is named in _V4/_V6 -- this pins that the fallback blocks by default, not
    # just the ranges someone remembered to enumerate. Asserting "is not None" (the behaviour that
    # matters), rather than the specific fallback name, means correctly promoting one of these into
    # the table later -- e.g. tabling 2001:2::/48 the way its IPv4 counterpart, 198.18.0.0/15, already
    # is -- will not read as a regression here.
    assert ip_category(address) is not None


def test_a_named_ranges_own_category_survives_next_to_the_fallback():
    # 240.0.0.0/4 is named "reserved" in the table. 203.0.113.1 (TEST-NET-3) is the fallback's
    # designated witness -- do not add this range to the table -- so it is the one address in this
    # file pinned to the specific fallback name rather than just "is not None". The two names must
    # stay distinct, or narrowing/deleting the table entry would be invisible.
    assert ip_category("240.0.0.1") == "reserved"
    assert ip_category("203.0.113.1") == "non-global"


def test_an_unparseable_address_is_treated_as_blocked():
    assert ip_category("not-an-ip") == "invalid"
