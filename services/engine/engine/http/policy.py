"""Egress allowlist and address classification (2b design §5.2, §5.3).

Pure: no DNS, no sockets. The client (client.py) supplies resolved addresses and asks questions here, so
the whole policy can be tested as a table without touching the network.

`ip_category` is default-deny: it returns None only when it can positively confirm an address is
globally routable. A range the tables below forgot to name is still blocked, generically as
"non-global", never silently let through -- for an egress filter, a forgotten range is an SSRF hole,
while a wrongly-blocked one is just a support ticket.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

DEFAULT_PORTS = {"http": 80, "https": 443}
LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
HOSTNAME = re.compile(rf"^{LABEL}(?:\.{LABEL})*$")
WILDCARD = re.compile(rf"^\*(?:\.{LABEL})+$")

# Named ranges, checked before the default-deny fallback below. Order matters only for readability:
# the ranges do not overlap.
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
    ("reserved", ipaddress.ip_network("192.0.0.0/24")),     # IETF protocol assignments (incl. the
                                                              # NAT64/DNS64 discovery addresses)
    ("reserved", ipaddress.ip_network("198.18.0.0/15")),     # benchmarking
    ("tunneled", ipaddress.ip_network("192.88.99.0/24")),    # 6to4 relay anycast
]
_V6 = [
    ("loopback", ipaddress.ip_network("::1/128")),
    # "::" is covered here AND by the v4 fallback (it unwraps to 0.0.0.0, which 0.0.0.0/8 also names
    # "unspecified") -- this row is deliberate defence in depth, not load-bearing: removing it changes
    # no output today. "::1" has no such second cover; see the ordering comment in ip_category.
    ("unspecified", ipaddress.ip_network("::/128")),
    ("link-local", ipaddress.ip_network("fe80::/10")),
    ("private", ipaddress.ip_network("fc00::/7")),
    ("private", ipaddress.ip_network("fec0::/10")),           # site-local, deprecated (RFC 3879)
    ("multicast", ipaddress.ip_network("ff00::/8")),
    # These are blanket blocks regardless of what they embed -- see _embedded_v4's docstring for why
    # that matters for 64:ff9b::/96 and 64:ff9b:1::/48 specifically.
    ("tunneled", ipaddress.ip_network("2002::/16")),          # 6to4
    ("tunneled", ipaddress.ip_network("2001::/32")),          # Teredo
    ("tunneled", ipaddress.ip_network("64:ff9b::/96")),       # NAT64, well-known prefix (RFC 6052)
    ("tunneled", ipaddress.ip_network("64:ff9b:1::/48")),     # NAT64, local-use prefix (RFC 8215)
]

# IPv6 forms that carry an IPv4 address in their low 32 bits. Consulted only after _V6 above has had
# first refusal (see ip_category), so this never overrides a blanket block.
_EMBEDDED_V4_NETWORKS = (
    ipaddress.ip_network("::ffff:0:0/96"),     # IPv4-mapped (RFC 4291 §2.5.5.2)
    ipaddress.ip_network("::/96"),             # IPv4-compatible, deprecated (RFC 4291 §2.5.5.1) --
                                                # "::" and "::1" both sit in here, but neither reaches
                                                # this check: the _V6 table above returns for both first.
                                                # Only "::1" actually depends on that -- without its
                                                # table entry it would unwrap to 0.0.0.1 and get the
                                                # wrong name ("unspecified", from 0.0.0.0/8) instead of
                                                # "loopback". "::" unwraps to 0.0.0.0, which is already
                                                # "unspecified" either way, so its table entry is
                                                # deliberate double coverage, not something this order
                                                # depends on.
    ipaddress.ip_network("::ffff:0:0:0/96"),   # IPv4-translated / SIIT
)


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


def match(entries: tuple[AllowEntry, ...], scheme: str, host: str, port: int) -> AllowEntry | None:
    """The most specific entry that permits this target, or None.

    "Most specific" -- not "first in the list" -- because `allowPrivate` belongs to the entry that
    matched and must not leak to a broader entry that merely happens to match too: an exact host always
    outranks a wildcard, and among wildcards a longer (more specific) pattern outranks a shorter one.
    When two matching entries are equally specific -- the same host written twice, e.g. by an operator
    who merged two allowlists -- the one WITHOUT `allowPrivate` wins: on a tie, the least privileged
    result is the safe one, not whichever happened to be written first. Together this makes the result
    fully independent of the order entries were written in: reversing "*.example.com;allowPrivate,
    api.example.com", or even "api.example.com;allowPrivate, api.example.com", must not change which
    entry -- or which privilege -- wins for a request to "api.example.com".

    `host` is matched case-insensitively against the (already-lowercase) entries, but must already be
    ASCII -- Task 2's client resolves DNS and is responsible for IDNA-encoding the host before calling
    here. A non-ASCII host is refused outright rather than lowercased, because casefolding non-ASCII
    text can equate characters that should stay distinct (e.g. U+212A KELVIN SIGN lowercases to "k" and
    would otherwise alias an entry for "ok.example.com").
    """
    if not host.isascii():
        return None
    host = host.lower()
    best = None
    for entry in entries:
        if entry.matches(scheme, host, port) and (best is None or _specificity(entry) > _specificity(best)):
            best = entry
    return best


def _specificity(entry: AllowEntry) -> tuple[bool, int, bool]:
    # (is_exact, pattern_length, not allow_private): an exact host beats every wildcard regardless of
    # length; among wildcards the longer -- so more specific -- host pattern wins; and between two
    # otherwise-equal entries (a duplicated host, most plausibly), the one WITHOUT allowPrivate wins,
    # so a tie fails closed instead of resolving by list position.
    return (not entry.host.startswith("*."), len(entry.host), not entry.allow_private)


def ip_category(address: str) -> str | None:
    """Name of the blocked category, or None -- but only when the address is confirmed globally
    routable (2b design §5.3, default-deny). An address that is neither named below nor provably
    global still comes back blocked, as "non-global", instead of falling through to None. "non-global"
    means specifically this: the catch-all rule blocked it, not a named range -- an operator seeing it
    knows the table has no opinion on this address and Python's own classification was relied on.

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
        # "unspecified", either way -- see _EMBEDDED_V4_NETWORKS' comment.) Checking the table first
        # lets the existing ::1/128 entry win with no special-casing, and is also what keeps
        # 64:ff9b::/96 and 64:ff9b:1::/48 blanket blocks (see _embedded_v4's docstring) from ever
        # reaching the unwrap step at all.
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
    if semicolon and flag.strip() != "allowPrivate":
        raise PolicyError(f"알 수 없는 옵션입니다: {item}")
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
        # match()'s docstring.)
        host = ipaddress.ip_address(host).compressed
    elif _looks_like_ip_literal(host):
        # Every label is digits-only (e.g. "127.000.000.001", "1.2.3", "999.1.1.1"): this was meant to
        # be an IPv4 address and _is_ip rejected it (leading zeros, too few octets, an out-of-range
        # one), not a real hostname -- no real domain delegates an all-numeric label. Refuse it rather
        # than silently accepting it as a hostname that happens to look like a typo'd IP.
        raise PolicyError(f"IP 주소 형식이 올바르지 않습니다: {item}")
    elif not (HOSTNAME.fullmatch(host) or WILDCARD.fullmatch(host)):
        raise PolicyError(f"호스트 형식이 올바르지 않습니다: {item}")
    if port is None:
        port = DEFAULT_PORTS[scheme]
    elif port == DEFAULT_PORTS["https" if scheme == "http" else "http"]:
        # http://host:443 or https://host:80 is almost always a mistake, and the mistake silently opens
        # or closes something the operator believes is the other way round.
        raise PolicyError(f"스킴과 포트가 맞지 않습니다: {item}")
    return AllowEntry(scheme, host, port, bool(flag))


def _split(rest: str, item: str) -> tuple[str, int | None]:
    if rest.startswith("["):  # [::1]:8080
        close = rest.find("]")
        if close < 0:
            raise PolicyError(f"IPv6 주소의 괄호가 닫히지 않았습니다: {item}")
        host, tail = rest[1:close], rest[close + 1:]
        if not tail:
            return host, None
        if not tail.startswith(":"):
            raise PolicyError(f"포트 형식이 올바르지 않습니다: {item}")
        return host, _port(tail[1:], item)
    host, separator, port_text = rest.partition(":")
    return host, (_port(port_text, item) if separator else None)


def _port(text: str, item: str) -> int:
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 65535:
        raise PolicyError(f"포트 형식이 올바르지 않습니다: {item}")
    return int(text)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _looks_like_ip_literal(host: str) -> bool:
    # True when every dot-separated label is digits-only. HOSTNAME would otherwise happily accept
    # "127.000.000.001" or "1.2.3" as ordinary labels, since digits are valid label characters -- but
    # ICANN never delegates an all-numeric label, so a host shaped like this was a typo'd IP, not a
    # hostname.
    labels = host.split(".")
    return bool(labels) and all(label.isdigit() for label in labels)
