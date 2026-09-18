"""Egress allowlist and address classification (2b design §5.2, §5.3).

Pure: no DNS, no sockets. The client (client.py) supplies resolved addresses and asks questions here, so
the whole policy can be tested as a table without touching the network. `ip_category` is default-deny;
see its docstring for what that means and why its fallback name has to stay distinct from every table
category.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

DEFAULT_PORTS = {"http": 80, "https": 443}
_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_HOSTNAME = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")
_WILDCARD = re.compile(rf"^\*(?:\.{_LABEL})+$")

# Named ranges, checked before the default-deny fallback (see ip_category). Order matters only for
# readability: the ranges do not overlap.
_V4 = [
    ("loopback", ipaddress.ip_network("127.0.0.0/8")),
    ("link-local", ipaddress.ip_network("169.254.0.0/16")),
    ("private", ipaddress.ip_network("10.0.0.0/8")),
    ("private", ipaddress.ip_network("172.16.0.0/12")),
    ("private", ipaddress.ip_network("192.168.0.0/16")),
    ("cgnat", ipaddress.ip_network("100.64.0.0/10")),
    ("unspecified", ipaddress.ip_network("0.0.0.0/8")),
    ("multicast", ipaddress.ip_network("224.0.0.0/4")),
    ("reserved", ipaddress.ip_network("240.0.0.0/4")),
    ("reserved", ipaddress.ip_network("192.0.0.0/24")),        # IETF protocol assignments (incl. the
                                                               # NAT64/DNS64 discovery addresses)
    ("reserved", ipaddress.ip_network("198.18.0.0/15")),       # benchmarking
    ("tunneled", ipaddress.ip_network("192.88.99.0/24")),      # 6to4 relay anycast
]
_V6 = [
    ("loopback", ipaddress.ip_network("::1/128")),
    # Redundant with the v4 fallback; see ip_category's ordering comment.
    ("unspecified", ipaddress.ip_network("::/128")),
    ("link-local", ipaddress.ip_network("fe80::/10")),
    ("private", ipaddress.ip_network("fc00::/7")),
    ("private", ipaddress.ip_network("fec0::/10")),            # site-local, deprecated (RFC 3879)
    ("multicast", ipaddress.ip_network("ff00::/8")),
    # Blanket blocks regardless of what they embed -- see _embedded_v4.
    ("tunneled", ipaddress.ip_network("2002::/16")),           # 6to4
    ("tunneled", ipaddress.ip_network("2001::/32")),           # Teredo
    ("tunneled", ipaddress.ip_network("64:ff9b::/96")),        # NAT64, well-known prefix (RFC 6052)
    ("tunneled", ipaddress.ip_network("64:ff9b:1::/48")),      # NAT64, local-use prefix (RFC 8215)
]

# IPv6 forms that carry an IPv4 address in their low 32 bits. Consulted only after _V6 above has had
# first refusal (see ip_category), so this never overrides a blanket block.
_EMBEDDED_V4_NETWORKS = (
    ipaddress.ip_network("::ffff:0:0/96"),     # IPv4-mapped (RFC 4291 §2.5.5.2)
    ipaddress.ip_network("::/96"),             # IPv4-compatible, deprecated (RFC 4291 §2.5.5.1)
    ipaddress.ip_network("::ffff:0:0:0/96"),   # IPv4-translated / SIIT
)
# "::" and "::1" never reach here: the _V6 table returns first. See ip_category.


class PolicyError(Exception):
    """A malformed allowlist entry. Raised at startup only, never per request."""


@dataclass(frozen=True)
class AllowEntry:
    scheme: str
    host: str  # lowercase; a wildcard entry keeps its leading "*."
    port: int
    allow_private: bool

    def matches(self, scheme: str, host: str, port: int) -> bool:
        if scheme != self.scheme or port != self.port:
            return False
        if not self.host.startswith("*."):
            return host == self.host
        suffix = self.host[1:]  # ".example.com"
        # The leading dot in `suffix` is what makes this safe: ending in ".example.com" requires an
        # actual label boundary right before "example.com", so "notexample.com" cannot match (no dot
        # there) and the bare "example.com" cannot match either (too short to contain a leading dot at
        # all). `len(host) > len(suffix)` excludes only the degenerate host == ".example.com" itself.
        return host.endswith(suffix) and len(host) > len(suffix)


def parse_allowlist(raw: str) -> tuple[AllowEntry, ...]:
    """Parse HTTP_ALLOWLIST. Raises PolicyError so load_config can refuse to start (2b design §5.2)."""
    return tuple(_entry(item) for item in (part.strip() for part in raw.split(",")) if item)


def find_entry(entries: tuple[AllowEntry, ...], scheme: str, host: str, port: int) -> AllowEntry | None:
    """The most specific entry that permits this target, or None.

    "Most specific" -- not "first in the list" -- because `allowPrivate` belongs to the entry that
    matched and must not leak to a broader entry that merely happens to match too: an exact host always
    outranks a wildcard, and among wildcards a longer (more specific) pattern outranks a shorter one.
    When two matching entries are equally specific -- the same host written twice, e.g. by an operator
    who merged two allowlists -- the one WITHOUT `allowPrivate` wins: on a tie, the least privileged
    result is the safe one, not whichever happened to be written first. Together this makes the result
    fully independent of the order entries were written in.

    `host` is matched case-insensitively against the (already-lowercase) entries, but must already be
    ASCII -- Task 2's client resolves DNS and is responsible for IDNA-encoding the host before calling
    here. A non-ASCII host is refused outright rather than lowercased, because casefolding non-ASCII
    text can equate characters that should stay distinct (e.g. U+212A KELVIN SIGN lowercases to "k" and
    would otherwise alias an entry for "ok.example.com").
    """
    if not host.isascii():
        return None
    host = host.lower()
    candidates = [entry for entry in entries if entry.matches(scheme, host, port)]
    return max(candidates, key=_specificity, default=None)


def _specificity(entry: AllowEntry) -> tuple[bool, int, bool]:
    # (is_exact, pattern_length, not allow_private): an exact host beats every wildcard regardless of
    # length; among wildcards the longer -- so more specific -- host pattern wins; and between two
    # otherwise-equal entries (a duplicated host, most plausibly), the one WITHOUT allowPrivate wins,
    # so a tie fails closed instead of resolving by list position.
    return (not entry.host.startswith("*."), len(entry.host), not entry.allow_private)


def has_hostname_syntax(host: str) -> bool:
    """True if `host` has valid hostname label syntax (dot-separated labels, each starting and ending
    with an alphanumeric, hyphens allowed inside).

    A wildcard entry's `matches()` is a bare suffix test (`host.endswith(".example.com")`): it never
    checks that what comes *before* the suffix is a well-formed label, because `_entry()` above already
    guaranteed that for every allowlist entry at parse time. The client (client.py) has to make the same
    guarantee for the request-side host it is about to match against that suffix, since nothing else
    stops something like "..example.com" (an empty label) or "%.example.com" (an invalid character) from
    passing the suffix test on its way to a wildcard entry. Neither can actually resolve, so this is
    defence in depth rather than a live bypass -- callers should check `ip_category(host) != "invalid"`
    first and skip this for an IP literal: an IPv4 literal happens to satisfy `_LABEL`'s digit-only
    labels and would pass anyway, but an IPv6 literal's colons never will, so this must not be asked to
    validate an address, only a name.
    """
    return bool(_HOSTNAME.fullmatch(host))


def ip_category(address: str) -> str | None:
    """Name of the blocked category, or None -- but only when the address is confirmed globally
    routable (2b design §5.3, default-deny). An address that is neither named below nor provably global
    still comes back blocked, as "non-global", instead of falling through to None. "non-global" means
    specifically this: the catch-all rule blocked it, not a named range -- an operator seeing it knows
    the table has no opinion on this address and Python's own classification was relied on.

    The tables stay authoritative for the ranges they name: `ipaddress`'s own idea of what counts as
    global has changed across Python patch releases, and a security boundary should not move just
    because the interpreter was upgraded. They no longer have to be exhaustive for safety, though --
    that is the fallback's job. Keeping "non-global" distinct from every table category name (rather
    than reusing one, e.g. "reserved") matters beyond naming: it is what makes each table entry
    observable. If the fallback used the same name as some entry, narrowing or deleting that entry
    would be silently absorbed by the fallback -- same output, on every input -- which is exactly the
    kind of change a test (or an operator reading a diff) should be able to catch and cannot if the two
    names collide.

    Only a category name is ever shown to a tenant (2b design §5.6): the address itself would tell an
    attacker what the engine can see.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "invalid"
    if isinstance(ip, ipaddress.IPv6Address):
        # The table is checked before any unwrapping. Unwrapping first would need an extra exclusion:
        # "::1" sits inside the IPv4-compatible embedding range (::/96) and would otherwise be
        # reclassified as "0.0.0.1", landing on the unrelated "unspecified" v4 entry instead of
        # "loopback". ("::" is also in that range, but would unwrap to 0.0.0.0 and get the *same* name,
        # "unspecified", either way, so its own table entry above is deliberate double coverage, not
        # something this order depends on.) Checking the table first lets the existing ::1/128 entry
        # win with no special-casing, and is also what keeps the two NAT64 blanket blocks (_embedded_v4)
        # from ever reaching the unwrap step at all.
        for name, network in _V6:
            if ip in network:
                return name
        embedded = _embedded_v4(ip)
        if embedded is not None:
            return ip_category(str(embedded))
        # is_site_local is true only for fec0::/10, which the table above already caught -- this
        # condition is unreachable today. Kept as a guard that stays correct if that table entry is
        # ever removed, rather than something this code currently depends on.
        if ip.is_global and not ip.is_site_local:
            return None
        return "non-global"
    for name, network in _V4:
        if ip in network:
            return name
    if ip.is_global:
        return None
    return "non-global"


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address carried in ip's low 32 bits, for the known embedding forms -- or None.

    Only called once the _V6 table above has had first refusal, so this can never soften a blanket
    block: 64:ff9b::/96 (the well-known, globally-significant NAT64 prefix) and 64:ff9b:1::/48 (its
    RFC 8215 local-use sibling) both stay blocked no matter what public address they embed -- that
    over-blocking is deliberate, not a gap.
    """
    if any(ip in network for network in _EMBEDDED_V4_NETWORKS):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _entry(item: str) -> AllowEntry:
    text, semicolon, flag = item.partition(";")
    allow_private = flag.strip() == "allowPrivate"
    if semicolon and not allow_private:
        raise PolicyError(f"allowPrivate 옵션만 사용할 수 있습니다: {item}")
    scheme, separator, rest = text.strip().lower().partition("://")
    if not separator or scheme not in DEFAULT_PORTS:
        raise PolicyError(f"http:// 또는 https:// 로 시작해야 합니다: {item}")
    host, port = _split(rest, item)
    if not host:
        raise PolicyError(f"호스트가 없습니다: {item}")
    if _is_ip(host):
        if "%" in host:
            # A scope id (RFC 4007, e.g. "fe80::1%eth0") names an interface on the machine that wrote
            # the config -- no address the client resolves at request time can ever carry it, so an
            # entry like this can never match anything. Refuse it instead of accepting silently-dead
            # config.
            raise PolicyError(f"스코프 ID가 있는 주소는 사용할 수 없습니다: {item}")
        # Normalise an IP-literal host to its canonical form at parse time, so an operator who wrote
        # "[::0001]" gets an entry that actually matches requests for "::1" instead of a silently dead
        # one. (The request-side host is expected to arrive already normalised the same way -- see
        # find_entry()'s docstring.)
        host = ipaddress.ip_address(host).compressed
    elif _is_all_numeric_labels(host):
        # Every label is decimal digits (e.g. "127.000.000.001", "1.2.3", "999.1.1.1"): this was meant
        # to be an IPv4 address and _is_ip rejected it (leading zeros, too few octets, an out-of-range
        # one), not a real hostname -- no real domain delegates an all-numeric label. Refuse it rather
        # than silently accepting it as a hostname that happens to look like a typo'd IP.
        raise PolicyError(
            f"IP 주소 형식이 올바르지 않습니다 (0으로 시작하거나, 옥텟이 4개가 아니거나, "
            f"255를 넘습니다): {item}"
        )
    elif not (_HOSTNAME.fullmatch(host) or _WILDCARD.fullmatch(host)):
        raise PolicyError(f"호스트 형식이 올바르지 않습니다: {item}")
    if port is None:
        port = DEFAULT_PORTS[scheme]
    elif port == DEFAULT_PORTS["https" if scheme == "http" else "http"]:
        # http://host:443 or https://host:80 is almost always a mistake, and the mistake silently opens
        # or closes something the operator believes is the other way round.
        raise PolicyError(
            f"https 에는 80 포트를, http 에는 443 포트를 쓸 수 없습니다 "
            f"(포트를 지우거나 스킴을 바꾸세요): {item}"
        )
    return AllowEntry(scheme, host, port, allow_private)


def _split(rest: str, item: str) -> tuple[str, int | None]:
    if rest.startswith("["):  # [::1]:8080
        close = rest.find("]")
        if close < 0:
            raise PolicyError(f"IPv6 주소의 괄호가 닫히지 않았습니다: {item}")
        host, tail = rest[1:close], rest[close + 1:]
        if not tail:
            return host, None
        if not tail.startswith(":"):
            raise PolicyError(f"']' 다음에는 ':포트'만 올 수 있습니다: {item}")
        return host, _port(tail[1:], item)
    host, separator, port_text = rest.partition(":")
    return host, (_port(port_text, item) if separator else None)


def _port(text: str, item: str) -> int:
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 65535:
        raise PolicyError(f"포트는 1-65535 사이의 숫자여야 합니다: {item}")
    return int(text)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _is_all_numeric_labels(host: str) -> bool:
    # True when every dot-separated label is decimal digits. _HOSTNAME would otherwise happily accept
    # "127.000.000.001" or "1.2.3" as ordinary labels, since digits are valid label characters -- but
    # ICANN never delegates an all-numeric label, so a host shaped like this (and already rejected by
    # _is_ip above) was a broken IPv4 literal, not a hostname. This is decimal-only: a hex-form literal
    # like "0x7f.0.0.1" has labels that are not all isdigit() and falls through to the hostname check
    # instead, unchanged from before -- chasing every alternate encoding is out of scope here.
    return all(label.isdigit() for label in host.split("."))
