"""Egress allowlist and address classification (2b design §5.2, §5.3).

Pure: no DNS, no sockets. The client (client.py) supplies resolved addresses and asks questions here, so
the whole policy can be tested as a table without touching the network.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

DEFAULT_PORTS = {"http": 80, "https": 443}
LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
HOSTNAME = re.compile(rf"^{LABEL}(?:\.{LABEL})*$")
WILDCARD = re.compile(rf"^\*(?:\.{LABEL})+$")

# Blocked ranges by category. Order matters only for readability: the ranges do not overlap.
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
]
_V6 = [
    ("loopback", ipaddress.ip_network("::1/128")),
    ("unspecified", ipaddress.ip_network("::/128")),
    ("link-local", ipaddress.ip_network("fe80::/10")),
    ("private", ipaddress.ip_network("fc00::/7")),
    ("multicast", ipaddress.ip_network("ff00::/8")),
    # Tunnel/translation ranges embed an IPv4 address that our v4 rules would otherwise never see.
    ("tunneled", ipaddress.ip_network("2002::/16")),      # 6to4
    ("tunneled", ipaddress.ip_network("2001::/32")),      # Teredo
    ("tunneled", ipaddress.ip_network("64:ff9b::/96")),   # NAT64
]


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
        # endswith alone would let "evil-example.com" through, and would also match the bare domain;
        # requiring at least one more character means only a real sub-label matches.
        return host.endswith(suffix) and len(host) > len(suffix)


def parse_allowlist(raw: str) -> tuple[AllowEntry, ...]:
    """Parse HTTP_ALLOWLIST. Raises PolicyError so load_config can refuse to start (2b design §5.2)."""
    return tuple(_entry(item) for item in (part.strip() for part in raw.split(",")) if item)


def match(entries: tuple[AllowEntry, ...], scheme: str, host: str, port: int) -> AllowEntry | None:
    """The entry that permits this target, or None. The entry is returned, not a bool, because
    `allowPrivate` belongs to the entry that matched and must not leak to any other."""
    for entry in entries:
        if entry.matches(scheme, host.lower(), port):
            return entry
    return None


def ip_category(address: str) -> str | None:
    """Name of the blocked category, or None when the address is publicly routable.

    Only a category name is ever shown to a tenant (2b design §5.6): the address itself would tell an
    attacker what the engine can see.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "invalid"
    if isinstance(ip, ipaddress.IPv6Address):
        for name, network in _V6:
            if ip in network:
                return name
        if ip.ipv4_mapped is not None:  # ::ffff:127.0.0.1 must be classified as the address it carries
            return ip_category(str(ip.ipv4_mapped))
        return None
    for name, network in _V4:
        if ip in network:
            return name
    return None


def _entry(item: str) -> AllowEntry:
    text, _, flag = item.partition(";")
    if flag and flag.strip() != "allowPrivate":
        raise PolicyError(f"알 수 없는 옵션입니다: {item}")
    scheme, separator, rest = text.strip().lower().partition("://")
    if not separator or scheme not in DEFAULT_PORTS:
        raise PolicyError(f"http:// 또는 https:// 로 시작해야 합니다: {item}")
    host, port = _split(rest, item)
    if not host:
        raise PolicyError(f"호스트가 없습니다: {item}")
    if not (HOSTNAME.match(host) or WILDCARD.match(host) or _is_ip(host)):
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
