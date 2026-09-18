# HTTP·보안·운영(Plan 2b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the engine the `http_request` node with an SSRF-hardened egress policy, an encrypted secret store whose values never leave the request path, retention cleanup, the deployment image, and the four hardening items Plan 2a deferred.

**Architecture:** A guarded HTTP client (`engine/http/`) checks an allowlist *before* DNS, validates every resolved address, then connects to the pinned IP while keeping the original name for `Host`, SNI and certificate verification — re-running the whole check on every redirect hop. Secrets never reach the render pool: the renderer substitutes a per-run marker, and `http_request.execute()` swaps markers for real values immediately before sending and scrubs them back out of everything it returns.

**Tech Stack:** Python 3.12, httpx/httpcore (pinned-IP connect via the `sni_hostname` request extension), `pycryptodome` (AES-GCM, already a dependency), psycopg3 + Alembic, pebble, FastAPI, pytest + testcontainers, `trustme` (test CA).

**Design:** `docs/superpowers/specs/2026-09-17-http-security-ops-design.md` (이하 **2b 설계**)
**선행:** Plan 1 엔진 코어(PR #1), Plan 2a 런타임 코어(PR #2)

---

## Conventions for every task

Every task in this plan follows the same rules. They are written once here; the tasks do not repeat them.

1. **Work in `services/engine`.** `cd /d/AI/agentic-workflows/services/engine`. Commands are `uv run …`.
2. **TDD.** Write the failing test first, run it and see it fail *for the stated reason*, then implement. A test that passes before the implementation is a broken test — find out why before continuing.
3. **Every task ends green.** `uv run pytest -q` and `uv run ruff check .` both pass before the commit. A task never leaves the suite red for the next one to fix.
4. **Docker-free by default.** A test that needs Postgres or Redis takes the `pool`/`redis`/`api` fixtures, and `tests/conftest.py::pytest_collection_modifyitems` marks it `integration` automatically. Everything else must pass with `uv run pytest -q -m "not integration"` and Docker unreachable.
5. **Bound every wait.** No test may hang: use `asyncio.timeout`/`wait_for` and the `until` helper in `tests/conftest.py`, never a bare `sleep` to wait for state.
6. **ruff's select list is `["E4","E7","E9","F","I","SIM"]`.** A `# noqa: BLE001` suppresses nothing here — write the explanation as a plain comment, the way the rest of the codebase does.
7. **Commit from the repo root** (`/d/AI/agentic-workflows`) with exactly two `-m` arguments, the second being the trailer:

```bash
git commit -m "<subject>" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

8. **Do not push.** The branch is finished as a whole at the end.
9. **Korean user-facing text.** Error messages a tenant can see are Korean, like the rest of the engine. Comments and docstrings are English.
10. **Never log a secret, a prompt, a node payload, an HTTP header or a URL query string.** URLs in logs are host + path only (MVP 설계 10.1).

---

## File structure

**New**

| File | Responsibility |
|---|---|
| `engine/http/__init__.py` | (empty) |
| `engine/http/policy.py` | Allowlist parsing and matching, IP category classification. Pure, no I/O |
| `engine/http/client.py` | `GuardedClient`: resolve → validate → pinned connect → redirect loop → size caps. Raises its own exceptions, knows nothing about nodes |
| `engine/http/headers.py` | Header sanitising and redirect filtering: the strip/credential/content tables, the RFC 7230 validators. Pure, table-driven (added during Task 2 review) |
| `engine/secrets/__init__.py` | (empty) |
| `engine/secrets/crypto.py` | AES-GCM seal/open with `ENGINE_SECRET_KEY`, AAD = secret name |
| `engine/secrets/markers.py` | Nonce derivation, the marker mapping the renderer binds, marker discovery, substitution, value redaction |
| `engine/secrets/store.py` | `PostgresSecretResolver` + the `secrets` table queries |
| `engine/nodes/http_request.py` | The 9th node type |
| `engine/api/routers/secrets.py` | Write-only secrets API |
| `engine/db/migrations/versions/0002_secrets_and_retention.py` | `secrets`, `runs.purged_at` + index |
| `engine/db/migrations/versions/0003_encrypt_run_payloads.py` | `runs.inputs`/`outputs` → `bytea` |
| `engine/db/crypto.py` | Encrypt/decrypt the two run payload columns |
| `engine/db/purge.py` | Retention and orphan-checkpoint queries |
| `services/engine/Dockerfile` | Runtime image (uv multi-stage, non-root) |
| `deploy/docker-compose.yml`, `deploy/.env.example` | Deployment |

**Modified**

| File | Change |
|---|---|
| `engine/config.py` | `HTTP_ALLOWLIST`, `ENGINE_SECRET_KEY`, `RUN_DATA_RETENTION_DAYS`, `http_*` limits; API token becomes required |
| `engine/errors.py` | `HTTP_BLOCKED`, `HTTP_ERROR`, `HTTP_RESPONSE_TOO_LARGE`, `HTTP_UNSUPPORTED_MEDIA_TYPE`, `SECRET_NOT_FOUND` |
| `engine/nodes/base.py` | `NodeContext` gains `http`, `secrets`, `secret_nonce` |
| `engine/nodes/registry.py` | Register `HttpRequestNode` |
| `engine/runtime/deps.py` | `RunDeps` gains `http`, `secrets`, `secret_nonce`; `RenderFn` gains the nonce argument |
| `engine/compiler/wrapper.py` | Pass the nonce to the renderer; move `_rendered` inside the node deadline |
| `engine/templates/render.py` | `render_template` takes the secret binding through its context |
| `engine/worker/render.py` | `RenderPool.__call__` forwards the nonce; bounded queue |
| `engine/worker/worker.py` | Dedicated heartbeat connection; self-declared lease loss |
| `engine/worker/reaper.py` | Retention purge in the sweep |
| `engine/events/redact.py` | Header-name redaction |
| `engine/events/stream.py` | Degrade to polling; don't end on a stale terminal event |
| `engine/db/runs.py`, `engine/api/routers/runs.py` | Encrypted payload columns |
| `engine/validator/refs.py` | Allow `secret` in `http_request`'s url/headers/body |
| `services/engine/README.md` | New variables, deployment, operational notes |

---

## Task 0: Spike — pinned-IP TLS, and the test CA

**2b 설계 §11.1.** Everything in §5 rests on one property: connecting to a validated IP while `Host`, SNI and certificate verification still use the original name. `httpcore` reads `sni_hostname` from request extensions (verified by reading the source), but that is not the same as having seen a certificate rejected. Prove it before building on it.

**Files:**
- Modify: `services/engine/pyproject.toml` (dev dependency `trustme`)
- Test: `services/engine/tests/test_http_pinning_spike.py`

- [x]  **Step 1: Add the test CA dependency**

In `services/engine/pyproject.toml`, add to `[dependency-groups] dev`:

```toml
    "trustme>=1.1,<2",
```

Run: `uv sync`
Expected: `trustme` is installed.

- [x]  **Step 2: Write the spike**

Create `services/engine/tests/test_http_pinning_spike.py`:

```python
"""Task 0 spike (2b design §11.1): does connecting to a pinned IP keep the original name for Host, SNI
and certificate verification? Everything in the egress policy depends on it, so it is proven here with a
real TLS server before any of it is built. These tests stay in the suite as a regression guard on httpx."""
import asyncio
import ssl

import httpx
import pytest
import trustme

LOCALHOST = "127.0.0.1"
NAME = "api.internal.test"


async def _serve(context: ssl.SSLContext) -> tuple[int, asyncio.AbstractServer]:
    """A TLS server that answers any request with a 200 and closes."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except Exception:  # a client that hangs up mid-handshake is the point of one of the tests
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, LOCALHOST, 0, ssl=context)
    return server.sockets[0].getsockname()[1], server


def _server_context(authority: trustme.CA, name: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    authority.issue_cert(name).configure_cert(context)
    return context


def _client_context(authority: trustme.CA) -> ssl.SSLContext:
    context = ssl.create_default_context()
    authority.configure_trust(context)
    return context


async def test_a_pinned_ip_still_verifies_the_certificate_for_the_original_name():
    authority = trustme.CA()
    port, server = await _serve(_server_context(authority, NAME))
    async with server:
        async with httpx.AsyncClient(verify=_client_context(authority)) as client:
            response = await client.get(
                f"https://{LOCALHOST}:{port}/",
                headers={"Host": NAME},
                extensions={"sni_hostname": NAME},
            )

    assert response.status_code == 200


async def test_a_certificate_for_another_name_is_rejected():
    """The proof that verification is still on: same connection shape, wrong certificate."""
    authority = trustme.CA()
    port, server = await _serve(_server_context(authority, "someone-else.test"))
    async with server:
        async with httpx.AsyncClient(verify=_client_context(authority)) as client:
            with pytest.raises(httpx.ConnectError) as exc:
                await client.get(
                    f"https://{LOCALHOST}:{port}/",
                    headers={"Host": NAME},
                    extensions={"sni_hostname": NAME},
                )

    assert "certificate" in str(exc.value).lower() or "hostname" in str(exc.value).lower()


async def test_without_the_sni_extension_the_ip_is_what_gets_verified():
    """Documents why the extension is mandatory: without it the certificate is checked against the IP."""
    authority = trustme.CA()
    port, server = await _serve(_server_context(authority, NAME))
    async with server:
        async with httpx.AsyncClient(verify=_client_context(authority)) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"https://{LOCALHOST}:{port}/", headers={"Host": NAME})
```

- [x]  **Step 3: Run the spike**

Run: `uv run pytest tests/test_http_pinning_spike.py -q`
Expected: 3 passed.

**If any of these fail, stop and report it.** The fallback in 2b 설계 §12 is to open the socket and TLS by hand (`ssl_context.wrap_socket(server_hostname=…)`) and hand the stream to httpx, and the last resort is a dedicated egress proxy process. Do not continue with §5 until one of them is proven.

- [x]  **Step 4: Commit**

```bash
git add services/engine/pyproject.toml services/engine/uv.lock services/engine/tests/test_http_pinning_spike.py
git commit -m "test(engine): prove pinned-IP TLS keeps certificate verification" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 1: Egress policy — allowlist and IP classification

**2b 설계 §5.2, §5.3.** Pure logic, no network, no DNS. This is the half of the egress policy that can be tested exhaustively as a table.

**Files:**
- Create: `services/engine/engine/http/__init__.py` (empty), `services/engine/engine/http/policy.py`
- Modify: `services/engine/engine/config.py`
- Test: `services/engine/tests/test_http_policy.py`

- [x]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_http_policy.py`:

```python
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
```

- [x]  **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_http_policy.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.http'`.

- [x]  **Step 3: Implement the policy**

Create `services/engine/engine/http/__init__.py` (empty file) and `services/engine/engine/http/policy.py`:

```python
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
```

- [x]  **Step 4: Run the tests**

Run: `uv run pytest tests/test_http_policy.py -q`
Expected: PASS (all parametrized cases).

- [x]  **Step 5: Wire the allowlist into the config**

In `services/engine/engine/config.py`, add to the `EngineConfig` dataclass, after `max_body_bytes`:

```python
    http_allowlist: tuple[AllowEntry, ...]
    http_max_redirects: int
    http_max_request_bytes: int
    http_max_response_bytes: int
```

Add the import at the top:

```python
from engine.http.policy import AllowEntry, PolicyError, parse_allowlist
```

and inside `load_config()`, before the `return`:

```python
    try:
        allowlist = parse_allowlist(os.getenv("HTTP_ALLOWLIST") or "")
    except PolicyError as exc:
        # A typo in the allowlist must not start a server that silently blocks everything, or worse,
        # silently allows something the operator did not mean to open.
        raise ConfigError(f"HTTP_ALLOWLIST: {exc}") from None
```

and to the `EngineConfig(...)` call:

```python
        http_allowlist=allowlist,
        http_max_redirects=_int("HTTP_MAX_REDIRECTS", 3),
        http_max_request_bytes=_int("HTTP_MAX_REQUEST_BYTES", 1_000_000),
        http_max_response_bytes=_int("HTTP_MAX_RESPONSE_BYTES", 5_000_000),
```

- [x]  **Step 6: Test the config wiring**

Append to `services/engine/tests/test_config.py`:

```python
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
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.delenv("HTTP_ALLOWLIST", raising=False)

    assert load_config().http_allowlist == ()
```

Check the imports at the top of `tests/test_config.py` — it already imports `load_config` and `ConfigError`; add `pytest` if it is not there.

- [x]  **Step 7: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine/http services/engine/engine/config.py services/engine/tests/test_http_policy.py services/engine/tests/test_config.py
git commit -m "feat(engine): parse the egress allowlist and classify addresses" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 2: The guarded HTTP client

**2b 설계 §5.1, §5.4, §5.5.** The client owns the order — allowlist, then DNS, then every address, then a pinned connection — and re-runs it on every redirect hop. It raises its own exceptions; mapping them to node errors is Task 7's job, so this whole task is testable without a node.

**Files:**
- Create: `services/engine/engine/http/client.py`
- Test: `services/engine/tests/test_http_client.py`

- [x]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_http_client.py`:

```python
"""The egress policy end to end, with a fake resolver so no test ever touches real DNS."""
import asyncio
import ssl

import httpx
import pytest
import trustme

from engine.http.client import EgressBlocked, GuardedClient, ResponseTooLarge, TransportFailed
from engine.http.policy import parse_allowlist

NAME = "api.internal.test"


class FakeResolver:
    """Answers from a table. `calls` proves the allowlist runs *before* DNS."""

    def __init__(self, table: dict[str, list[str]]) -> None:
        self.table = table
        self.calls: list[str] = []

    async def resolve(self, host: str) -> list[str]:
        self.calls.append(host)
        try:
            return self.table[host]
        except KeyError:
            raise OSError(f"no such host: {host}") from None


async def _serve(handler, context: ssl.SSLContext | None = None) -> tuple[int, asyncio.AbstractServer]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=context)
    return server.sockets[0].getsockname()[1], server


def _responder(body: bytes = b"ok", status: str = "200 OK", headers: str = "") -> object:
    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            head = f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n{headers}Connection: close\r\n\r\n"
            writer.write(head.encode() + body)
            await writer.drain()
        finally:
            writer.close()

    return handle


def _client(allowlist: str, resolver, **kwargs) -> GuardedClient:
    return GuardedClient(parse_allowlist(allowlist), resolver=resolver, **kwargs)


async def test_a_host_outside_the_allowlist_is_never_resolved():
    resolver = FakeResolver({"evil.test": ["93.184.216.34"]})
    client = _client("https://api.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="https://evil.test/x", headers={}, body=None, timeout_sec=5)

    assert exc.value.category == "allowlist"
    assert resolver.calls == []  # the host name itself must not become a DNS exfiltration channel
    await client.aclose()


async def test_one_forbidden_address_blocks_the_whole_request():
    resolver = FakeResolver({"api.example.com": ["93.184.216.34", "10.0.0.5"]})
    client = _client("https://api.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="https://api.example.com/x", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "private"
    await client.aclose()


async def test_a_bare_ip_url_is_blocked_by_the_allowlist():
    resolver = FakeResolver({})
    client = _client("https://api.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="http://169.254.169.254/latest/meta-data/", headers={},
                             body=None, timeout_sec=5)

    assert exc.value.category == "allowlist"
    assert resolver.calls == []
    await client.aclose()


async def test_an_allowlisted_ip_literal_is_still_classified():
    """An IP literal in the allowlist is not a way around the address rules."""
    resolver = FakeResolver({})
    client = _client("http://169.254.169.254", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="http://169.254.169.254/", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "link-local"
    await client.aclose()


async def test_allow_private_does_not_open_the_metadata_endpoint():
    """allowPrivate means "a machine on my network", not "any address the policy would block"."""
    resolver = FakeResolver({})
    client = _client("http://169.254.169.254;allowPrivate", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="http://169.254.169.254/latest/meta-data/", headers={},
                             body=None, timeout_sec=5)

    assert exc.value.category == "link-local"
    await client.aclose()


async def test_allow_private_permits_only_its_own_entry():
    resolver = FakeResolver({"internal.test": ["10.0.0.7"], "other.test": ["10.0.0.8"]})
    client = _client("http://internal.test:8080;allowPrivate, http://other.test:8080", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="http://other.test:8080/x", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "private"
    await client.aclose()


async def test_a_request_reaches_the_pinned_address_over_tls():
    authority = trustme.CA()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    authority.issue_cert(NAME).configure_cert(context)
    port, server = await _serve(_responder(b'{"ok": true}', headers="Content-Type: application/json\r\n"),
                                context)
    verify = ssl.create_default_context()
    authority.configure_trust(verify)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"https://{NAME}:{port};allowPrivate", resolver, verify=verify)

    async with server:
        response = await client.request(method="GET", url=f"https://{NAME}:{port}/x", headers={},
                                        body=None, timeout_sec=5)

    assert (response.status, response.body) == (200, {"ok": True})
    assert resolver.calls == [NAME]
    await client.aclose()


async def test_a_redirect_to_a_private_address_is_blocked():
    port, server = await _serve(_responder(b"", status="302 Found",
                                           headers="Location: http://internal.test:9000/x\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"], "internal.test": ["10.0.0.5"]})
    client = _client(f"http://{NAME}:{port};allowPrivate, http://internal.test:9000", resolver)

    async with server:
        with pytest.raises(EgressBlocked) as exc:
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert exc.value.category == "private"
    await client.aclose()


async def test_a_redirect_to_a_host_outside_the_allowlist_is_blocked():
    port, server = await _serve(_responder(b"", status="302 Found",
                                           headers="Location: http://evil.test/x\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"], "evil.test": ["93.184.216.34"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        with pytest.raises(EgressBlocked) as exc:
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert exc.value.category == "allowlist"
    assert "evil.test" not in resolver.calls
    await client.aclose()


async def test_too_many_redirects_is_blocked():
    port = 0
    holder: dict = {}

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        location = f"http://{NAME}:{holder['port']}/next"
        head = f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        writer.write(head.encode())
        await writer.drain()
        writer.close()

    port, server = await _serve(handle)
    holder["port"] = port
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver, max_redirects=3)

    async with server:
        with pytest.raises(EgressBlocked) as exc:
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert exc.value.category == "too-many-redirects"
    await client.aclose()


async def test_an_https_to_http_downgrade_is_blocked():
    authority = trustme.CA()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    authority.issue_cert(NAME).configure_cert(context)
    port, server = await _serve(_responder(b"", status="302 Found",
                                           headers=f"Location: http://{NAME}:9000/x\r\n"), context)
    verify = ssl.create_default_context()
    authority.configure_trust(verify)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"https://{NAME}:{port};allowPrivate, http://{NAME}:9000;allowPrivate", resolver,
                     verify=verify)

    async with server:
        with pytest.raises(EgressBlocked) as exc:
            await client.request(method="GET", url=f"https://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert exc.value.category == "downgrade"
    await client.aclose()


async def test_a_response_over_the_cap_stops_being_read():
    port, server = await _serve(_responder(b"x" * 20_000))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver, max_response_bytes=1_000)

    async with server:
        with pytest.raises(ResponseTooLarge):
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    await client.aclose()


async def test_an_oversized_request_body_never_leaves_the_process():
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:9000;allowPrivate", resolver, max_request_bytes=100)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="POST", url=f"http://{NAME}:9000/x", headers={}, body="y" * 200,
                             timeout_sec=5)

    assert exc.value.category == "request-too-large"
    assert resolver.calls == []
    await client.aclose()


async def test_a_connection_failure_is_a_transport_error():
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:9;allowPrivate", resolver)  # port 9 (discard) refuses

    with pytest.raises(TransportFailed):
        await client.request(method="GET", url=f"http://{NAME}:9/x", headers={}, body=None, timeout_sec=2)

    await client.aclose()


async def test_a_name_that_does_not_resolve_is_blocked():
    resolver = FakeResolver({})
    client = _client("https://api.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="https://api.example.com/x", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "dns"
    await client.aclose()


async def test_a_text_response_comes_back_as_a_string():
    port, server = await _serve(_responder(b"hello", headers="Content-Type: text/plain\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        response = await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={},
                                        body=None, timeout_sec=5)

    assert response.body == "hello" and response.headers["content-type"] == "text/plain"
    await client.aclose()


async def test_an_unsupported_content_type_is_reported():
    from engine.http.client import UnsupportedMedia

    port, server = await _serve(_responder(b"\x00\x01", headers="Content-Type: image/png\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        with pytest.raises(UnsupportedMedia):
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    await client.aclose()
```

- [x]  **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_http_client.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.http.client'`.

- [x]  **Step 3: Implement the client**

Create `services/engine/engine/http/client.py`:

```python
"""The guarded HTTP client (2b design §5).

Order is the whole point: allowlist, then DNS, then every resolved address, then a connection pinned to an
address that passed — repeated in full for every redirect hop. Checking the allowlist before DNS means an
unapproved host name is never even looked up, so the name cannot be used as a DNS exfiltration channel.

Raises its own exceptions; `engine/nodes/http_request.py` turns them into node errors. Nothing here knows
about nodes, so the policy can be tested without one.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from engine.http.policy import DEFAULT_PORTS, AllowEntry, find_entry, ip_category
from engine.jsondata import parse_json

log = logging.getLogger(__name__)

# allowPrivate exempts only the categories that mean "a machine on the operator's own network".
# link-local is deliberately absent: 169.254.169.254 is the cloud metadata endpoint, and an operator who
# opened one internal API did not ask for that (2b design §5.2).
ALLOW_PRIVATE_CATEGORIES = frozenset({"private", "loopback", "cgnat"})
REDIRECTS = {301, 302, 303, 307, 308}
BODYLESS = {301, 302, 303}  # these become a GET without a body, as every browser and client does
MAX_DECODED_CHARS = 5_000_000


class EgressBlocked(Exception):
    """Policy refused the request. `category` is all a tenant is ever told (2b design §5.6)."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class ResponseTooLarge(Exception):
    """The response passed the byte cap and was abandoned unread."""


class UnsupportedMedia(Exception):
    """Neither JSON nor text: the DSL type system has nowhere to put it."""


class TransportFailed(Exception):
    """Connection, TLS or read failure. Retryable, unlike everything else here."""


class Resolver(Protocol):
    async def resolve(self, host: str) -> list[str]: ...


class SystemResolver:
    """getaddrinfo on the event loop's executor, with a cap so a burst of runs cannot take every thread
    (the same executor runs `analyze` for the API)."""

    def __init__(self, limit: int = 8) -> None:
        self._semaphore = asyncio.Semaphore(limit)

    async def resolve(self, host: str) -> list[str]:
        loop = asyncio.get_running_loop()
        async with self._semaphore:
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(info[4][0] for info in infos))


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]  # lowercased names
    body: Any


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    path: str
    address: str  # the validated address to connect to


class GuardedClient:
    def __init__(self, allowlist: tuple[AllowEntry, ...], *, resolver: Resolver | None = None,
                 max_redirects: int = 3, max_request_bytes: int = 1_000_000,
                 max_response_bytes: int = 5_000_000, verify: ssl.SSLContext | bool = True) -> None:
        self._allowlist = allowlist
        self._resolver = resolver or SystemResolver()
        self._max_redirects = max_redirects
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        # follow_redirects=False: every hop is re-checked here. cookies are never stored; trust_env=False
        # so an ambient HTTP_PROXY cannot route around the pinning.
        self._client = httpx.AsyncClient(follow_redirects=False, trust_env=False, verify=verify,
                                         cookies=None)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, *, method: str, url: str, headers: dict[str, str], body: str | None,
                      timeout_sec: float) -> HttpResponse:
        if body is not None and len(body.encode("utf-8")) > self._max_request_bytes:
            raise EgressBlocked("request-too-large")
        current_method, current_body, current_url = method.upper(), body, url
        for _ in range(self._max_redirects + 1):
            target = await self._check(current_url, previous_scheme=urlsplit(current_url).scheme)
            response = await self._send(target, current_method, headers, current_body, timeout_sec)
            if response.status_code not in REDIRECTS or "location" not in response.headers:
                return await self._read(response)
            await response.aclose()
            location = urljoin(current_url, response.headers["location"])
            if urlsplit(current_url).scheme == "https" and urlsplit(location).scheme == "http":
                raise EgressBlocked("downgrade")
            if response.status_code in BODYLESS and current_method not in ("GET", "HEAD"):
                current_method, current_body = "GET", None
            current_url = location
        raise EgressBlocked("too-many-redirects")

    async def _check(self, url: str, *, previous_scheme: str) -> _Target:
        parts = urlsplit(url)
        if parts.scheme not in DEFAULT_PORTS:
            raise EgressBlocked("scheme")
        host = (parts.hostname or "").lower()
        if not host:
            raise EgressBlocked("allowlist")
        port = parts.port or DEFAULT_PORTS[parts.scheme]
        entry = find_entry(self._allowlist, parts.scheme, host, port)
        if entry is None:  # before DNS, deliberately
            raise EgressBlocked("allowlist")
        addresses = [host] if ip_category(host) != "invalid" else await self._resolve(host)
        for address in addresses:
            category = ip_category(address)
            if category is not None and not (entry.allow_private and category in ALLOW_PRIVATE_CATEGORIES):
                raise EgressBlocked(category)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return _Target(parts.scheme, host, port, path, addresses[0])

    async def _resolve(self, host: str) -> list[str]:
        try:
            addresses = await self._resolver.resolve(host)
        except OSError:
            raise EgressBlocked("dns") from None
        if not addresses:
            raise EgressBlocked("dns")
        return addresses

    async def _send(self, target: _Target, method: str, headers: dict[str, str], body: str | None,
                    timeout_sec: float) -> httpx.Response:
        literal = f"[{target.address}]" if ":" in target.address else target.address
        authority = target.host if target.port == DEFAULT_PORTS[target.scheme] else f"{target.host}:{target.port}"
        pinned = f"{target.scheme}://{literal}:{target.port}{target.path}"
        request = self._client.build_request(
            method, pinned,
            headers={**headers, "Host": authority},
            content=body.encode("utf-8") if body is not None else None,
            timeout=httpx.Timeout(timeout_sec, connect=timeout_sec),
            # httpcore hands this to the TLS layer as `server_hostname`, so SNI *and* certificate
            # verification use the real name even though the socket goes to the validated address.
            extensions={"sni_hostname": target.host},
        )
        try:
            return await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            # Never let httpx's message out: it carries the full URL, query string included.
            log.warning("http_request transport failure for %s%s: %s", target.host, target.path,
                        type(exc).__name__)
            raise TransportFailed(type(exc).__name__) from None

    async def _read(self, response: httpx.Response) -> HttpResponse:
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._max_response_bytes:
                    raise ResponseTooLarge(str(self._max_response_bytes))
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise TransportFailed(type(exc).__name__) from None
        finally:
            await response.aclose()
        headers = {name.lower(): value for name, value in response.headers.items()}
        return HttpResponse(response.status_code, headers, _decode(b"".join(chunks), headers))


def _decode(raw: bytes, headers: dict[str, str]) -> Any:
    media = headers.get("content-type", "").split(";")[0].strip().lower()
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise UnsupportedMedia(media or "binary") from None
    if len(text) > MAX_DECODED_CHARS:
        raise ResponseTooLarge(str(MAX_DECODED_CHARS))
    if media == "application/json" or media.endswith("+json"):
        try:
            return parse_json(text)  # the engine's strict parser: no NaN, no duplicate keys, bounded depth
        except ValueError as exc:
            raise UnsupportedMedia(f"invalid json: {exc}") from None
    if media.startswith("text/"):
        return text
    raise UnsupportedMedia(media or "unknown")
```

- [x]  **Step 4: Run the tests**

Run: `uv run pytest tests/test_http_client.py -q`
Expected: PASS (17 tests).

- [x]  **Step 5: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine/http/client.py services/engine/tests/test_http_client.py
git commit -m "feat(engine): add the guarded HTTP client with pinned-address egress" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 3: Migration 0002 — secrets table and retention columns

**2b 설계 §4.1, §7.** Schema only, so the suite stays green: the columns exist before anything writes them.

**Files:**
- Create: `services/engine/engine/db/migrations/versions/0002_secrets_and_retention.py`
- Test: `services/engine/tests/test_db_schema.py` (append)

- [x]  **Step 1: Write the failing test**

Append to `services/engine/tests/test_db_schema.py`:

```python
async def test_the_secrets_table_and_retention_columns_exist(pool):
    async with pool.connection() as conn:
        columns = await (await conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='secrets'")).fetchall()
        purged = await (await conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name='runs' AND column_name='purged_at'")).fetchall()
        key = await (await conn.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a"
            "   ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
            " WHERE i.indrelid = 'secrets'::regclass AND i.indisprimary ORDER BY a.attname")).fetchall()

    assert {row["column_name"] for row in columns} == {
        "workspace_id", "name", "ciphertext", "created_at", "updated_at"}
    assert [row["column_name"] for row in purged] == ["purged_at"]
    assert [row["attname"] for row in key] == ["name", "workspace_id"]
```

- [x]  **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_db_schema.py -q`
Expected: FAIL — the `secrets` column set comes back empty.

- [x]  **Step 3: Write the migration**

Create `services/engine/engine/db/migrations/versions/0002_secrets_and_retention.py`:

```python
"""Secrets and retention bookkeeping (2b design §4.1, §7).

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UP = """
CREATE TABLE secrets (
    workspace_id uuid NOT NULL,
    name text NOT NULL,
    ciphertext bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, name)
);

ALTER TABLE runs ADD COLUMN purged_at timestamptz;
-- The retention sweep asks for finished runs that have not been purged yet; without purged_at in the
-- index it would rescan everything it has already cleaned on every sweep, forever.
CREATE INDEX runs_retention_idx ON runs (finished_at) WHERE purged_at IS NULL AND finished_at IS NOT NULL;
"""

DOWN = """
DROP INDEX runs_retention_idx;
ALTER TABLE runs DROP COLUMN purged_at;
DROP TABLE secrets;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
```

- [x]  **Step 4: Run the test**

Run: `uv run pytest tests/test_db_schema.py -q`
Expected: PASS.

- [x]  **Step 5: Check the truncation list**

`tests/conftest.py` truncates `APP_TABLES` between tests. Add `secrets` so a secret written by one test cannot leak into another:

```python
APP_TABLES = ("run_events", "node_runs", "runs", "workflow_versions", "workflows", "secrets")
```

- [x]  **Step 6: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine/db/migrations/versions/0002_secrets_and_retention.py services/engine/tests/test_db_schema.py services/engine/tests/conftest.py
git commit -m "feat(engine): add the secrets table and retention columns" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: Secret storage — AES-GCM and the resolver

**2b 설계 §4.1.** `pycryptodome` is already a dependency (Plan 2a uses it for checkpoint encryption), so no new package.

**Files:**
- Create: `services/engine/engine/secrets/__init__.py` (empty), `services/engine/engine/secrets/crypto.py`, `services/engine/engine/secrets/store.py`
- Modify: `services/engine/engine/config.py`, `services/engine/tests/conftest.py`
- Test: `services/engine/tests/test_secrets_store.py`

- [x]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_secrets_store.py`:

```python
import pytest

from engine.secrets.crypto import SecretCryptoError, open_secret, seal_secret
from engine.secrets.store import PostgresSecretResolver, delete_secret, list_secrets, put_secret

KEY = b"0123456789abcdef0123456789abcdef"


def test_a_sealed_secret_round_trips():
    sealed = seal_secret(KEY, "TOKEN", "hunter2-secret")

    assert open_secret(KEY, "TOKEN", sealed) == "hunter2-secret"


def test_the_ciphertext_does_not_contain_the_plaintext():
    assert b"hunter2-secret" not in seal_secret(KEY, "TOKEN", "hunter2-secret")


def test_two_seals_of_the_same_value_differ():
    """A fresh nonce each time: equal ciphertexts would tell an attacker two secrets are the same."""
    assert seal_secret(KEY, "TOKEN", "same-value") != seal_secret(KEY, "TOKEN", "same-value")


def test_a_ciphertext_moved_to_another_name_will_not_open():
    """The name is the AAD, so a row copied onto a different name is detected."""
    sealed = seal_secret(KEY, "TOKEN", "hunter2-secret")

    with pytest.raises(SecretCryptoError):
        open_secret(KEY, "OTHER", sealed)


def test_a_tampered_ciphertext_will_not_open():
    sealed = bytearray(seal_secret(KEY, "TOKEN", "hunter2-secret"))
    sealed[-1] ^= 0xFF

    with pytest.raises(SecretCryptoError):
        open_secret(KEY, "TOKEN", bytes(sealed))


def test_a_wrong_key_will_not_open():
    sealed = seal_secret(KEY, "TOKEN", "hunter2-secret")

    with pytest.raises(SecretCryptoError):
        open_secret(b"f" * 32, "TOKEN", sealed)


async def test_a_secret_is_stored_encrypted_and_resolved_back(pool):
    async with pool.connection() as conn:
        await put_secret(conn, key=KEY, name="TOKEN", value="hunter2-secret")
        row = await (await conn.execute("SELECT ciphertext FROM secrets WHERE name='TOKEN'")).fetchone()

    assert b"hunter2-secret" not in bytes(row["ciphertext"])
    assert await PostgresSecretResolver(pool, KEY).resolve({"TOKEN"}) == {"TOKEN": "hunter2-secret"}


async def test_putting_a_secret_twice_replaces_it(pool):
    async with pool.connection() as conn:
        await put_secret(conn, key=KEY, name="TOKEN", value="first-value")
        await put_secret(conn, key=KEY, name="TOKEN", value="second-value")

    assert await PostgresSecretResolver(pool, KEY).resolve({"TOKEN"}) == {"TOKEN": "second-value"}


async def test_resolving_a_missing_secret_leaves_it_out(pool):
    async with pool.connection() as conn:
        await put_secret(conn, key=KEY, name="TOKEN", value="hunter2-secret")

    assert await PostgresSecretResolver(pool, KEY).resolve({"TOKEN", "GONE"}) == {"TOKEN": "hunter2-secret"}


async def test_a_row_sealed_with_another_key_reads_as_missing(pool):
    """A rotated key must not turn into an infrastructure error: the node reports SECRET_NOT_FOUND."""
    async with pool.connection() as conn:
        await put_secret(conn, key=b"f" * 32, name="TOKEN", value="hunter2-secret")

    assert await PostgresSecretResolver(pool, KEY).resolve({"TOKEN"}) == {}


async def test_listing_returns_names_and_times_but_never_values(pool):
    async with pool.connection() as conn:
        await put_secret(conn, key=KEY, name="TOKEN", value="hunter2-secret")
        rows = await list_secrets(conn)

    assert [row["name"] for row in rows] == ["TOKEN"]
    assert "ciphertext" not in rows[0]


async def test_deleting_reports_whether_it_applied(pool):
    async with pool.connection() as conn:
        await put_secret(conn, key=KEY, name="TOKEN", value="hunter2-secret")

        assert await delete_secret(conn, "TOKEN") is True
        assert await delete_secret(conn, "TOKEN") is False
```

- [x]  **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_secrets_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.secrets'`.

- [x]  **Step 3: Implement the crypto**

Create `services/engine/engine/secrets/__init__.py` (empty) and `services/engine/engine/secrets/crypto.py`:

```python
"""Secret values at rest (2b design §4.1).

AES-GCM under ENGINE_SECRET_KEY, deliberately a different key from LANGGRAPH_AES_KEY: if one leaks the
other still holds. The secret's name is the AAD, so a ciphertext copied onto another row fails to open
instead of silently becoming that other secret.
"""
from __future__ import annotations

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

NONCE_BYTES = 12
TAG_BYTES = 16


class SecretCryptoError(Exception):
    """The ciphertext does not open: wrong key, wrong name, or tampering."""


def seal_secret(key: bytes, name: str, value: str) -> bytes:
    cipher = AES.new(key, AES.MODE_GCM, nonce=get_random_bytes(NONCE_BYTES))
    cipher.update(name.encode("utf-8"))
    body, tag = cipher.encrypt_and_digest(value.encode("utf-8"))
    return cipher.nonce + tag + body


def open_secret(key: bytes, name: str, sealed: bytes) -> str:
    if len(sealed) < NONCE_BYTES + TAG_BYTES:
        raise SecretCryptoError("ciphertext is too short")
    nonce = sealed[:NONCE_BYTES]
    tag = sealed[NONCE_BYTES:NONCE_BYTES + TAG_BYTES]
    body = sealed[NONCE_BYTES + TAG_BYTES:]
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        cipher.update(name.encode("utf-8"))
        return cipher.decrypt_and_verify(body, tag).decode("utf-8")
    except (ValueError, KeyError, UnicodeDecodeError) as exc:
        # Deliberately no detail: which of the three went wrong is information an attacker would use.
        raise SecretCryptoError("secret could not be decrypted") from exc
```

- [x]  **Step 4: Implement the store**

Create `services/engine/engine/secrets/store.py`:

```python
"""The secrets table and the resolver http_request uses (2b design §4).

Values are fetched and decrypted just in time, inside one node execution. Nothing here caches them: a
worker must not hold plaintext across a run.
"""
from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from engine.db.workflows import WORKSPACE
from engine.secrets.crypto import SecretCryptoError, open_secret, seal_secret


async def put_secret(conn: AsyncConnection, *, key: bytes, name: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO secrets (workspace_id, name, ciphertext) VALUES (%s, %s, %s)"
        " ON CONFLICT (workspace_id, name)"
        " DO UPDATE SET ciphertext = EXCLUDED.ciphertext, updated_at = now()",
        (WORKSPACE, name, seal_secret(key, name, value)),
    )


async def delete_secret(conn: AsyncConnection, name: str) -> bool:
    row = await (await conn.execute(
        "DELETE FROM secrets WHERE workspace_id=%s AND name=%s RETURNING name", (WORKSPACE, name)
    )).fetchone()
    return row is not None


async def list_secrets(conn: AsyncConnection) -> list[dict[str, Any]]:
    """Names and times only. No query anywhere returns `ciphertext` to a caller."""
    return await (await conn.execute(
        "SELECT name, created_at, updated_at FROM secrets WHERE workspace_id=%s ORDER BY name",
        (WORKSPACE,),
    )).fetchall()


async def secret_names(conn: AsyncConnection) -> set[str]:
    """Used by run creation to reject a workflow that references a secret nobody stored."""
    rows = await (await conn.execute(
        "SELECT name FROM secrets WHERE workspace_id=%s", (WORKSPACE,))).fetchall()
    return {row["name"] for row in rows}


class PostgresSecretResolver:
    """Matches engine.nodes.base.SecretResolver."""

    def __init__(self, pool: AsyncConnectionPool, key: bytes) -> None:
        self._pool = pool
        self._key = key

    async def resolve(self, names: set[str]) -> dict[str, str]:
        """Decrypted values for the names that exist. A missing name is simply absent from the result —
        the caller decides what that means (the node fails the attempt with SECRET_NOT_FOUND)."""
        if not names:
            return {}
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT name, ciphertext FROM secrets WHERE workspace_id=%s AND name = ANY(%s)",
                (WORKSPACE, sorted(names)),
            )).fetchall()
        resolved: dict[str, str] = {}
        for row in rows:
            try:
                resolved[row["name"]] = open_secret(self._key, row["name"], bytes(row["ciphertext"]))
            except SecretCryptoError:
                # A row sealed with a key that has since been rotated reads as missing, so the node fails
                # the tenant's attempt instead of raising an infrastructure error at the worker.
                continue
        return resolved
```

- [x]  **Step 5: Add the key to the config**

In `services/engine/engine/config.py`, add to `EngineConfig`:

```python
    secret_key: bytes | None
```

and in `load_config()`, right after the existing `LANGGRAPH_AES_KEY` check:

```python
    secret_key_text = os.getenv("ENGINE_SECRET_KEY")
    if not secret_key_text and not dev_insecure:
        raise ConfigError("ENGINE_SECRET_KEY is required (set ENGINE_DEV_INSECURE=1 only for development)")
    secret_key = secret_key_text.encode("utf-8") if secret_key_text else None
    if secret_key is not None and len(secret_key) not in (16, 24, 32):
        raise ConfigError("ENGINE_SECRET_KEY must be 16, 24 or 32 bytes")
```

and to the `EngineConfig(...)` call: `secret_key=secret_key,`.

- [x]  **Step 6: Give the fixtures a key**

In `services/engine/tests/conftest.py`, in the `db_url` fixture next to the existing `LANGGRAPH_AES_KEY` line:

```python
        os.environ.setdefault("ENGINE_SECRET_KEY", "1" * 32)
```

Then fix every test that builds an environment by hand and calls `load_config()` — `tests/test_config.py`, `tests/test_worker_main.py`, `tests/test_api_main.py`. Set `ENGINE_SECRET_KEY` there, except where the test is specifically about a missing key.

Run: `uv run pytest -q tests/test_config.py tests/test_worker_main.py tests/test_api_main.py`
Expected: PASS. If the existing "missing LANGGRAPH_AES_KEY" test now fails on the secret key instead, give it an `ENGINE_SECRET_KEY` so it still tests what its name says.

- [x]  **Step 7: Add config tests**

Append to `services/engine/tests/test_config.py`:

```python
def test_a_missing_secret_key_is_refused_unless_dev_insecure(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.delenv("ENGINE_SECRET_KEY", raising=False)

    with pytest.raises(ConfigError):
        load_config()

    monkeypatch.setenv("ENGINE_DEV_INSECURE", "1")
    assert load_config().secret_key is None


def test_a_secret_key_of_the_wrong_length_is_refused(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("ENGINE_SECRET_KEY", "too-short")

    with pytest.raises(ConfigError):
        load_config()
```

- [x]  **Step 8: Run everything and commit**

Run: `uv run pytest tests/test_secrets_store.py -q` → PASS.
Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine/secrets services/engine/engine/config.py services/engine/tests
git commit -m "feat(engine): store secrets encrypted and resolve them just in time" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: The write-only secrets API

**2b 설계 §4.2.** No endpoint returns a value — not a masked one, none.

**Files:**
- Create: `services/engine/engine/api/routers/secrets.py`
- Modify: `services/engine/engine/api/app.py`
- Test: `services/engine/tests/test_api_secrets.py`

- [x]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_api_secrets.py`:

```python
import logging

import pytest

SECRET = "hunter2-secret-value"


async def test_a_secret_can_be_stored_and_listed_but_never_read_back(api):
    stored = await api.put("/secrets/API_TOKEN", json={"value": SECRET})
    listed = await api.get("/secrets")

    assert stored.status_code == 204
    body = listed.json()
    assert [item["name"] for item in body["secrets"]] == ["API_TOKEN"]
    assert SECRET not in listed.text
    assert "value" not in body["secrets"][0] and "ciphertext" not in body["secrets"][0]


async def test_there_is_no_endpoint_that_returns_a_value(api):
    assert (await api.get("/secrets/API_TOKEN")).status_code in (404, 405)


async def test_replacing_a_secret_keeps_one_row(api):
    await api.put("/secrets/API_TOKEN", json={"value": SECRET})
    await api.put("/secrets/API_TOKEN", json={"value": "another-secret-value"})

    assert len((await api.get("/secrets")).json()["secrets"]) == 1


async def test_a_secret_can_be_deleted(api):
    await api.put("/secrets/API_TOKEN", json={"value": SECRET})

    assert (await api.delete("/secrets/API_TOKEN")).status_code == 204
    assert (await api.delete("/secrets/API_TOKEN")).status_code == 404
    assert (await api.get("/secrets")).json()["secrets"] == []


@pytest.mark.parametrize("name", ["lower", "1LEADING", "WITH-DASH", "A" * 65])
async def test_a_bad_name_is_rejected(api, name):
    assert (await api.put(f"/secrets/{name}", json={"value": SECRET})).status_code in (404, 422)


@pytest.mark.parametrize("value", ["short", "", "x" * 4097])
async def test_a_value_outside_the_length_bounds_is_rejected(api, value):
    response = await api.put("/secrets/API_TOKEN", json={"value": value})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_ERROR"


async def test_a_non_string_value_is_rejected(api):
    assert (await api.put("/secrets/API_TOKEN", json={"value": 12345678})).status_code == 422


async def test_the_value_never_appears_in_the_logs(api, caplog):
    with caplog.at_level(logging.DEBUG):
        await api.put("/secrets/API_TOKEN", json={"value": SECRET})
        await api.get("/secrets")

    assert SECRET not in caplog.text
```

- [x]  **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_api_secrets.py -q`
Expected: FAIL — every request answers 404 because the router does not exist.

- [x]  **Step 3: Implement the router**

Create `services/engine/engine/api/routers/secrets.py`:

```python
"""Secrets: write-only (2b design §4.2).

There is no read endpoint. `GET /secrets` returns names and times so the editor can offer
`{{secret.NAME}}` completions; the value exists in exactly two places — the encrypted row, and the inside
of one http_request call.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request, Response

from engine.api.body import field, read_json, require_object
from engine.api.errors import ApiError
from engine.secrets import store as secret_db

router = APIRouter()

NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MIN_VALUE_CHARS = 8  # below this, value-based redaction (2b design §6) would scrub unrelated text
MAX_VALUE_CHARS = 4096


def _name(raw: str) -> str:
    if not NAME.match(raw):
        raise ApiError(404, "NOT_FOUND", "시크릿을 찾을 수 없습니다")
    return raw


def _key(request: Request) -> bytes:
    key = request.app.state.config.secret_key
    if key is None:  # only reachable with ENGINE_DEV_INSECURE=1
        raise ApiError(503, "INTERNAL", "시크릿 저장소가 설정되지 않았습니다")
    return key


@router.put("/secrets/{name}", status_code=204)
async def put_secret(name: str, request: Request) -> Response:
    name = _name(name)
    key = _key(request)
    body = require_object(await read_json(request))
    value = field(body, "value", str)
    if not MIN_VALUE_CHARS <= len(value) <= MAX_VALUE_CHARS:
        raise ApiError(422, "REQUEST_ERROR",
                       f"시크릿 값은 {MIN_VALUE_CHARS}~{MAX_VALUE_CHARS}자여야 합니다")
    async with request.app.state.pool.connection() as conn:
        await secret_db.put_secret(conn, key=key, name=name, value=value)
    return Response(status_code=204)


@router.delete("/secrets/{name}", status_code=204)
async def delete_secret(name: str, request: Request) -> Response:
    name = _name(name)
    async with request.app.state.pool.connection() as conn:
        if not await secret_db.delete_secret(conn, name):
            raise ApiError(404, "NOT_FOUND", "시크릿을 찾을 수 없습니다")
    return Response(status_code=204)


@router.get("/secrets")
async def list_secrets(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        rows = await secret_db.list_secrets(conn)
    return {"secrets": [{"name": row["name"], "createdAt": row["created_at"].isoformat(),
                         "updatedAt": row["updated_at"].isoformat()} for row in rows]}
```

- [x]  **Step 4: Register it**

In `services/engine/engine/api/app.py`, import `secrets` alongside the other routers and add `app.include_router(secrets.router)` next to the existing `include_router` calls.

- [x]  **Step 5: Run the tests**

Run: `uv run pytest tests/test_api_secrets.py -q`
Expected: PASS.

- [x]  **Step 6: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine/api services/engine/tests/test_api_secrets.py
git commit -m "feat(engine): add the write-only secrets API" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 6: Marker rendering

**2b 설계 §4.3.** The renderer substitutes a per-run marker, never a value — this is what keeps secrets out of the render subprocess, the run state, the checkpoint and `node_runs`.

**Files:**
- Create: `services/engine/engine/secrets/markers.py`
- Modify: `services/engine/engine/compiler/wrapper.py`, `services/engine/engine/runtime/deps.py`, `services/engine/engine/nodes/base.py`, `services/engine/engine/worker/render.py`, `services/engine/engine/worker/worker.py`
- Test: `services/engine/tests/test_secrets_markers.py`, `services/engine/tests/test_compiler_wrapper.py` (append)

- [x]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_secrets_markers.py`:

```python
import pytest

from engine.secrets.markers import (
    SecretMarkers,
    find_names,
    marker_for,
    nonce_for,
    redact_values,
    substitute,
)

KEY = b"0123456789abcdef0123456789abcdef"
NONCE = nonce_for("run-1", KEY)


def test_the_nonce_is_stable_for_a_run():
    """A replay after a worker restart must render identically, or the record and the checkpoint diverge."""
    assert nonce_for("run-1", KEY) == NONCE
    assert nonce_for("run-2", KEY) != NONCE


def test_the_marker_mapping_answers_any_valid_name():
    markers = SecretMarkers(NONCE)

    assert markers["API_TOKEN"] == marker_for("API_TOKEN", NONCE)
    with pytest.raises(KeyError):
        markers["not-a-valid-name"]


def test_names_are_found_only_for_this_runs_nonce():
    text = f"Bearer {marker_for('API_TOKEN', NONCE)} and {marker_for('OTHER', 'f' * 16)}"

    assert find_names(text, NONCE) == {"API_TOKEN"}


def test_substitution_replaces_only_this_runs_markers():
    other = marker_for("OTHER", "f" * 16)
    text = f"{marker_for('API_TOKEN', NONCE)}|{other}"

    assert substitute(text, {"API_TOKEN": "real-value"}, NONCE) == f"real-value|{other}"


def test_an_unresolved_marker_is_left_alone():
    text = marker_for("API_TOKEN", NONCE)

    assert substitute(text, {}, NONCE) == text


def test_redaction_replaces_a_value_anywhere_in_a_string():
    value = {"message": "token=abc12345", "other": "xxabc12345yy", "n": 1}

    assert redact_values(value, ["abc12345"]) == {
        "message": "token=[REDACTED]", "other": "xx[REDACTED]yy", "n": 1}


def test_redaction_reaches_nested_values_and_keys():
    value = {"a": [{"b": "see abc12345"}], "abc12345": "x"}

    assert redact_values(value, ["abc12345"]) == {"a": [{"b": "see [REDACTED]"}], "[REDACTED]": "x"}


def test_overlapping_secrets_are_replaced_longest_first():
    """Shortest-first would cut the long one in half and leave the rest of it exposed."""
    assert redact_values("abc12345678", ["abc12345678", "abc12345"]) == "[REDACTED]"


def test_every_occurrence_is_replaced():
    assert redact_values("a abc12345 b abc12345", ["abc12345"]) == "a [REDACTED] b [REDACTED]"
```

Append to `services/engine/tests/test_compiler_wrapper.py`:

```python
async def test_a_secret_renders_as_a_marker_not_a_value():
    from engine.secrets.markers import marker_for

    deps, recorder, _ = _deps(ScriptedLLM(["답"]))
    deps.secret_nonce = "0123456789abcdef"
    plan = _plan(LLMNode(), {"model": "m", "prompt": "key={{ secret.API_TOKEN }}"},
                 policy=LLMNode.default_policy)

    await _run(plan, deps)

    assert marker_for("API_TOKEN", "0123456789abcdef") in str(recorder.records)


async def test_without_a_nonce_a_secret_reference_fails_the_node():
    """Defence in depth: every node except http_request renders with secret_nonce=None, and the
    validator already refuses `secret` anywhere else."""
    deps, _, _ = _deps(ScriptedLLM(["답"]))
    plan = _plan(LLMNode(), {"model": "m", "prompt": "key={{ secret.API_TOKEN }}"},
                 policy=LLMNode.default_policy)

    with pytest.raises(NodeFailedError) as exc:
        await _run(plan, deps)

    assert exc.value.error.code == ErrorCode.TEMPLATE_ERROR
```

- [x]  **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_secrets_markers.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.secrets.markers'`.

- [x]  **Step 3: Implement the markers**

Create `services/engine/engine/secrets/markers.py`:

```python
"""Secret markers (2b design §4.3).

The renderer binds `secret` to a mapping that answers with a marker instead of a value, so a secret never
enters the render subprocess, the run state, the checkpoint or node_runs. `http_request.execute()` swaps
markers for real values immediately before sending, and scrubs the values back out of what it returns.

The nonce is derived from the run id and ENGINE_SECRET_KEY rather than drawn at random: a replayed render
after a worker restart must produce exactly what the first one produced, or the recorded input and the
checkpoint disagree. It also means tenant text cannot forge a marker.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MARKER = re.compile(r"\[\[secret:([A-Z][A-Z0-9_]{0,63}):([0-9a-f]{16})\]\]")
REDACTED = "[REDACTED]"


def nonce_for(run_id: str, key: bytes) -> str:
    return hmac.new(key, run_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def marker_for(name: str, nonce: str) -> str:
    return f"[[secret:{name}:{nonce}]]"


class SecretMarkers(dict):
    """Bound as `secret` in the render context. The template environment reads mappings by key, so
    `{{ secret.API_TOKEN }}` lands in __getitem__; an invalid name raises KeyError, which the environment
    turns into an undefined value and therefore a template error."""

    def __init__(self, nonce: str) -> None:
        super().__init__()
        self._nonce = nonce

    def __getitem__(self, name: str) -> str:
        if not isinstance(name, str) or not NAME.match(name):
            raise KeyError(name)
        return marker_for(name, self._nonce)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and bool(NAME.match(name))


def find_names(text: str, nonce: str) -> set[str]:
    return {found.group(1) for found in MARKER.finditer(text) if found.group(2) == nonce}


def substitute(text: str, values: dict[str, str], nonce: str) -> str:
    """Markers of this run whose name resolved become the value; anything else is left untouched."""

    def replace(found: re.Match[str]) -> str:
        if found.group(2) != nonce:
            return found.group(0)
        return values.get(found.group(1), found.group(0))

    return MARKER.sub(replace, text)


def redact_values(value: Any, secrets: list[str]) -> Any:
    """Replace every secret value wherever it appears in a JSON value.

    Longest first: a short secret that is a prefix of a longer one would otherwise cut the longer one in
    half and leave the rest of it in the output.
    """
    ordered = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    return _walk(value, ordered) if ordered else value


def _walk(value: Any, ordered: list[str]) -> Any:
    if isinstance(value, str):
        for secret in ordered:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {_walk(key, ordered): _walk(item, ordered) for key, item in value.items()}
    if isinstance(value, list):
        return [_walk(item, ordered) for item in value]
    return value
```

- [x]  **Step 4: Thread the nonce through the render path**

In `services/engine/engine/compiler/wrapper.py`, add `from engine.secrets.markers import SecretMarkers` and replace the two functions:

```python
def render_fields(fields: list[TemplateField], outputs: dict[str, Any],
                  secret_nonce: str | None = None) -> dict[str, Any]:
    """Pure rendering: runs inline or, through RunDeps.render, in the worker's render pool.

    With a nonce the context gains `secret`, which answers with markers rather than values (2b design
    §4.3). Without one there is no `secret` binding at all, so `{{ secret.X }}` fails — every node except
    http_request renders that way.
    """
    context = outputs if secret_nonce is None else {**outputs, "secret": SecretMarkers(secret_nonce)}
    return {f.path: render_template(f.source, context, f.target) for f in fields}


async def _rendered(deps: RunDeps, fields: list[TemplateField], outputs: dict[str, Any]) -> dict[str, Any]:
    if deps.render is None:
        rendered = render_fields(fields, outputs, deps.secret_nonce)
    else:
        try:
            rendered = await deps.render(fields, outputs, deps.secret_nonce)
        except TimeoutError as exc:
            raise NodeError(
                ErrorCode.TEMPLATE_ERROR, "템플릿 렌더링이 제한 시간을 초과했습니다", retryable=False
            ) from exc
    try:
        check_storable(rendered)  # recorded as the attempt's input (jsonb) and passed on to outputs
    except ValueError as exc:
        raise NodeError(ErrorCode.TEMPLATE_ERROR, f"템플릿 결과를 사용할 수 없습니다: {exc}", retryable=False) from exc
    return rendered
```

In `services/engine/engine/runtime/deps.py`:

```python
RenderFn = Callable[[list["TemplateField"], dict[str, Any], str | None], Awaitable[dict[str, Any]]]
```

extend the `TYPE_CHECKING` import to `from engine.nodes.base import HttpClient, SecretResolver, TemplateField`, and add to `RunDeps` after `render`:

```python
    secret_nonce: str | None = None
    """Per-run marker nonce (engine.secrets.markers). None disables the `secret` binding entirely."""
    http: "HttpClient | None" = None
    secrets: "SecretResolver | None" = None
```

In `services/engine/engine/worker/render.py`:

```python
def _job(fields: list[TemplateField], outputs: dict[str, Any],
         secret_nonce: str | None = None) -> dict[str, Any]:
    return render_fields(fields, outputs, secret_nonce)
```

and in `RenderPool.__call__`, take `secret_nonce: str | None = None` as a third parameter and schedule with `args=(fields, outputs, secret_nonce)`.

- [x]  **Step 5: Add the ports and the context fields**

In `services/engine/engine/nodes/base.py`, add `Protocol` to the `typing` import, import the response type
the client already defines — `from engine.http.client import HttpResponse` — and, above `NodeContext`:

```python
class HttpClient(Protocol):
    async def request(self, *, method: str, url: str, headers: dict[str, str], body: str | None,
                      timeout_sec: float) -> HttpResponse: ...


class SecretResolver(Protocol):
    async def resolve(self, names: set[str]) -> dict[str, str]: ...
```

and to `NodeContext`:

```python
    http: HttpClient | None = None
    secrets: SecretResolver | None = None
    secret_nonce: str | None = None
    timeout_sec: float | None = None
```

`HttpResponse` is deliberately not redefined here: one type, defined next to the only thing that builds it
(`engine/http/client.py`, Task 2), and imported by the protocol that describes it. `engine.http.client`
imports nothing from `engine.nodes`, so this direction is the one that cannot cycle.

In `services/engine/engine/compiler/wrapper.py`, `_context(...)` must pass them on. It does not currently see the effective timeout, so give it one more parameter:

```python
def _context(plan, deps, state, outputs, exec_index, attempt, resumed, timeout):
    # everything above the return stays exactly as it is; only the call below grows four arguments
    return NodeContext(
        # ... every argument it already passes, unchanged ...
        http=deps.http,
        secrets=deps.secrets,
        secret_nonce=deps.secret_nonce,
        timeout_sec=timeout,
    )
```

and at the call site inside `node_fn`: `ctx = _context(plan, deps, state, outputs, exec_index, attempt, resumed, timeout)`.

- [x]  **Step 6: Give the worker the nonce and the two ports**

In `services/engine/engine/worker/worker.py`, add `from engine.secrets.markers import nonce_for`, accept `http=None, secrets=None` in `__init__` and store them, add:

```python
    def _nonce(self, run_id: str) -> str | None:
        key = self._config.secret_key
        return None if key is None else nonce_for(run_id, key)
```

and extend the `RunDeps(...)` construction in `_run`:

```python
        deps = RunDeps(run_id=run_id, llm=self._llm, recorder=recorder, guard=guard, render=self._render,
                       secret_nonce=self._nonce(run_id), http=self._http, secrets=self._secrets)
```

- [x]  **Step 7: Run the tests**

Run: `uv run pytest tests/test_secrets_markers.py tests/test_compiler_wrapper.py tests/test_worker_render.py -q`
Expected: PASS.

- [x]  **Step 8: Run everything and commit**

Run: `uv run pytest -q` → all pass; existing `render_fields`/`RenderPool` callers keep working because the new argument has a default.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine services/engine/tests
git commit -m "feat(engine): render secrets as per-run markers" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 7: Allow `{{secret.NAME}}` in http_request only

**2b 설계 §4.4.** Plan 2a refuses `secret` everywhere with `SECRET_NOT_ALLOWED`. Open exactly one door.

**Files:**
- Modify: `services/engine/engine/validator/refs.py`
- Test: `services/engine/tests/test_validator_refs.py` (append)

- [x]  **Step 1: Write the failing tests**

Append to `services/engine/tests/test_validator_refs.py`, reusing whatever helper the file already uses to run `analyze` on a DSL dict (do not invent a new one):

```python
HTTP_DSL = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "http_1", "type": "http_request", "config": {
            "method": "GET", "url": "https://api.example.com/{{ secret.API_TOKEN }}", "headers": {}}},
        {"id": "end", "type": "end", "config": {"outputs": {"status": "{{ http_1.status }}"}}},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "http_1"},
              {"id": "e2", "source": "http_1", "target": "end"}],
}


def test_a_secret_is_allowed_in_an_http_request_url():
    assert "SECRET_NOT_ALLOWED" not in {issue.code for issue in analyze(HTTP_DSL).issues}


def test_a_secret_is_allowed_in_an_http_request_header_and_body():
    dsl = copy.deepcopy(HTTP_DSL)
    dsl["nodes"][1]["config"] = {
        "method": "POST", "url": "https://api.example.com/x",
        "headers": {"Authorization": "Bearer {{ secret.API_TOKEN }}"},
        "body": '{"k": {{ secret.API_TOKEN }}}',
    }

    assert "SECRET_NOT_ALLOWED" not in {issue.code for issue in analyze(dsl).issues}


def test_a_secret_in_an_llm_prompt_is_still_refused():
    dsl = copy.deepcopy(HTTP_DSL)
    dsl["nodes"][1] = {"id": "llm_1", "type": "llm",
                       "config": {"model": "m", "prompt": "{{ secret.API_TOKEN }}"}}
    dsl["nodes"][2]["config"] = {"outputs": {"text": "{{ llm_1.text }}"}}
    dsl["edges"] = [{"id": "e1", "source": "start", "target": "llm_1"},
                    {"id": "e2", "source": "llm_1", "target": "end"}]

    assert "SECRET_NOT_ALLOWED" in {issue.code for issue in analyze(dsl).issues}


def test_a_bare_secret_reference_is_refused_even_in_http_request():
    dsl = copy.deepcopy(HTTP_DSL)
    dsl["nodes"][1]["config"]["url"] = "https://api.example.com/{{ secret }}"

    assert "SECRET_NOT_ALLOWED" in {issue.code for issue in analyze(dsl).issues}
```

Add `import copy` at the top if it is not already there.

- [x]  **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_validator_refs.py -q`
Expected: FAIL. The two `http_request` cases also report an unknown node type until Task 9 registers it — mark exactly those two `@pytest.mark.xfail(reason="http_request lands in Task 9", strict=True)` and remove the marker in Task 9. Do not weaken the assertions.

- [x]  **Step 3: Open the one door**

In `services/engine/engine/validator/refs.py`, replace the `secret` branch inside `_check_ref`:

```python
    if ref.root == "secret":
        node_type = graph.nodes[where["nodeId"]].spec.type
        field_ok = (template_field.path in ("url", "body")
                    or template_field.path.startswith("headers."))
        # Exactly one label below `secret`: a bare `{{ secret }}` would render the marker mapping itself.
        if node_type == "http_request" and field_ok and len(ref.path) == 1:
            return []
        return [error("SECRET_NOT_ALLOWED",
                      "시크릿은 HTTP 요청 노드의 url·headers·body에서만 참조할 수 있습니다", **where)]
```

- [x]  **Step 4: Run the tests**

Run: `uv run pytest tests/test_validator_refs.py -q`
Expected: PASS (with the two xfail markers).

- [x]  **Step 5: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine/validator/refs.py services/engine/tests/test_validator_refs.py
git commit -m "feat(engine): allow secret references in http_request fields only" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 8: Header-name redaction

**2b 설계 §6, 계층 1.** Applied whether or not a secret was used.

**Files:**
- Modify: `services/engine/engine/events/redact.py`
- Test: `services/engine/tests/test_events_redact.py` (append)

- [x]  **Step 1: Write the failing test**

Append to `services/engine/tests/test_events_redact.py`:

```python
def test_sensitive_headers_are_redacted_by_name():
    from engine.events.redact import redact_headers

    headers = {"Authorization": "Bearer abc", "set-cookie": "s=1", "X-API-Key": "k",
               "Content-Type": "application/json"}

    assert redact_headers(headers) == {"authorization": "[REDACTED]", "set-cookie": "[REDACTED]",
                                       "x-api-key": "[REDACTED]", "content-type": "application/json"}


def test_header_redaction_lowercases_names_and_keeps_order():
    from engine.events.redact import redact_headers

    assert list(redact_headers({"COOKIE": "a", "Accept": "b"})) == ["cookie", "accept"]
```

- [x]  **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_events_redact.py -q`
Expected: FAIL — `ImportError: cannot import name 'redact_headers'`.

- [x]  **Step 3: Implement it**

In `services/engine/engine/events/redact.py`, add:

```python
SECRET_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token",
})


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Header values that carry credentials, by name (MVP design 10.3). Applied to every stored request
    and response header set, whether or not the workflow used a secret."""
    return {name.lower(): (REDACTED if name.lower() in SECRET_HEADERS else value)
            for name, value in headers.items()}
```

and update the module docstring: header-name redaction lives here now, and value-based redaction lives at the `http_request` boundary (2b design §6).

- [x]  **Step 4: Run the tests**

Run: `uv run pytest tests/test_events_redact.py -q`
Expected: PASS.

- [x]  **Step 5: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine/events/redact.py services/engine/tests/test_events_redact.py
git commit -m "feat(engine): redact credential headers by name" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 9: The `http_request` node

**2b 설계 §3, §5.6, §6 계층 2.** The node is where a secret briefly exists in plaintext — and the only place — so it is also where value-based redaction happens.

**Files:**
- Create: `services/engine/engine/nodes/http_request.py`
- Modify: `services/engine/engine/errors.py`, `services/engine/engine/nodes/registry.py`, `services/engine/tests/test_validator_refs.py` (remove the xfail markers)
- Test: `services/engine/tests/test_nodes_http_request.py`

- [x]  **Step 1: Add the error codes**

In `services/engine/engine/errors.py`, add to `ErrorCode` (MVP design 8.2 already names the first three):

```python
    HTTP_BLOCKED = "HTTP_BLOCKED"
    HTTP_ERROR = "HTTP_ERROR"
    HTTP_RESPONSE_TOO_LARGE = "HTTP_RESPONSE_TOO_LARGE"
    HTTP_UNSUPPORTED_MEDIA_TYPE = "HTTP_UNSUPPORTED_MEDIA_TYPE"
    SECRET_NOT_FOUND = "SECRET_NOT_FOUND"
```

- [x]  **Step 2: Write the failing tests**

Create `services/engine/tests/test_nodes_http_request.py`:

```python
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


@pytest.mark.parametrize(("status", "retryable"), [(404, False), (401, False), (429, True), (500, True),
                                                   (503, True)])
async def test_a_non_2xx_response_is_a_node_error(status, retryable):
    http = FakeHttp(HttpResponse(status, {"content-type": "text/plain"}, "nope"))

    with pytest.raises(NodeError) as exc:
        await _run(_config(), {"url": "https://api.example.com/x"}, http=http)

    assert exc.value.code == ErrorCode.HTTP_ERROR and exc.value.retryable is retryable
    assert str(status) in exc.value.message


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


async def test_without_the_flag_no_idempotency_header_is_sent():
    http = FakeHttp()

    await _run(_config(method="POST"), {"url": "https://api.example.com/x"}, http=http)

    assert "Idempotency-Key" not in http.calls[0]["headers"]


async def test_a_missing_port_is_an_engine_fault_not_a_node_error():
    """No client wired up is a deployment mistake, not something a tenant did."""
    with pytest.raises(EngineFault):
        await _run(_config(), {"url": "https://api.example.com/x"}, http=None)


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
```

- [x]  **Step 3: Run them to see them fail**

Run: `uv run pytest tests/test_nodes_http_request.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.nodes.http_request'`.

- [x]  **Step 4: Implement the node**

Create `services/engine/engine/nodes/http_request.py`:

```python
"""The http_request node (MVP design 4.7, 2b design §3).

This is the only place a secret value exists in plaintext, so it is also where value-based redaction
happens (2b design §6): markers become values immediately before sending, and the values are scrubbed out
of everything the node returns — including the message of any error it raises. Nothing downstream, and
nothing stored, ever sees them.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.dsl.models import Policy, RetrySpec
from engine.errors import EngineFault, ErrorCode, NodeError
from engine.events.redact import redact_headers
from engine.http.client import EgressBlocked, ResponseTooLarge, TransportFailed, UnsupportedMedia
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField, Usage
from engine.secrets.markers import find_names, redact_values, substitute

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
IDEMPOTENT = {"GET", "PUT", "DELETE", "HEAD"}
HEADER_NAME = r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$"
MAX_HEADERS = 20
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "integer"},
        "headers": {"type": "object"},
        "body": {},
    },
    "required": ["status", "headers", "body"],
}


class HttpRequestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal[METHODS] = "GET"  # type: ignore[valid-type]
    url: str = Field(min_length=1, max_length=4096, json_schema_extra=TEMPLATE)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = Field(None, json_schema_extra=TEMPLATE)
    bodyFormat: Literal["json", "text"] = "json"
    sendIdempotencyKey: bool = False

    @field_validator("headers")
    @classmethod
    def _header_names(cls, value: dict[str, str]) -> dict[str, str]:
        import re

        if len(value) > MAX_HEADERS:
            raise ValueError(f"헤더는 최대 {MAX_HEADERS}개입니다")
        for name in value:
            if not re.match(HEADER_NAME, name):
                raise ValueError(f"헤더 이름 형식이 올바르지 않습니다: {name}")
        # Host is set from the URL by the client (2b design §5.4); letting a template set it would be a
        # way to point the request at one host while presenting another.
        if any(name.lower() in ("host", "content-length") for name in value):
            raise ValueError("Host와 Content-Length 헤더는 지정할 수 없습니다")
        return value


class HttpRequestNode(NodeSpec):
    type = "http_request"
    label = "HTTP 요청"
    category = "action"
    Config = HttpRequestConfig
    default_policy = Policy(timeoutSec=30, retry=RetrySpec(maxAttempts=3))
    side_effects = True

    def policy_for(self, config: HttpRequestConfig) -> Policy:
        """POST and PATCH are not idempotent, so a retry can double a payment (MVP design 5.4)."""
        if config.method in IDEMPOTENT:
            return self.default_policy
        return Policy(timeoutSec=30, retry=RetrySpec(maxAttempts=1))

    def template_fields(self, config: HttpRequestConfig) -> list[TemplateField]:
        fields = [TemplateField("url", config.url, "string")]
        fields += [TemplateField(f"headers.{name}", value, "string")
                   for name, value in config.headers.items()]
        if config.body is not None:
            fields.append(TemplateField("body", config.body,
                                        "json" if config.bodyFormat == "json" else "string"))
        return fields

    def output_schema(self, config: HttpRequestConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return OUTPUT_SCHEMA

    async def execute(self, ctx: NodeContext, config: HttpRequestConfig,
                      rendered: dict[str, Any]) -> NodeResult:
        if ctx.http is None:
            raise EngineFault("http_request needs an HTTP client")
        url = rendered["url"]
        headers = {name: rendered[f"headers.{name}"] for name in config.headers}
        body = _body(config, rendered)
        values = await self._secrets(ctx, url, headers, body)
        if values:
            nonce = ctx.secret_nonce or ""
            url = substitute(url, values, nonce)
            headers = {name: substitute(value, values, nonce) for name, value in headers.items()}
            body = substitute(body, values, nonce) if body is not None else None
        used = list(values.values())
        if config.sendIdempotencyKey:
            headers["Idempotency-Key"] = _idempotency_key(ctx)
        response = await self._send(ctx, config, url, headers, body, used)
        output = redact_values(
            {"status": response.status, "headers": redact_headers(response.headers),
             "body": response.body},
            used,
        )
        if not 200 <= response.status < 300:
            raise NodeError(ErrorCode.HTTP_ERROR, f"HTTP {response.status} 응답을 받았습니다",
                            retryable=_retryable(response.status))
        return NodeResult(output, Usage())

    async def _secrets(self, ctx: NodeContext, url: str, headers: dict[str, str],
                       body: str | None) -> dict[str, str]:
        nonce = ctx.secret_nonce
        if not nonce:
            return {}
        names: set[str] = set()
        for text in (url, body, *headers.values()):
            if text is not None:
                names |= find_names(text, nonce)
        if not names:
            return {}
        if ctx.secrets is None:
            raise EngineFault("http_request needs a secret resolver")
        values = await ctx.secrets.resolve(names)
        missing = sorted(names - set(values))
        if missing:
            raise NodeError(ErrorCode.SECRET_NOT_FOUND,
                            f"시크릿을 찾을 수 없습니다: {', '.join(missing)}", retryable=False)
        return values

    async def _send(self, ctx: NodeContext, config: HttpRequestConfig, url: str,
                    headers: dict[str, str], body: str | None, used: list[str]):
        try:
            return await ctx.http.request(method=config.method, url=url, headers=headers, body=body,
                                          timeout_sec=ctx.timeout_sec or 30)
        except EgressBlocked as exc:
            # Only the category: the host and address are what an attacker is probing for.
            raise NodeError(ErrorCode.HTTP_BLOCKED, f"차단된 요청입니다 ({exc.category})",
                            retryable=False) from None
        except ResponseTooLarge as exc:
            raise NodeError(ErrorCode.HTTP_RESPONSE_TOO_LARGE,
                            f"응답이 너무 큽니다 (최대 {exc} bytes)", retryable=False) from None
        except UnsupportedMedia as exc:
            raise NodeError(ErrorCode.HTTP_UNSUPPORTED_MEDIA_TYPE,
                            f"처리할 수 없는 응답 형식입니다: {redact_values(str(exc), used)}",
                            retryable=False) from None
        except TransportFailed as exc:
            # httpx puts the whole URL in its message, query string and all.
            raise NodeError(ErrorCode.HTTP_ERROR,
                            f"요청에 실패했습니다: {redact_values(str(exc), used)}",
                            retryable=True) from None


def _body(config: HttpRequestConfig, rendered: dict[str, Any]) -> str | None:
    if config.body is None:
        return None
    value = rendered["body"]
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _idempotency_key(ctx: NodeContext) -> str:
    """Stable for one execution point, across every attempt of it — that is what lets the far side
    deduplicate a retry (MVP design 5.4). `attempt` is deliberately not part of it."""
    seed = f"{ctx.run_id}:{ctx.node_id}:{ctx.exec_index}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:32]


def _retryable(status: int) -> bool:
    return status == 429 or 500 <= status < 600
```

- [x]  **Step 5: Register the node and honour `policy_for`**

In `services/engine/engine/nodes/registry.py`, import `HttpRequestNode` and add `HttpRequestNode(),` to `default_registry()`.

`policy_for` is new on `NodeSpec`. Add the default to `services/engine/engine/nodes/base.py`:

```python
    def policy_for(self, config: BaseModel) -> Policy | None:
        """Effective default policy for this config. Only http_request varies it (by method)."""
        return self.default_policy
```

and find where the validator resolves a node's effective policy (`engine/validator/structure.py` builds `ParsedNode.policy` from `spec.default_policy`); change that one site to call `spec.policy_for(config)` instead. Search with:

Run: `grep -rn "default_policy" services/engine/engine`
and update every place that reads `spec.default_policy` to decide a node's *effective* policy. The `/node-types` route keeps using `default_policy` — it describes the palette, not one configured node.

- [x]  **Step 6: Un-xfail the validator tests**

Remove the two `@pytest.mark.xfail` markers added in Task 7.

- [x]  **Step 7: Run the tests**

Run: `uv run pytest tests/test_nodes_http_request.py tests/test_validator_refs.py -q`
Expected: PASS.

Run: `uv run pytest tests/test_api_basics.py -q`
Expected: PASS — `/node-types` now returns nine types. Update the expected set in `test_node_types_describe_the_registry` to include `http_request`.

- [x]  **Step 8: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine services/engine/tests
git commit -m "feat(engine): add the http_request node with secret substitution" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 10: Encrypt `runs.inputs` and `runs.outputs`

**2b 설계 §2, §9.** `runs.inputs` is the run's initial state, so it cannot be redacted at write. Encrypt at rest instead, and keep redacting on the read path.

**Files:**
- Create: `services/engine/engine/db/migrations/versions/0003_encrypt_run_payloads.py`, `services/engine/engine/db/crypto.py`
- Modify: `services/engine/engine/db/runs.py`, `services/engine/engine/api/routers/runs.py`, `services/engine/engine/worker/worker.py`
- Test: `services/engine/tests/test_db_crypto.py`, `services/engine/tests/test_api_runs.py` (append)

- [ ]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_db_crypto.py`:

```python
import pytest

from engine.db import runs as run_db
from tests.factories import WORKSPACE

SECRETISH = "hunter2-in-the-inputs"


async def test_run_inputs_are_not_stored_in_the_clear(pool, db_url):
    import dataclasses
    import uuid

    from engine.config import load_config
    from engine.db import workflows as workflow_db

    config = dataclasses.replace(load_config(), database_url=db_url)
    dsl = {"version": "1", "nodes": [], "edges": []}
    async with pool.connection() as conn:
        workflow = await workflow_db.create(conn, name="w")
        version = await workflow_db.pin_version(conn, workflow_id=str(workflow["id"]), dsl=dsl,
                                                dsl_hash="h")
        run = await run_db.insert_queued(
            conn, workflow_id=str(workflow["id"]), version_id=str(version["id"]),
            workspace_id=WORKSPACE, inputs={"note": SECRETISH}, idempotency_key=None,
            store_run_data=True, key=config.secret_key)
        raw = await (await conn.execute("SELECT inputs FROM runs WHERE id=%s", (run["id"],))).fetchone()

    assert raw["inputs"] is not None and SECRETISH.encode() not in bytes(raw["inputs"])
    assert run["inputs"] == {"note": SECRETISH}  # the caller still gets the value back


async def test_a_stored_payload_round_trips(pool, db_url):
    """A worker builds the run's initial state from this column: if the decode is not exact the run cannot
    start at all. Round-tripping the awkward values is the cheap version of that check; Task 17 proves it
    through a real worker."""
    import dataclasses

    from engine.config import load_config
    from engine.db.crypto import open_payload, seal_payload

    key = dataclasses.replace(load_config(), database_url=db_url).secret_key
    value = {"한글": "값", "nested": [{"n": 1.5}, None, True], "empty": {}}

    assert open_payload(key, "inputs", seal_payload(key, "inputs", value)) == value
    assert open_payload(key, "inputs", seal_payload(key, "inputs", None)) is None


async def test_a_payload_cannot_be_moved_between_the_two_columns(pool, db_url):
    """The column name is the AAD, so `outputs` ciphertext pasted into `inputs` reads as unreadable rather
    than as a value the worker would then start a run from."""
    import dataclasses

    from engine.config import load_config
    from engine.db.crypto import open_payload, seal_payload

    key = dataclasses.replace(load_config(), database_url=db_url).secret_key
    if key is None:
        pytest.skip("no key configured: the dev path stores plain JSON on purpose")

    sealed = seal_payload(key, "outputs", {"result": "값"})

    assert open_payload(key, "inputs", sealed) is None
```

Append to `services/engine/tests/test_api_runs.py`:

```python
async def test_inputs_and_outputs_come_back_decrypted_and_redacted(api, pool, worker_factory):
    from engine.llm.scripted import ScriptedLLM
    from tests.conftest import until

    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(["요약본"]))
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI", "password": "hunter2-secret"},
                                    "revision": 2})).json()

    async def done():
        run = (await api.get(f"/runs/{created['runId']}")).json()
        return run if run["status"] == "succeeded" else None

    run = await until(done)

    assert run["inputs"]["topic"] == "AI"
    assert run["inputs"]["password"] == "[REDACTED]"  # key-name redaction on the read path
    assert run["outputs"] == {"result": "요약본"}
```

- [ ]  **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_db_crypto.py -q`
Expected: FAIL — `insert_queued()` got an unexpected keyword argument `key`.

- [ ]  **Step 3: Write the migration**

Create `services/engine/engine/db/migrations/versions/0003_encrypt_run_payloads.py`:

```python
"""Encrypt the run payload columns (2b design §2, §9).

Revision ID: 0003
Revises: 0002

runs.inputs is the run's initial state, so it cannot be redacted at write time the way node_runs is —
the worker has to read the real value back. Encrypting at rest keeps a database dump from handing over
every run's input, and the read path still redacts before anything reaches a client.

PR #1 and PR #2 are unmerged and nothing is deployed, so there is no data to carry across: the columns
are dropped and recreated rather than converted.
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE runs DROP COLUMN inputs;
ALTER TABLE runs DROP COLUMN outputs;
ALTER TABLE runs ADD COLUMN inputs bytea;
ALTER TABLE runs ADD COLUMN outputs bytea;
"""

DOWN = """
ALTER TABLE runs DROP COLUMN inputs;
ALTER TABLE runs DROP COLUMN outputs;
ALTER TABLE runs ADD COLUMN inputs jsonb;
ALTER TABLE runs ADD COLUMN outputs jsonb;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
```

- [ ]  **Step 4: Add the payload codec**

Create `services/engine/engine/db/crypto.py`:

```python
"""The two encrypted run payload columns (2b design §9).

Same AES-GCM construction as engine/secrets/crypto.py, with the column name as AAD so an `inputs` value
cannot be moved into `outputs`. With no key configured (ENGINE_DEV_INSECURE=1) the value is stored as
plain UTF-8 JSON, which is what a development database already was.
"""
from __future__ import annotations

import json
from typing import Any

from engine.secrets.crypto import SecretCryptoError, open_secret, seal_secret


def seal_payload(key: bytes | None, column: str, value: Any) -> bytes | None:
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if key is None:
        return text.encode("utf-8")
    return seal_secret(key, column, text)


def open_payload(key: bytes | None, column: str, stored: bytes | memoryview | None) -> Any:
    if stored is None:
        return None
    raw = bytes(stored)
    if key is None:
        return json.loads(raw.decode("utf-8"))
    try:
        return json.loads(open_secret(key, column, raw))
    except (SecretCryptoError, ValueError):
        # A row written under a different key is unreadable, not a reason to fail the request: the run's
        # metadata still describes what happened.
        return None
```

- [ ]  **Step 5: Use it in the queries**

In `services/engine/engine/db/runs.py`:

- `insert_queued` takes `key: bytes | None` and writes `seal_payload(key, "inputs", inputs)` instead of `Jsonb(inputs)`, then returns the row with `row["inputs"]` replaced by the original `inputs` (the caller expects the value, not the ciphertext).
- `finish` takes `key: bytes | None` and writes `seal_payload(key, "outputs", outputs)`.
- Add a helper used by every reader:

```python
def decode_run(row: dict[str, Any] | None, key: bytes | None) -> dict[str, Any] | None:
    """Return the row with its two encrypted columns decoded in place."""
    if row is None:
        return None
    decoded = dict(row)
    decoded["inputs"] = open_payload(key, "inputs", row.get("inputs"))
    decoded["outputs"] = open_payload(key, "outputs", row.get("outputs"))
    return decoded
```

- `get_run`, `claim_next` and `lock_run` keep returning raw rows; every *caller* decodes. Update:
  - `engine/worker/worker.py`: after `claim_next`, `row = run_db.decode_run(row, self._config.secret_key)` so `row["inputs"]` is the real dict the graph starts from.
  - `engine/api/routers/runs.py`: `_run_view` decodes with `request.app.state.config.secret_key` and then applies the existing `redact` + `_sanitize` to `inputs` and `outputs`.
  - `engine/worker/reaper.py` does not read either column — check with `grep -n "inputs\|outputs" services/engine/engine/worker/reaper.py` and leave it alone if it only nulls them.

Run: `grep -rn "\"inputs\"\]\|\[.outputs.\]" services/engine/engine` and make sure every read goes through `decode_run` or `open_payload`.

- [ ]  **Step 6: Run the tests**

Run: `uv run pytest tests/test_db_crypto.py tests/test_api_runs.py tests/test_worker_run.py -q`
Expected: PASS.

- [ ]  **Step 7: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine services/engine/tests
git commit -m "feat(engine): encrypt run inputs and outputs at rest" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 11: Retention purge and orphan checkpoints

**2b 설계 §7.** Small batches inside the reaper's existing sweep — no daily cron, no "last run" bookkeeping.

**Files:**
- Create: `services/engine/engine/db/purge.py`
- Modify: `services/engine/engine/config.py`, `services/engine/engine/worker/reaper.py`
- Test: `services/engine/tests/test_db_purge.py`

- [ ]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_db_purge.py`:

```python
import dataclasses

from engine.config import load_config
from engine.db import purge as purge_db
from engine.db import runs as run_db
from engine.worker.reaper import Reaper
from tests.factories import make_run


async def _age(pool, run_id: str, days: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET status='succeeded', finished_at = now() - make_interval(days => %s)"
            " WHERE id=%s", (days, run_id))


async def _row(pool, run_id: str):
    async with pool.connection() as conn:
        return await run_db.get_run(conn, run_id)


async def _checkpoints(pool, run_id: str) -> int:
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT count(*) AS n FROM checkpoints WHERE thread_id=%s", (run_id,))).fetchone()
    return row["n"]


def _config(**overrides):
    return dataclasses.replace(load_config(), retention_days=30, purge_batch=100, **overrides)


async def test_a_fresh_run_keeps_its_payloads(pool, redis):
    run_id = await make_run(pool, status="succeeded", inputs={"topic": "AI"})

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert row["purged_at"] is None


async def test_an_expired_run_keeps_metadata_and_loses_payloads(pool, redis):
    run_id = await make_run(pool, status="running", inputs={"topic": "AI"})
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status, input, output)"
            " VALUES (gen_random_uuid(), %s, 'llm_1', 1, 1, 'succeeded', '{\"a\": 1}', '{\"b\": 2}')",
            (run_id,))
        await conn.execute(
            "INSERT INTO run_events (run_id, seq, type, payload) VALUES (%s, 1, 'run_started', '{\"x\": 1}')",
            (run_id,))
        await conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata)"
            " VALUES (%s, '', '1', 't', '{}', '{}')", (run_id,))
    await _age(pool, run_id, 31)

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert (row["status"], row["inputs"], row["outputs"]) == ("succeeded", None, None)
    assert row["purged_at"] is not None and row["finished_at"] is not None
    async with pool.connection() as conn:
        node = await (await conn.execute(
            "SELECT status, input, output FROM node_runs WHERE run_id=%s", (run_id,))).fetchone()
        event = await (await conn.execute(
            "SELECT type, payload FROM run_events WHERE run_id=%s", (run_id,))).fetchone()
    assert (node["status"], node["input"], node["output"]) == ("succeeded", None, None)
    assert (event["type"], event["payload"]) == ("run_started", {})
    assert await _checkpoints(pool, run_id) == 0


async def test_a_purged_run_is_not_scanned_again(pool, redis):
    run_id = await make_run(pool, status="running")
    await _age(pool, run_id, 31)
    reaper = Reaper(_config(), pool, redis)
    await reaper.sweep()

    async with pool.connection() as conn:
        rows = await purge_db.expired_runs(conn, retention_days=30, limit=100)

    assert [str(row["id"]) for row in rows] == []


async def test_an_unfinished_run_is_never_purged(pool, redis):
    run_id = await make_run(pool, status="running", inputs={"topic": "AI"})
    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET created_at = now() - interval '90 days' WHERE id=%s", (run_id,))

    await Reaper(_config(), pool, redis).sweep()

    assert (await _row(pool, run_id))["purged_at"] is None


async def test_an_orphan_checkpoint_is_collected(pool, redis):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata,"
            " created_at) VALUES (gen_random_uuid()::text, '', '1', 't', '{}', '{}', now() - interval '2 hours')")

    await Reaper(_config(), pool, redis).sweep()

    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT count(*) AS n FROM checkpoints")).fetchone()
    assert row["n"] == 0


async def test_a_young_orphan_is_left_alone(pool, redis):
    """Safety margin: a checkpoint written seconds ago is not evidence that its run is gone."""
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata)"
            " VALUES (gen_random_uuid()::text, '', '1', 't', '{}', '{}')")

    await Reaper(_config(), pool, redis).sweep()

    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT count(*) AS n FROM checkpoints")).fetchone()
    assert row["n"] == 1


async def test_a_live_runs_checkpoint_is_never_collected(pool, redis):
    run_id = await make_run(pool, status="running")
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata,"
            " created_at) VALUES (%s, '', '1', 't', '{}', '{}', now() - interval '2 hours')", (run_id,))

    await Reaper(_config(), pool, redis).sweep()

    assert await _checkpoints(pool, run_id) == 1
```

Before writing these, check the real column list of `checkpoints` — LangGraph owns that table:

Run: `uv run python -c "print(open('engine/db/migrate.py').read())"` and, with a database up, inspect it:

```sql
SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name='checkpoints';
```

Adjust the INSERTs above to the real column set and NOT NULL constraints before running the tests.

- [ ]  **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_db_purge.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.db.purge'`.

- [ ]  **Step 3: Implement the queries**

Create `services/engine/engine/db/purge.py`:

```python
"""Retention cleanup (2b design §7).

Metadata is kept forever; payloads and checkpoints are dropped once a run has been finished for longer
than the retention window. `runs.purged_at` marks a run as done so the sweep never rescans it — without
it the same rows would come back on every tick for the life of the deployment.
"""
from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

ORPHAN_MIN_AGE_HOURS = 1


async def expired_runs(conn: AsyncConnection, *, retention_days: int, limit: int) -> list[dict[str, Any]]:
    return await (await conn.execute(
        "SELECT id FROM runs"
        " WHERE purged_at IS NULL AND finished_at IS NOT NULL"
        "   AND finished_at < now() - make_interval(days => %s)"
        " ORDER BY finished_at LIMIT %s",
        (retention_days, limit),
    )).fetchall()


async def purge_run(conn: AsyncConnection, run_id: str) -> None:
    """Drop everything a tenant put in, keep everything an operator needs."""
    await conn.execute("UPDATE node_runs SET input=NULL, output=NULL WHERE run_id=%s", (run_id,))
    await conn.execute("UPDATE run_events SET payload='{}'::jsonb WHERE run_id=%s", (run_id,))
    await conn.execute(
        "DELETE FROM checkpoint_writes WHERE thread_id=%s;"
        " DELETE FROM checkpoint_blobs WHERE thread_id=%s;"
        " DELETE FROM checkpoints WHERE thread_id=%s",
        (run_id, run_id, run_id),
    )
    await conn.execute(
        "UPDATE runs SET inputs=NULL, outputs=NULL, resume_payload=NULL, purged_at=now(), updated_at=now()"
        " WHERE id=%s", (run_id,))


async def collect_orphan_checkpoints(conn: AsyncConnection, *, limit: int) -> int:
    """Checkpoints whose run row is gone — deleting a workflow cascades its runs away but not these.

    Only rows older than ORPHAN_MIN_AGE_HOURS are considered. A checkpoint is always written after its
    run row exists, so there is no race to lose; the age bound is a second belt for any future path that
    writes one another way.
    """
    cursor = await conn.execute(
        "WITH orphan AS ("
        "  SELECT c.thread_id FROM checkpoints c"
        "   LEFT JOIN runs r ON r.id::text = c.thread_id"
        "   WHERE r.id IS NULL AND c.created_at < now() - make_interval(hours => %s)"
        "   GROUP BY c.thread_id LIMIT %s)"
        " DELETE FROM checkpoints WHERE thread_id IN (SELECT thread_id FROM orphan)",
        (ORPHAN_MIN_AGE_HOURS, limit),
    )
    deleted = cursor.rowcount
    await conn.execute(
        "DELETE FROM checkpoint_writes w WHERE NOT EXISTS ("
        "  SELECT 1 FROM runs r WHERE r.id::text = w.thread_id)")
    await conn.execute(
        "DELETE FROM checkpoint_blobs b WHERE NOT EXISTS ("
        "  SELECT 1 FROM runs r WHERE r.id::text = b.thread_id)")
    return deleted
```

If `checkpoints` has no `created_at` column, use its own timestamp column or fall back to the run's absence plus a `checkpoint_id` age — check the real schema in Step 1 and adjust both the query and the two tests that depend on the age bound.

- [ ]  **Step 4: Add the settings**

In `services/engine/engine/config.py`, add to `EngineConfig`:

```python
    retention_days: int
    purge_batch: int
```

and to `load_config()`:

```python
        retention_days=_int("RUN_DATA_RETENTION_DAYS", 30),
        purge_batch=_int("RUN_PURGE_BATCH", 100),
```

- [ ]  **Step 5: Run it from the reaper**

In `services/engine/engine/worker/reaper.py`, extend `sweep`:

```python
    async def sweep(self) -> None:
        async with self.exclusive() as acquired:
            if not acquired:
                log.debug("reaper advisory lock held by another reaper; skipping this sweep")
                return
            await self._recover_expired()
            await self._expire_waiting()
            await self._purge()
```

and add:

```python
    async def _purge(self) -> None:
        """Retention, one small batch per sweep (2b design §7): cheap once caught up, and a worker that
        restarts hourly never skips a day the way a daily cron would."""
        async with self._pool.connection() as conn:
            rows = await purge_db.expired_runs(conn, retention_days=self._config.retention_days,
                                               limit=self._config.purge_batch)
        for row in rows:
            run_id = str(row["id"])
            try:
                async with self._pool.connection() as conn, conn.transaction():
                    await purge_db.purge_run(conn, run_id)
            except Exception:
                log.warning("purging run %s failed; the next sweep will retry", run_id, exc_info=True)
        try:
            async with self._pool.connection() as conn, conn.transaction():
                collected = await purge_db.collect_orphan_checkpoints(conn, limit=self._config.purge_batch)
            if collected:
                log.info("collected %s orphan checkpoint rows", collected)
        except Exception:
            log.warning("collecting orphan checkpoints failed", exc_info=True)
```

with `from engine.db import purge as purge_db` at the top.

- [ ]  **Step 6: Run the tests**

Run: `uv run pytest tests/test_db_purge.py tests/test_worker_reaper.py -q`
Expected: PASS.

- [ ]  **Step 7: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine services/engine/tests/test_db_purge.py
git commit -m "feat(engine): purge expired run data and orphan checkpoints" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 12: Hardening — the heartbeat gets its own connection

**2b 설계 §8.1.** Measured on the 2a branch: 10 runs × a 10-way fan-out used 13 of 14 pool connections with 7 tasks waiting, and psycopg's default 30 s acquisition timeout equals `WORKER_LEASE_SEC`, so a heartbeat can wait out its own lease and never learn it lost it.

**Files:**
- Modify: `services/engine/engine/worker/worker.py`, `services/engine/engine/db/pool.py`
- Test: `services/engine/tests/test_worker_lease.py` (append)

- [ ]  **Step 1: Write the failing tests**

Append to `services/engine/tests/test_worker_lease.py`:

```python
async def test_the_heartbeat_does_not_use_the_shared_pool(pool, redis, worker_factory):
    """Pool starvation must not be able to cost a healthy run its lease."""
    from engine.llm.scripted import ScriptedLLM

    worker = await worker_factory(ScriptedLLM(["요약본"]))

    assert worker._beat_conn is not None
    assert worker._beat_conn is not pool


async def test_the_lease_survives_a_pool_with_no_free_connections(pool, redis, worker_factory, db_url):
    """§11.2's first hardening invariant, and the measurement that motivated this task: 10 runs x a 10-way
    fan-out used 13 of 14 connections with 7 waiting. A heartbeat queued behind that can wait out the very
    lease it is trying to extend, and the reaper then hands a perfectly healthy run to a second worker."""
    import asyncio
    import contextlib

    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    from tests.factories import make_run

    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM(6.0)
    await worker_factory(llm, lease_sec=2, heartbeat_sec=0.2)
    await llm.started.wait()

    async with contextlib.AsyncExitStack() as stack:
        for _ in range(pool.max_size):  # nothing is left for anyone else
            await stack.enter_async_context(pool.connection())
        await asyncio.sleep(3)  # longer than lease_sec: an unextended lease is gone by now
        # A connection of its own, because the pool has nothing left to give -- the same reason the
        # heartbeat now has one.
        probe = await AsyncConnection.connect(db_url, autocommit=True, row_factory=dict_row)
        try:
            row = await (await probe.execute(
                "SELECT status, lease_expires_at > now() AS alive FROM runs WHERE id=%s",
                (run_id,))).fetchone()
        finally:
            await probe.close()

    assert row["status"] == "running"
    assert row["alive"], "the lease expired while the pool was full"


async def test_a_heartbeat_that_cannot_reach_the_database_gives_up_the_run(pool, redis, worker_factory,
                                                                          monkeypatch):
    """After lease_sec of failures the worker must stop, or two workers end up on one checkpoint."""
    import asyncio

    from engine.db import runs as run_db
    from tests.factories import make_run

    async def always_fails(*args, **kwargs):
        raise RuntimeError("database is unreachable")

    monkeypatch.setattr(run_db, "heartbeat", always_fails)
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM(30.0)
    await worker_factory(llm, lease_sec=1, heartbeat_sec=0.2)

    async with asyncio.timeout(20):
        await llm.started.wait()

        async def stopped():
            return llm.cancelled.is_set()

        await until(stopped)

    async with pool.connection() as conn:
        row = await run_db.get_run(conn, run_id)
    assert row["status"] == "running"  # left for recovery, not written by a worker that lost its lease
```

`_SlowLLM` already exists in this file; give it a `cancelled` event set in a `finally` if it does not have one:

```python
class _SlowLLM:
    def __init__(self, seconds: float = 30.0) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._seconds = seconds

    async def chat(self, **kwargs):
        self.started.set()
        try:
            await asyncio.sleep(self._seconds)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("should have been cancelled")
```

- [ ]  **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_worker_lease.py -q`
Expected: FAIL — `AttributeError: 'Worker' object has no attribute '_beat_conn'`; the lease dies while the
pool is held; and the last test times out because a failing heartbeat currently just keeps retrying.

- [ ]  **Step 3: Give the pool a shorter acquisition timeout**

In `services/engine/engine/db/pool.py`, pass `timeout=5.0` to `AsyncConnectionPool` (and document it):

```python
    # Shorter than WORKER_LEASE_SEC on purpose: a checkout that waits out the lease is worse than one
    # that fails and says so (2b design §8.1).
    timeout=5.0,
```

- [ ]  **Step 4: Implement the dedicated connection**

In `services/engine/engine/worker/worker.py`:

```python
    async def start(self) -> None:
        self._running = True
        await self._reconnect_listen()
        self._beat_conn = await AsyncConnection.connect(self._config.database_url, autocommit=True,
                                                        row_factory=dict_row)
        self._spawn(self._claim_loop())
        self._spawn(self._control_loop())
        await self._reaper.start()
```

with `self._beat_conn: AsyncConnection | None = None` and `self._beat_lock = asyncio.Lock()` in `__init__`, `from psycopg.rows import dict_row` imported, and `stop()` closing it next to `self._listen`.

Replace the heartbeat's pooled connection with the dedicated one, and add the give-up rule:

```python
    async def _heartbeat(self, run_id: str, guard: FlagGuard, task: asyncio.Task) -> None:
        interval = self._config.heartbeat_sec
        last = time.monotonic()
        last_ok = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            try:
                async with self._beat_lock:
                    conn = await self._beat_connection()
                    beat = await run_db.heartbeat(conn, run_id=run_id, owner=self.owner,
                                                  lease_sec=self._config.lease_sec,
                                                  delta_ms=int((now - last) * 1000))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("heartbeat failed for run %s; retrying next interval", run_id, exc_info=True)
                if now - last_ok > self._config.lease_sec:
                    # The lease has certainly expired by now and the reaper may already have handed this
                    # run to someone else. Stop rather than keep executing against a checkpoint we no
                    # longer own (2b design §8.1).
                    log.error("no heartbeat for run %s in %.0fs; giving up the run", run_id, now - last_ok)
                    guard.lose_lease()
                    task.cancel()
                    return
                continue
            last = now
            last_ok = now
            if beat is None:
                guard.lose_lease()
                task.cancel()
                return
            if beat["cancel_requested_at"] is not None:
                self._cancelled.add(run_id)
                guard.cancel()
                task.cancel()
                return
            if beat["active_ms"] > self._config.run_max_active_ms:
                self._timed_out.add(run_id)
                task.cancel()
                return
```

and:

```python
    async def _beat_connection(self) -> AsyncConnection:
        """The heartbeat never queues behind the pool: losing the lease to a full pool is the one failure
        this connection exists to prevent. Reconnects in place if the connection broke."""
        if self._beat_conn is None or self._beat_conn.closed:
            self._beat_conn = await AsyncConnection.connect(self._config.database_url, autocommit=True,
                                                            row_factory=dict_row)
        return self._beat_conn
```

- [ ]  **Step 5: Run the tests**

Run: `uv run pytest tests/test_worker_lease.py tests/test_worker_run.py tests/test_worker_reaper.py -q`
Expected: PASS, run twice.

- [ ]  **Step 6: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine/worker/worker.py services/engine/engine/db/pool.py services/engine/tests/test_worker_lease.py
git commit -m "fix(engine): keep the heartbeat off the shared pool" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 13: Hardening — SSE degrades instead of dying

**2b 설계 §8.2, §8.3.**

**Files:**
- Modify: `services/engine/engine/events/stream.py`
- Test: `services/engine/tests/test_api_events.py` (append)

- [ ]  **Step 1: Write the failing tests**

Append to `services/engine/tests/test_api_events.py`:

```python
async def test_the_stream_keeps_delivering_from_postgres_when_redis_is_gone(pool, redis):
    """Every event is durably in Postgres; a Redis outage must degrade the stream, not end it."""
    import asyncio

    from engine.events.stream import event_stream
    from engine.events.writer import append_event
    from tests.factories import make_run

    class DeadRedis:
        def pubsub(self):
            raise ConnectionError("redis is down")

    run_id = await make_run(pool, status="running")
    stream = event_stream(pool, DeadRedis(), run_id, ping_sec=0.1)
    collected: list[str] = []

    async def pump():
        async for chunk in stream:
            collected.append(chunk)

    task = asyncio.create_task(pump())
    try:
        async with pool.connection() as conn, conn.transaction():
            await append_event(conn.cursor(), run_id, "node_started", node_id="n1", exec_index=1, attempt=1)

        async def seen():
            return any("node_started" in chunk for chunk in collected)

        await until(seen, timeout=10)

        async with pool.connection() as conn, conn.transaction():
            await append_event(conn.cursor(), run_id, "run_succeeded")

        async with asyncio.timeout(10):
            await task
    finally:
        task.cancel()

    assert any("run_succeeded" in chunk for chunk in collected)


async def test_a_retried_run_keeps_one_stream_open(pool, redis):
    """Task 15 appends run_queued after run_failed; the stream must not stop at the stale terminal event."""
    import asyncio

    from engine.db import runs as run_db
    from engine.events.stream import event_stream
    from engine.events.writer import append_event
    from tests.factories import make_run

    run_id = await make_run(pool, status="failed")
    async with pool.connection() as conn, conn.transaction():
        await append_event(conn.cursor(), run_id, "run_failed")
    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET status='queued' WHERE id=%s", (run_id,))

    collected: list[str] = []

    async def pump():
        async for chunk in event_stream(pool, redis, run_id, ping_sec=0.1):
            collected.append(chunk)

    task = asyncio.create_task(pump())
    try:
        async with pool.connection() as conn, conn.transaction():
            await append_event(conn.cursor(), run_id, "run_started")
            await append_event(conn.cursor(), run_id, "run_succeeded")
        async with pool.connection() as conn:
            await run_db.finish(conn, run_id=run_id, owner=None, status="succeeded")  # adjust to the real signature

        async with asyncio.timeout(10):
            await task
    finally:
        task.cancel()

    types = "".join(collected)
    assert "run_started" in types and types.rindex("run_succeeded") > types.index("run_failed")
```

The second test needs the run's status to be non-terminal at the moment `run_failed` is read and terminal at the moment `run_succeeded` is read. If `run_db.finish` is awkward to call directly, set the status with plain SQL — the point is what the stream does, not which helper sets the column.

- [ ]  **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_api_events.py -q`
Expected: FAIL — the first stream ends as soon as the pump dies; the second stops at `run_failed`.

- [ ]  **Step 3: Implement both**

In `services/engine/engine/events/stream.py`:

```python
MAX_RESUBSCRIBES = 5
```

In the ping branch, replace `if subscriber.done(): return` with a degraded polling mode:

```python
            except TimeoutError:
                if subscriber.done():
                    # The subscription is gone. Every event is already in Postgres, so keep serving from
                    # there instead of ending the stream: a Redis outage should slow the editor down, not
                    # break it (2b design §8.2). Try to resubscribe a few times, then stay on polling.
                    if resubscribes < MAX_RESUBSCRIBES:
                        resubscribes += 1
                        ready = asyncio.Event()
                        subscriber = asyncio.create_task(pump(ready))
                        with contextlib.suppress(TimeoutError):
                            async with asyncio.timeout(1):
                                await ready.wait()
                for missed in await _stored_all(pool, run_id, last):
                    yield format_event(missed)
                    last = missed["seq"]
                    if missed["type"] in TERMINAL and await _is_terminal(pool, run_id):
                        return
                yield PING
                continue
```

with `resubscribes = 0` initialised next to `last`, and the pump refactored to take its own `ready` event so it can be restarted. Log the first failure at warning and nothing after it.

Replace both `if event["type"] in TERMINAL: return` checks with a status re-read:

```python
async def _is_terminal(pool: AsyncConnectionPool, run_id: str) -> bool:
    """A terminal *event* is not the end of the stream if the run has since been retried (2b design §8.3):
    Task 15's /retry appends run_queued after run_failed, and a client that had to reconnect to see it
    would need code the editor should not have to write."""
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT status FROM runs WHERE id=%s", (run_id,))).fetchone()
    return row is None or row["status"] in TERMINAL_STATUS


TERMINAL_STATUS = {"succeeded", "failed", "cancelled"}
```

- [ ]  **Step 4: Correct the contract that is now out of date**

`retry_run` in `services/engine/engine/api/routers/runs.py` carries a docstring saying the client has to
reconnect to see a retried run — that was true until this task and is now wrong. Replace that paragraph
with:

```python
    """Requeue a failed run from its last checkpoint (design 5.9). A client that is still attached sees the
    run continue on the same connection: the stream re-reads `runs.status` when it meets a terminal event
    and only ends if the run is really over (2b design §8.3), so the `run_queued` appended here keeps it
    open."""
```

Run: `grep -rn "reconnect" services/engine/engine` and fix any other comment that promises the old
behaviour.

- [ ]  **Step 5: Run the tests**

Run: `uv run pytest tests/test_api_events.py tests/test_api_runs.py -q`
Expected: PASS, run twice.

- [ ]  **Step 6: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine/events/stream.py services/engine/engine/api/routers/runs.py services/engine/tests/test_api_events.py
git commit -m "fix(engine): serve SSE from Postgres while Redis is down" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 14: Hardening — rendering inside the node's budget

**2b 설계 §8.4.** Today a node's `timeoutSec` covers `execute` only. Rendering has its own
`RENDER_TIMEOUT_SEC`, and the wait for a *free pool worker* has no bound at all — so `RENDER_POOL_SIZE` is
the worker's real concurrency limit and nothing says so.

**Files:**
- Modify: `services/engine/engine/compiler/wrapper.py`, `services/engine/engine/worker/render.py`,
  `services/engine/engine/config.py`, `services/engine/engine/api/main.py`,
  `services/engine/engine/worker/main.py`
- Test: `services/engine/tests/test_compiler_wrapper.py` (append),
  `services/engine/tests/test_worker_render.py` (append)

- [ ]  **Step 1: Write the failing tests**

Append to `services/engine/tests/test_compiler_wrapper.py`:

```python
async def test_a_slow_render_fails_the_node_with_its_own_timeout():
    """A render that outlives the node's budget must fail as NODE_TIMEOUT, not run on unbounded."""
    import asyncio

    async def slow_render(fields, outputs):
        await asyncio.sleep(10)
        return {}

    plan = make_plan(node_type="template", config={"template": "{{ start.topic }}"},
                     policy=Policy(timeoutSec=0.2, retry=RetrySpec(maxAttempts=1)))
    deps = make_deps(render=slow_render)

    state = await run_node(plan, deps, {"start": {"topic": "AI"}})

    assert state["error"]["code"] == "NODE_TIMEOUT"


async def test_the_render_and_the_call_share_one_budget():
    """Two `asyncio.timeout(timeout)` blocks would give a node twice its policy; one deadline does not."""
    import asyncio

    async def half_the_budget(fields, outputs):
        await asyncio.sleep(0.3)
        return {"text": "x"}

    plan = make_plan(node_type="llm", config={"prompt": "x"},
                     policy=Policy(timeoutSec=0.5, retry=RetrySpec(maxAttempts=1)))
    deps = make_deps(render=half_the_budget, llm=SlowLLM(0.3))

    started = asyncio.get_running_loop().time()
    state = await run_node(plan, deps, {})
    elapsed = asyncio.get_running_loop().time() - started

    assert state["error"]["code"] == "NODE_TIMEOUT"
    assert elapsed < 0.9  # not 0.3 + 0.5
```

`make_plan`, `make_deps` and `run_node` are this file's existing helpers — use whatever it already calls
them; `SlowLLM` is whatever slow stub the file already has (add one modelled on `ScriptedLLM` if not).
Match the existing style rather than introducing new helpers.

Append to `services/engine/tests/test_worker_render.py`:

```python
async def test_the_pool_serves_at_most_size_renders_at_once():
    """RENDER_POOL_SIZE is the worker's real concurrency limit; the queue in front of it has to be bounded
    too, or a saturated pool shows up as latency nobody configured (2b design §8.4)."""
    import asyncio

    from engine.worker.render import RenderPool

    pool = RenderPool(size=1, timeout=5.0, memory_limit_mb=None)
    try:
        fields = [TemplateField("text", "{{ start.n }}", "string")]
        first = asyncio.create_task(pool(fields, {"start": {"n": 1}}))
        await asyncio.sleep(0)
        assert pool.in_flight <= 1
        await first
    finally:
        await asyncio.to_thread(pool.close)


async def test_a_full_queue_makes_callers_wait_rather_than_pile_up():
    import asyncio

    from engine.worker.render import RenderPool

    pool = RenderPool(size=1, timeout=5.0, memory_limit_mb=None)
    try:
        fields = [TemplateField("text", "{{ start.n }}", "string")]
        results = await asyncio.gather(*(pool(fields, {"start": {"n": i}}) for i in range(5)))
        assert [result["text"] for result in results] == ["0", "1", "2", "3", "4"]
        assert pool.in_flight == 0
    finally:
        await asyncio.to_thread(pool.close)
```

- [ ]  **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_compiler_wrapper.py tests/test_worker_render.py -q`
Expected: FAIL — the slow render finishes after 10 s instead of timing out, and `RenderPool` has no
`in_flight`.

- [ ]  **Step 3: Give the attempt one deadline**

In `services/engine/engine/compiler/wrapper.py`, inside the `for tries in ...` loop:

```python
        for tries in range(1, max_attempts + 1):
            error = None
            # One deadline for the whole attempt, not one per stage: rendering is tenant-controlled CPU
            # work and waiting for a free render worker is queueing, so both belong inside the node's
            # policy budget (2b design §8.4). `timeout_at(None)` is simply no deadline.
            deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
            try:
                async with asyncio.timeout_at(deadline):
                    rendered = await _rendered(deps, fields, outputs)
            except CONTROL_FLOW:  # a dead render pool is the worker's problem, not this attempt's
                raise
            except Exception as exc:  # noqa: BLE001 - a template failure is this attempt's node error
                rendered, error = None, _as_node_error(exc, timeout)
```

and replace the `execute` block's `async with asyncio.timeout(timeout):` with
`async with asyncio.timeout_at(deadline):`.

- [ ]  **Step 4: Bound the render queue**

In `services/engine/engine/worker/render.py`:

```python
    def __init__(self, *, size: int = 2, timeout: float = 5.0,
                 memory_limit_mb: int | None = _DEFAULT_MEMORY_LIMIT_MB) -> None:
        self._timeout = timeout
        self._size = max(1, size)
        # pebble queues everything handed to it, so without this a hundred nodes hand it a hundred jobs and
        # each one's wait is invisible. With the semaphore the wait happens here, inside the caller's node
        # deadline (Step 3), so a saturated pool fails the node it belongs to instead of stretching every
        # run on the worker (2b design §8.4).
        self._slots = asyncio.Semaphore(self._size)
        # the rest of __init__ (the pebble ProcessPool, _closed, _futures) is unchanged

    @property
    def in_flight(self) -> int:
        return len(self._futures)

    async def __call__(self, fields: list[TemplateField], outputs: dict[str, Any],
                       secret_nonce: str | None = None) -> dict[str, Any]:
        async with self._slots:
            return await self._render(fields, outputs, secret_nonce)

    async def _render(self, fields: list[TemplateField], outputs: dict[str, Any],
                      secret_nonce: str | None) -> dict[str, Any]:
        # this is the current __call__, renamed: schedule, wrap_future, the three except branches and the
        # finally that discards the future all stay exactly as they are
```

Mechanically: rename today's `__call__` to `_render`, make its `secret_nonce` parameter required (the
default moves up to the new `__call__`), and add the two-line `__call__` above it. Nothing inside the
renamed method changes.

`asyncio.Semaphore()` binds to the running loop on first use, not at construction, so building the pool
outside the loop (as `worker/main.py` does) stays fine on Python 3.10+.

- [ ]  **Step 5: Size both pools the same way**

In `services/engine/engine/config.py`, add `db_pool_max: int` and
`db_pool_max=_int("DB_POOL_MAX", 10),`.

In `services/engine/engine/api/main.py`: `pool = make_pool(config.database_url, max_size=config.db_pool_max)`.

In `services/engine/engine/worker/main.py`:

```python
    # The worker needs a connection per in-flight run plus the recorder/reaper traffic around them; the
    # heartbeat has its own connection (2b design §8.1) and is deliberately not counted here.
    pool = make_pool(config.database_url, max_size=max(config.db_pool_max, config.worker_max_runs + 4))
    log.info("render pool size %s bounds concurrent template renders", config.render_pool_size)
```

- [ ]  **Step 6: Run the tests**

Run: `uv run pytest tests/test_compiler_wrapper.py tests/test_worker_render.py tests/test_worker_main.py tests/test_api_main.py -q`
Expected: PASS.

- [ ]  **Step 7: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine services/engine/tests
git commit -m "fix(engine): put rendering inside the node timeout and bound its queue" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 15: `ENGINE_API_TOKEN` becomes required

**2b 설계 §9.** An engine with no token serves every workflow, run and secret name to anyone who can reach
the port. Today an unset token is a warning.

**Files:**
- Modify: `services/engine/engine/config.py`, `services/engine/engine/api/middleware.py` (wherever
  `TokenAuthMiddleware` lives — check with `grep -rn "class TokenAuthMiddleware" services/engine/engine`)
- Test: `services/engine/tests/test_config.py` (append), `services/engine/tests/test_api_auth.py` (append)

- [ ]  **Step 1: Write the failing tests**

Append to `services/engine/tests/test_config.py`:

```python
def test_a_missing_api_token_is_refused_unless_dev_insecure(monkeypatch):
    _base_env(monkeypatch)  # the helper this file already uses to set the required variables
    monkeypatch.delenv("ENGINE_API_TOKEN", raising=False)
    monkeypatch.delenv("ENGINE_DEV_INSECURE", raising=False)

    with pytest.raises(ConfigError):
        load_config()

    monkeypatch.setenv("ENGINE_DEV_INSECURE", "1")
    assert load_config().api_token is None


def test_a_token_that_is_too_short_is_refused(monkeypatch):
    """A 'token' someone can guess is the same as no token."""
    _base_env(monkeypatch)
    monkeypatch.setenv("ENGINE_API_TOKEN", "short")

    with pytest.raises(ConfigError):
        load_config()
```

If `tests/test_config.py` has no `_base_env` helper, use whatever it already does to build a valid
environment and keep the two tests in that style.

- [ ]  **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL — `load_config()` returns a config instead of raising.

- [ ]  **Step 3: Implement**

In `services/engine/engine/config.py`, next to the existing `LANGGRAPH_AES_KEY` and `ENGINE_SECRET_KEY`
checks:

```python
MIN_API_TOKEN_LEN = 16

    api_token = os.getenv("ENGINE_API_TOKEN") or None
    if api_token is None and not dev_insecure:
        raise ConfigError("ENGINE_API_TOKEN is required (set ENGINE_DEV_INSECURE=1 only for development)")
    if api_token is not None and len(api_token) < MIN_API_TOKEN_LEN:
        raise ConfigError(f"ENGINE_API_TOKEN must be at least {MIN_API_TOKEN_LEN} characters")
```

and pass `api_token=api_token,` instead of reading the environment inline.

In the token middleware, the `config.api_token is None` branch now means "development, on purpose": keep
letting requests through and log once at startup, not per request:

```python
        if self._token is None:
            # Only reachable with ENGINE_DEV_INSECURE=1 — load_config refuses to build otherwise.
            log.warning("ENGINE_API_TOKEN is not set: every request is accepted (development only)")
```

Move that `log.warning` out of the request path and into `__init__` if it is not already there.

- [ ]  **Step 4: Fix the tests and fixtures that ran without a token**

Run: `grep -rln "load_config\|ENGINE_API_TOKEN" services/engine/tests`
Every fixture that builds an environment needs a token of at least 16 characters, except the tests that
are specifically about a missing one. `tests/conftest.py` already sets development defaults with
`os.environ.setdefault` — add `os.environ.setdefault("ENGINE_API_TOKEN", "dev-token-0123456789")` there.

- [ ]  **Step 5: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine services/engine/tests
git commit -m "feat(engine): require an API token unless development mode is on" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 16: Deployment — image, compose, and the environment file

**2b 설계 §10.** Ollama is not bundled: it already exists on the on-prem GPU host and is pointed at with
`OLLAMA_BASE_URL`.

**Files:**
- Create: `services/engine/Dockerfile`, `services/engine/.dockerignore`, `deploy/docker-compose.yml`,
  `deploy/.env.example`
- Modify: `services/engine/README.md`
- Test: manual (`docker compose config`, then a real `up`)

- [ ]  **Step 1: Write the Dockerfile**

Create `services/engine/Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.5-python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Dependencies first: they change far less often than the source, so this layer stays cached.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project --no-dev
COPY engine ./engine
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm AS runtime
# The render pool forks worker processes and the API serves untrusted templates: neither has any reason to
# be able to write to the image or to run as uid 0.
RUN useradd --create-home --uid 10001 engine
WORKDIR /app
COPY --from=build --chown=engine:engine /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER engine
CMD ["python", "-m", "engine.api.main"]
```

Create `services/engine/.dockerignore`:

```
.venv
__pycache__
*.pyc
.pytest_cache
tests
```

Pin the uv image tag to whatever `uv --version` reports locally if 0.5 is not what the project uses; check
with `uv --version` before building.

- [ ]  **Step 2: Write the compose file**

Create `deploy/docker-compose.yml`:

```yaml
name: engine

x-engine: &engine
  build:
    context: ../services/engine
  image: engine:local
  environment:
    ENGINE_DATABASE_URL: postgresql://engine:${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}@postgres:5432/engine
    ENGINE_REDIS_URL: redis://redis:6379/0
    ENGINE_API_TOKEN: ${ENGINE_API_TOKEN:?set ENGINE_API_TOKEN}
    LANGGRAPH_AES_KEY: ${LANGGRAPH_AES_KEY:?set LANGGRAPH_AES_KEY}
    ENGINE_SECRET_KEY: ${ENGINE_SECRET_KEY:?set ENGINE_SECRET_KEY}
    OLLAMA_BASE_URL: ${OLLAMA_BASE_URL:?set OLLAMA_BASE_URL}
    HTTP_ALLOWLIST: ${HTTP_ALLOWLIST:-}
    RUN_DATA_RETENTION_DAYS: ${RUN_DATA_RETENTION_DAYS:-30}
  depends_on:
    postgres: {condition: service_healthy}
    redis: {condition: service_healthy}
  restart: unless-stopped

services:
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: engine
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}
      POSTGRES_DB: engine
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U engine"]
      interval: 5s
      timeout: 3s
      retries: 10
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--save", "", "--appendonly", "no"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10
    restart: unless-stopped

  api:
    <<: *engine
    command: ["python", "-m", "engine.api.main"]
    ports: ["${ENGINE_API_PORT:-8000}:8000"]
    healthcheck:
      # /healthz sits behind the token like every other route, so the check has to carry it.
      test: ["CMD-SHELL", "python -c \"import os,urllib.request as u; r=u.Request('http://localhost:8000/healthz', headers={'Authorization': 'Bearer '+os.environ['ENGINE_API_TOKEN']}); u.urlopen(r, timeout=3)\""]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s

  worker:
    <<: *engine
    command: ["python", "-m", "engine.worker.main"]
    # Safe to scale: leases, fencing and the reaper's advisory lock were built for more than one.
    deploy:
      replicas: ${WORKER_REPLICAS:-1}

volumes:
  pgdata:
```

Redis holds only live event fan-out and control messages, all of which are rebuilt from Postgres, so
persistence is turned off deliberately.

- [ ]  **Step 3: Write the environment example**

Create `deploy/.env.example`:

```bash
# Copy to .env next to docker-compose.yml and fill in. Never commit the filled-in file.

# Both keys are 32 random bytes written as 32 ASCII characters (16 and 24 also work):
#   python -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(32)))"
# LANGGRAPH_AES_KEY encrypts checkpoints; ENGINE_SECRET_KEY encrypts stored secrets and run payloads.
# They are deliberately different keys: one leaking must not open the other. Rotating either one makes the
# data encrypted under the old key unreadable — there is no re-encryption path in this release.
LANGGRAPH_AES_KEY=
ENGINE_SECRET_KEY=

# At least 16 characters. Every route including /healthz requires it.
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
ENGINE_API_TOKEN=

POSTGRES_PASSWORD=

# The on-prem GPU host that already runs Ollama. Not bundled here.
OLLAMA_BASE_URL=http://ollama.internal:11434

# Comma-separated hosts http_request may reach. Empty means every request is blocked.
#   HTTP_ALLOWLIST=https://api.example.com,https://*.internal.example.com,http://10.0.0.7:8080;allowPrivate
HTTP_ALLOWLIST=

RUN_DATA_RETENTION_DAYS=30
WORKER_REPLICAS=1
ENGINE_API_PORT=8000
```

Make sure `deploy/.env` is ignored: `grep -n "^\.env" .gitignore` and add `deploy/.env` if it is not
covered.

- [ ]  **Step 4: Check the compose file parses**

Run: `docker compose -f deploy/docker-compose.yml --env-file deploy/.env.example config`
Expected: the resolved configuration prints. The `:?` variables are set (empty) in the example file, so
this validates the file without needing real values. If it fails on an empty required variable, that is
the file doing its job — fill that one in locally to check the rest.

- [ ]  **Step 5: Bring it up for real**

Copy `deploy/.env.example` to `deploy/.env`, fill in real values, then:

Run: `docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build`
Run: `docker compose -f deploy/docker-compose.yml ps`
Expected: `postgres`, `redis` healthy; `api` healthy; `worker` running.

Run: `curl -s -H "Authorization: Bearer $ENGINE_API_TOKEN" localhost:8000/healthz`
Expected: `{"status":"ok"}` (or whatever `/healthz` returns — check the route).

Run: `docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --scale worker=3`
Run: `docker compose -f deploy/docker-compose.yml logs worker | grep -c "worker started"`
Expected: 3. One of them wins the reaper's advisory lock each sweep; the others log the skip at debug.

Then run one workflow the whole way through, which is completion criterion 5 of the design:

```bash
curl -sX POST localhost:8000/workflows -H "Authorization: Bearer $ENGINE_API_TOKEN" -H 'Content-Type: application/json' -d '{"name":"smoke"}'
```

Save the workflow with `tests/golden/chaining.json` as its DSL, start a run with `{"topic":"AI"}`, then poll
`GET /runs/{id}` until it is `succeeded`. The LLM calls go to the real `OLLAMA_BASE_URL`, so this is also
the check that the on-prem host is reachable from inside the compose network — the one thing no unit test
can tell you.

Run: `docker compose -f deploy/docker-compose.yml down`

If Docker is not available in this environment, stop and report it rather than skipping the task: the
compose file is the deliverable and an unbuilt one is not evidence of anything.

- [ ]  **Step 6: Update the README**

Add a "Deployment" section to `services/engine/README.md` covering: copy `.env.example`, generate the two
keys and the token, point `OLLAMA_BASE_URL` at the GPU host, fill `HTTP_ALLOWLIST` (and that an empty one
blocks every `http_request`), `up -d --build`, and scaling workers with `--scale worker=N`. Say plainly
that rotating either key makes existing encrypted data unreadable.

- [ ]  **Step 7: Commit**

```bash
git add services/engine/Dockerfile services/engine/.dockerignore services/engine/README.md deploy .gitignore
git commit -m "feat(deploy): add the engine image, compose stack and environment example" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 17: End-to-end acceptance

**2b 설계 §11.2.** Every task so far proved its own piece against a fake. This one wires the real client and
the real resolver into the worker and proves the invariants through the API, against a real HTTP server.

**Files:**
- Modify: `services/engine/engine/worker/main.py`, `services/engine/tests/conftest.py`
- Create: `services/engine/tests/golden/http_call.json`, `services/engine/tests/test_acceptance_2b.py`
- Test: `services/engine/tests/test_golden_patterns.py` (append)

- [ ]  **Step 1: Wire the real ports into the worker**

Nothing has built a `GuardedClient` or a `PostgresSecretResolver` yet — Task 6 gave `Worker` the two
parameters and Task 9 used them, but the entrypoint still passes neither.

In `services/engine/engine/worker/main.py`, where the worker is constructed:

```python
    resolver = SystemResolver(limit=config.worker_max_runs)
    http = GuardedClient(config.http_allowlist, resolver=resolver,
                         max_redirects=config.http_max_redirects,
                         max_request_bytes=config.http_max_request_bytes,
                         max_response_bytes=config.http_max_response_bytes)
    secrets = PostgresSecretResolver(pool, config.secret_key)
    worker = Worker(config, pool, redis, owner=owner, llm=llm, http=http, secrets=secrets)
```

with the imports, and `await http.aclose()` in the shutdown path next to the other `close` calls (check
what the `finally` in `main.py` already closes and follow it exactly). If `config.http_allowlist` is empty
the client still builds — every request is then blocked, which is the documented default.

Log the allowlist once at startup, hosts only:

```python
    log.info("egress allowlist: %s", ", ".join(f"{e.scheme}://{e.host}:{e.port}" for e in config.http_allowlist) or "(empty: all http_request calls are blocked)")
```

In `services/engine/tests/conftest.py`, let `worker_factory` take the two ports so an acceptance test can
hand in a client pointed at its own server:

```python
    async def make(llm, *, owner: str = "worker-1", http=None, secrets=None, **overrides):
        import dataclasses

        config = dataclasses.replace(load_config(), claim_poll_sec=0.2, heartbeat_sec=0.2, **overrides)
        worker = Worker(config, pool, redis, owner=owner, llm=llm, http=http, secrets=secrets)
```

- [ ]  **Step 2: Add the sixth golden**

Create `services/engine/tests/golden/http_call.json` — the pattern a tenant actually writes: call an API
with a stored credential, then summarise what came back.

```json
{
  "version": "1",
  "name": "http_call",
  "nodes": [
    {"id": "start", "type": "start", "config": {"inputs": [{"name": "issue", "type": "string", "required": true}]}},
    {"id": "http_1", "type": "http_request",
     "config": {"method": "GET",
                "url": "{{ start.issue }}",
                "headers": {"Authorization": "Bearer {{ secret.API_TOKEN }}"}},
     "policy": {"timeoutSec": 10, "retry": {"maxAttempts": 1}}},
    {"id": "template_1", "type": "template",
     "config": {"template": "상태 {{ http_1.status }}: {{ http_1.body.title }}"}},
    {"id": "end", "type": "end", "config": {"outputs": {"summary": "{{ template_1.text }}"}}}
  ],
  "edges": [
    {"source": "start", "target": "http_1"},
    {"source": "http_1", "target": "template_1"},
    {"source": "template_1", "target": "end"}
  ]
}
```

Check the exact shape of `start`, `template` and `end` configs against `tests/golden/chaining.json` and
copy its conventions — the fields above are the ones this workflow needs, not necessarily the spelling the
other goldens use.

Append `"http_call"` to `PATTERNS` in `services/engine/tests/test_golden_patterns.py` so it is validated
with the rest. Do not add an `execute_run` test for it there: that file runs against `InMemorySaver` with
no HTTP port, and the real execution is Step 4's acceptance test.

- [ ]  **Step 3: Add the server fixture**

Append to `services/engine/tests/conftest.py`:

```python
@pytest.fixture
def http_server():
    """A real HTTP server on 127.0.0.1, so the guarded client makes a real connection.

    It echoes the Authorization header back in the body, which is what makes "a secret the server returns
    never reaches storage" testable end to end.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's own naming
            body = json.dumps({"title": "이슈 제목", "seen": self.headers.get("Authorization", "")})
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Set-Cookie", "session=should-not-be-stored")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # keep pytest output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
```

- [ ]  **Step 4: Write the acceptance tests**

Create `services/engine/tests/test_acceptance_2b.py`:

```python
"""The invariants from 2b design §11.2, end to end: API in, worker out, real sockets.

Every other test in this branch proves one component against a fake. These prove the assembled thing, and
in particular the claim the whole secret design rests on: a secret exists in plaintext inside the
http_request node and nowhere else that anyone can read.
"""
import json
import logging

import pytest

from engine.http.client import GuardedClient, SystemResolver
from engine.http.policy import parse_allowlist
from engine.llm.scripted import ScriptedLLM
from engine.secrets.store import PostgresSecretResolver, put_secret
from tests.helpers import load_golden

SECRET = "hunter2-a-real-looking-token"
KEY = b"0" * 32


async def _saved(api, dsl) -> str:
    created = (await api.post("/workflows", json={"name": "acceptance"})).json()
    workflow_id = created["id"]
    saved = await api.put(f"/workflows/{workflow_id}", json={"dsl": dsl, "revision": created["revision"]})
    assert saved.status_code == 200, saved.text
    return workflow_id


async def _worker(worker_factory, pool, allowlist: str):
    http = GuardedClient(parse_allowlist(allowlist), resolver=SystemResolver())
    secrets = PostgresSecretResolver(pool, KEY)
    worker = await worker_factory(ScriptedLLM([]), http=http, secrets=secrets, secret_key=KEY)
    return worker, http


async def _finished(api, run_id: str) -> dict:
    from tests.conftest import until

    async def done():
        run = (await api.get(f"/runs/{run_id}")).json()
        return run if run["status"] in ("succeeded", "failed", "cancelled") else None

    return await until(done, timeout=30)


async def _streamed_events(api, run_id: str) -> str:
    """What a browser attached to the run would have seen. The run is already finished, so the stream
    replays every stored event and ends at the terminal one."""
    lines: list[str] = []
    async with api.stream("GET", f"/runs/{run_id}/events") as response:
        async for line in response.aiter_lines():
            lines.append(line)
            if "run_succeeded" in line or "run_failed" in line:
                break
    return "
".join(lines)


async def _everything_stored(pool, run_id: str) -> str:
    """Every row a person or a backup could read, as one string."""
    async with pool.connection() as conn:
        runs = await (await conn.execute("SELECT * FROM runs WHERE id=%s", (run_id,))).fetchall()
        nodes = await (await conn.execute("SELECT * FROM node_runs WHERE run_id=%s", (run_id,))).fetchall()
        events = await (await conn.execute("SELECT * FROM run_events WHERE run_id=%s", (run_id,))).fetchall()
        points = await (await conn.execute(
            "SELECT checkpoint, metadata FROM checkpoints WHERE thread_id=%s", (run_id,))).fetchall()
    return repr([runs, nodes, events, points])


async def test_a_secret_reaches_the_server_and_nothing_else(api, pool, worker_factory, http_server, caplog):
    """The four places at once (2b design §11.2): database rows, the API's own responses, the event stream
    and the logs. If the plaintext is in any of them the design has failed, whichever one it is."""
    caplog.set_level(logging.DEBUG)
    async with pool.connection() as conn:
        await put_secret(conn, KEY, workspace_id="default", name="API_TOKEN", value=SECRET)
    workflow_id = await _saved(api, load_golden("http_call"))
    await _worker(worker_factory, pool, f"{http_server};allowPrivate")

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"issue": f"{http_server}/issues/1"}, "revision": 2})).json()
    run = await _finished(api, created["runId"])

    assert run["status"] == "succeeded", run
    assert "이슈 제목" in json.dumps(run["outputs"], ensure_ascii=False)  # the call really happened
    stored = await _everything_stored(pool, created["runId"])
    nodes = (await api.get(f"/runs/{created['runId']}/nodes")).json()
    events = await _streamed_events(api, created["runId"])
    for place, text in (("database", stored), ("run view", json.dumps(run, ensure_ascii=False)),
                        ("node runs", json.dumps(nodes, ensure_ascii=False)),
                        ("logs", caplog.text), ("events", events)):
        assert SECRET not in text, f"the secret leaked into the {place}"
    assert "[REDACTED]" in json.dumps(nodes, ensure_ascii=False)  # and it was replaced, not just absent


async def test_a_host_outside_the_allowlist_fails_the_run(api, pool, worker_factory, http_server):
    workflow_id = await _saved(api, load_golden("http_call"))
    async with pool.connection() as conn:
        await put_secret(conn, KEY, workspace_id="default", name="API_TOKEN", value=SECRET)
    await _worker(worker_factory, pool, "https://api.example.com")  # not the test server

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"issue": f"{http_server}/issues/1"}, "revision": 2})).json()
    run = await _finished(api, created["runId"])

    assert run["status"] == "failed"
    assert run["error"]["code"] == "HTTP_BLOCKED"
    assert "127.0.0.1" not in json.dumps(run, ensure_ascii=False)  # the category, never the address


async def test_a_purged_run_cannot_be_retried(api, pool, redis, worker_factory):
    """2b design §11.2: retention removes what a retry would need, and the API has to say so rather than
    requeue a run that cannot possibly finish."""
    import dataclasses

    from engine.config import load_config
    from engine.worker.reaper import Reaper

    workflow_id = await _saved(api, load_golden("chaining"))
    await worker_factory(ScriptedLLM([RuntimeError("모델 없음")]))
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    run = await _finished(api, created["runId"])
    assert run["status"] == "failed"

    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET finished_at = now() - interval '400 days' WHERE id=%s",
                           (created["runId"],))
    await Reaper(dataclasses.replace(load_config(), retention_days=30, purge_batch=100), pool, redis).sweep()

    response = await api.post(f"/runs/{created['runId']}/retry")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_DATA_EXPIRED"
    purged = (await api.get(f"/runs/{created['runId']}")).json()
    assert purged["status"] == "failed" and purged["inputs"] is None  # metadata kept, payload gone


async def test_deleting_a_workflow_leaves_no_orphan_checkpoints(api, pool, redis, worker_factory):
    import dataclasses

    from engine.config import load_config
    from engine.worker.reaper import Reaper

    workflow_id = await _saved(api, load_golden("chaining"))
    await worker_factory(ScriptedLLM(["개요", "본문"]))
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    await _finished(api, created["runId"])
    async with pool.connection() as conn:
        before = await (await conn.execute("SELECT count(*) AS n FROM checkpoints WHERE thread_id=%s",
                                           (created["runId"],))).fetchone()
    assert before["n"] > 0

    assert (await api.delete(f"/workflows/{workflow_id}")).status_code in (200, 204)
    async with pool.connection() as conn:
        await conn.execute("UPDATE checkpoints SET created_at = now() - interval '2 hours'"
                           " WHERE thread_id=%s", (created["runId"],))
    await Reaper(dataclasses.replace(load_config(), retention_days=30, purge_batch=100), pool, redis).sweep()

    async with pool.connection() as conn:
        after = await (await conn.execute("SELECT count(*) AS n FROM checkpoints WHERE thread_id=%s",
                                          (created["runId"],))).fetchone()
    assert after["n"] == 0
```

Two things to confirm while writing this file rather than copying blindly:

- `worker_factory(..., secret_key=KEY)` assumes `secret_key` is an `EngineConfig` field that
  `dataclasses.replace` accepts (Task 4 added it). Confirm the name before running.
- `ScriptedLLM([RuntimeError(...)])` assumes the stub raises what it is given. Check
  `engine/llm/scripted.py`; if it does not, make the run fail the way the existing failure tests do.

- [ ]  **Step 5: Run them**

Run: `uv run pytest tests/test_acceptance_2b.py tests/test_golden_patterns.py -q`
Expected: PASS. Run twice — these drive real workers and a real socket, so a pass that only happens once is
a failure.

If the first test fails because the secret appears in `caplog`, do not weaken the assertion: find the log
line and fix it. That assertion is the point of the task.

- [ ]  **Step 6: Run everything and commit**

Run: `uv run pytest -q` → all pass.
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/engine services/engine/tests
git commit -m "test(engine): prove the 2b invariants end to end" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 18: Documentation and final verification

**Files:**
- Modify: `services/engine/README.md`
- Test: the whole suite, plus a migration round trip

- [ ]  **Step 1: Document what a tenant can now do**

Add to `services/engine/README.md`, after the existing node list:

- **`http_request`** — method, URL, headers and body are templates. `GET`/`PUT`/`DELETE`/`HEAD` retry three
  times; `POST`/`PATCH` run once because a retry can double a side effect. Output is
  `{status, headers, body}`; a non-2xx status fails the node (`429` and `5xx` are retryable). Only hosts in
  `HTTP_ALLOWLIST` can be reached, and only over the exact scheme and port the entry names.
- **Secrets** — `PUT /secrets/{NAME}` stores a value, `GET /secrets` lists names only, `DELETE /secrets/{NAME}`
  removes one. Values are never readable through the API after they are written. Reference one as
  `{{ secret.NAME }}`, and only in an `http_request` node's URL, headers or body.
- **What a secret looks like everywhere else** — the stored template keeps the reference; the recorded node
  input keeps an opaque marker; anything the server echoes back comes out as `[REDACTED]`.

Add an "Operations" section covering:

- `HTTP_ALLOWLIST` syntax with the three forms (`https://host`, `https://*.sub.example.com`,
  `http://10.0.0.7:8080;allowPrivate`), comma-separated, and that an empty list blocks everything. A
  malformed entry refuses to start.
- `RUN_DATA_RETENTION_DAYS` (default 30): after that a finished run keeps its metadata and loses its
  payloads, events bodies and checkpoints. A retry after that returns 409 `RUN_DATA_EXPIRED`.
- The two keys and the token, and that `ENGINE_DEV_INSECURE=1` is the only way to start without them.
- That rotating `ENGINE_SECRET_KEY` or `LANGGRAPH_AES_KEY` makes existing data unreadable: there is no
  re-encryption path in this release.

- [ ]  **Step 2: Verify the migrations both ways**

Run: `uv run alembic -c engine/db/alembic.ini upgrade head` (check the real config path with
`ls services/engine/engine/db`)
Run: `uv run alembic -c engine/db/alembic.ini downgrade 0001`
Run: `uv run alembic -c engine/db/alembic.ini upgrade head`
Expected: all three succeed. A downgrade that fails is a broken migration even though nothing is deployed
yet — it is what an operator reaches for when an upgrade goes wrong.

- [ ]  **Step 3: Verify the whole branch**

Run: `uv run pytest -q`
Expected: every test passes. Record the count.

Run: `uv run ruff check .`
Expected: `All checks passed!`

Run: `docker compose -f deploy/docker-compose.yml --env-file deploy/.env config -q`
Expected: no output.

Run: `git log --oneline feat/runtime-core..HEAD`
Expected: one commit per task, in order, each with the trailer.

- [ ]  **Step 4: Check the secret really is absent, by hand**

With the stack up and one `http_call` run finished:

```bash
docker compose -f deploy/docker-compose.yml exec postgres psql -U engine -d engine -c "SELECT count(*) FROM node_runs WHERE input::text LIKE '%hunter2%' OR output::text LIKE '%hunter2%'"
```

Expected: `0`, using whatever value you actually stored. Do the same against `run_events.payload`. This is
the check an operator would run, and it is worth running once by hand rather than only in a fixture.

- [ ]  **Step 5: Commit**

```bash
git add services/engine/README.md
git commit -m "docs(engine): document http_request, secrets, egress and retention" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Design coverage (Plan 2b)

Every section of `docs/superpowers/specs/2026-09-17-http-security-ops-design.md` and where it is built.

| 설계 | 구현 |
|---|---|
| §3 `http_request` 노드, 두 포트 | Task 6 (포트 정의), Task 9 (노드) |
| §4.1 시크릿 저장 | Task 3 (테이블), Task 4 (AES-GCM, 리졸버) |
| §4.2 쓰기 전용 API | Task 5 |
| §4.3 마커 렌더링, nonce | Task 6 |
| §4.4 참조 허용 범위 | Task 7 |
| §5.1 허용 목록 형식 | Task 1 |
| §5.2 와일드카드·포트·`allowPrivate` | Task 1 |
| §5.3 IP 분류 | Task 1 |
| §5.4 DNS 전 검사, 전체 A/AAAA, 핀 고정 연결 | Task 0 (증명), Task 2 (구현) |
| §5.5 리다이렉트 재검사 | Task 2 |
| §5.6 오류 코드 | Task 9 |
| §6 계층 1 헤더 이름 레닥션 | Task 8 |
| §6 계층 2 값 레닥션 | Task 6 (`redact_values`), Task 9 (적용) |
| §7 보존 기간 정리 | Task 11 |
| §7 고아 체크포인트 | Task 11 |
| §8.1 하트비트 전용 연결 | Task 12 |
| §8.2 SSE 폴링 강등 | Task 13 |
| §8.3 재시도 후 스트림 계약 | Task 13 |
| §8.4 렌더링을 노드 타임아웃 안으로 | Task 14 |
| §9 `ENGINE_API_TOKEN` 필수 | Task 15 |
| §9 `ENGINE_SECRET_KEY` 필수 | Task 4 |
| §9 `runs.inputs/outputs` 암호화 | Task 3 (열), Task 10 (코덱) |
| §9 시크릿 최소 8자 | Task 5 |
| §10 배포 | Task 16 |
| §11.1 TLS 핀 스파이크 | Task 0 |
| §11.2 인수 인바리언트 | Task 2 (egress), Task 9 (secret), Task 17 (종단 간) |

## Post-review notes

Append one note per task here after its adversarial review, the way
`docs/superpowers/plans/2026-09-16-runtime-core.md` does: what the review looked for, what it found, and
what changed. A task with a clean review still gets a note saying so and naming what was checked.

### Task 0 — spike: pinned-IP TLS

**Commits:** `34963df`, `fd72b0e`, `e398f58`.

**The gate holds.** Connecting to a validated IP while passing `Host` and
`extensions={"sni_hostname": ...}` keeps TLS certificate verification bound to the original hostname.
Proven against a real TLS server with a `trustme` CA, not inferred from reading `httpcore`. The §12
fallbacks (hand-rolled `wrap_socket`, dedicated egress proxy) are not needed; §5 is safe to build on.

**Spec review** confirmed the tests are not vacuous by running the negative controls itself: with
`verify=False` both negative tests stop raising, and with a foreign CA the failure is
`CERTIFICATE_VERIFY_FAILED`. It captured the wire traffic (SNI and `Host` both `api.internal.test`,
socket to `127.0.0.1`, TLS 1.3, real 200) and confirmed a plain connection refusal does *not* satisfy the
`"certificate"`/`"hostname"` assertion, so the assertion discriminates. No socket or server leakage across
repeated runs under `-X dev -W always::ResourceWarning`.

It found one real gap the plan itself had: the file's docstring claims to guard **Host**, SNI and
certificate verification, but the test server read the request and threw it away, so every test would have
passed if httpx stopped honouring an explicit `Host` override. `_serve` now records the request head and
the first test asserts on it. Verified falsifiable: dropping the override produces
`Host: 127.0.0.1:<port>` and fails the test.

**Quality review** found the follow-up's second assertion
(`LOCALHOST not in received[0].split("

")[1]`) was both redundant — the exact-match check above it
already pinned the Host line — and silently dependent on `Host` landing at line index 1. Replaced with
`LOCALHOST not in received[0]`, which checks the whole request head and is order-independent; proven to
fail on its own, with the first assertion commented out. The first test was renamed to name both legs it
proves.

**Deviations from the plan as written:** each `client.get` is wrapped in a `_get` helper that bounds it at
5 s and converts a bare `TimeoutError` (whose `str()` is empty) into a `pytest.fail` naming the URL —
convention 5. The two `async with` statements are combined, which ruff's SIM117 requires. The plan's code
block at Task 0 Step 2 still shows the original test name and no Host assertion; it is a pre-implementation
snippet and was left as the record of what was asked for.

### Task 1 — egress policy

**Commits:** `a717018`, `4b856c1`, `6a1b37c`, `3de6037`, `a2e6044`, `e1c8d7b`, `dae5805`. Design changes
this task forced are recorded in the spec (`c3c5072`, `35b5962`).

**The plan shipped a default-allow classifier, and that was a real SSRF hole.** The first implementation
reproduced the plan's code byte for byte. A differential oracle — comparing `ip_category` against
`ipaddress`'s own classification across 1,060,407 addresses — found 145 addresses the policy called public
that are not globally routable. Three were reachable from a tenant-controlled AAAA record:
`::169.254.169.254` (IPv4-compatible), `::ffff:0:169.254.169.254` (SIIT translated) and
`64:ff9b:1::a9fe:a9fe` (NAT64 local-use, directly routable in IPv6-only VPCs, and sibling to a prefix that
*was* already in the table). Two of them reach cloud metadata.

Fixed by inverting the architecture: the table now *names* a blocked address, every IPv6-embeds-IPv4 form
is unwrapped, and an address not in the table passes only if it is genuinely globally routable. The table
stays authoritative rather than delegating to `is_global`, because `ipaddress`'s judgement of a few ranges
has changed across Python patch releases and a security boundary must not move when the interpreter is
upgraded. Final oracle: zero holes.

**`allowPrivate` exempted only `private`, which made every one of the plan's own TLS tests impossible** —
they all resolve to `127.0.0.1`, which classifies as `loopback`. Caught while reading Task 2 rather than
by running it. The exemption set is now `{private, loopback, cgnat}` and deliberately excludes
`link-local`: an operator who opened one internal API did not ask for `169.254.169.254`.

**`match` failed open on duplicate entries.** First-match-wins meant
`"https://api.example.com;allowPrivate, https://api.example.com"` granted private access purely because
the privileged copy was written first — realistic when two allowlists are merged. Now most-specific-wins,
order-independent, and a tie goes to the least-privileged entry. Renamed to `find_entry` because `match`
and `AllowEntry.matches` were one word apart with different return types.

**Mutation testing drove three rounds.** 14 of 38 mutations survived the first pass, including
`AllowEntry.matches` returning `True` unconditionally for non-wildcard hosts. Two survivors turned out to
be genuine *equivalent* mutants and the implementer correctly refused to write tests for unobservable
behaviour: narrowing `240.0.0.0/4` was unobservable because `"reserved"` was both a table name and the
fallback name, and that was fixed at the source by giving the fallback its own category, `"non-global"` —
which makes every table entry's prefix length observable, not just that one.

**Quality review** found three arguments each restated three or four times (comment/code ratio 1.06
against a house range of 0.09–0.27), a test comment claiming coverage of "every range" while covering 7 of
23, and six addresses pinned to `== "non-global"` that would have punished a maintainer for the correct
hardening of tabling a range. All fixed; ratio now 0.93, the remainder being non-repeated reasoning in two
long docstrings.

**Known flakes, unrelated to this task and untouched by it:** two timing-sensitive tests failed once each
across many full-suite runs and never in isolation —
`tests/test_worker_render.py::test_a_slow_template_hits_the_deadline_and_the_pool_survives` (a 0.5 s
render deadline raced against a `< 5 s` wall-clock assertion) and
`tests/test_worker_lease.py::test_a_run_over_its_active_time_limit_fails` (a 300 ms deadline against real
containers). Both have the same shape: a hard-coded deadline raced against wall-clock under full-suite
load. Revisit when Task 14 touches the render pool.

### Task 2 — the guarded HTTP client

**Commits:** `0b18123`, `a8199b7`, `37ae89f`, `ba174a1`. Plan file structure updated in `4b4ec71`.

**The core SSRF property held from the first round.** Across two adversarial rounds nothing reached a
socket for an address `ip_category` rejects: userinfo confusion, relative and protocol-relative `Location`
headers, split DNS answers, rebinding across hops, percent-encoding, trailing dots and IPv6 zone ids all
fail closed. There is no TOCTOU window, because the URL handed to httpx already contains the validated IP
literal.

**Everything around that property was broken, and the plan's code was the source of most of it.** The
implementer found five defects in the plan before review even started: tenant headers merged as
`{**headers, "Host": …}` (h11 refuses two Host headers, so the plan's code was a tenant-triggerable denial
of every request); `Authorization` and `Cookie` forwarded unchanged to a cross-origin redirect; no IDNA
encoding, though `find_entry` refuses non-ASCII hosts; a `MAX_DECODED_CHARS` constant that was dead below
5 MB and a silent *lower* cap above it; and an unbracketed IPv6 `Host`.

Review then found three more, all of which needed *executing* something rather than reading it:

- **TLS verification was bypassed by connection reuse.** httpcore keys its pool on `(scheme, IP, port)` and
  `sni_hostname` is not in the key, so a second request to a *different* hostname on the same IP:port
  reused the first verified connection with no handshake. One TCP accept, two hostnames, both 200. The
  client is one per worker, so this crossed runs and tenants. Closed with
  `max_keepalive_connections=0`.
- **The cookie jar was live.** `cookies=None` seeds an empty jar, it does not disable one, and because the
  request URL carries the pinned IP every cookie became a host-only cookie for that IP — leaking across
  hostnames and re-adding the exact `Cookie` header the redirect filter strips.
- **Request smuggling.** Only `Host` was stripped; `Content-Length`, `Transfer-Encoding`, `Connection` and
  `Expect` reached the wire verbatim. One tenant header produced both framing headers, and a real desync
  was demonstrated against a keep-alive server with a chunk-size line parsed as the next request line.

Plus a decompression bomb that peaked at 148.7 MB against a 1,000-byte cap (httpx yields *decompressed*
bytes), and a `log.warning` — written into the plan by me — that printed the query string two lines under
a comment about not leaking the URL, on a path where Task 7 puts secrets.

**The fix for the TLS bypass had a test that could not fail.** Deleting the entire `limits=` argument left
all 60 tests passing, because the test's server closed the connection after one request in its `finally`;
the `sleep(0.3)` delayed that close rather than preventing it. Only a mutant exposed it. The same fix also
silently removed httpx's default `max_connections=100` — `Limits.__init__` defaults it to `None`, meaning
unlimited — so a security fix uncapped file descriptors.

**Structure.** Header rules moved to `engine/http/headers.py` after `client.py` accumulated six invariants
a modifier had to hold simultaneously with nothing but comments enforcing them, and 8 of 18 surviving
mutants were header-table entries. The split was judged successful on re-review, with one leak (the
bodyless-GET content strip) closed in the final round. The order in `_resolve_target` — allowlist, DNS,
validate every address, pin — was deliberately left alone; it survived every attack and reads top to
bottom on one screen.

**Accepted trade, recorded:** `Accept-Encoding: identity` plus `aiter_raw()` means a server that
compresses anyway now fails with `UnsupportedMedia` rather than being decompressed transparently. Bounded
incremental decompression was considered and rejected for now: the httpx route needs a private
underscore-prefixed API in a security-critical module, and bounding output size does not bound
decompression CPU.

**Renames from the plan's text:** `match` → `find_entry`, `_check` → `_resolve_target`,
`_request` → `_follow_redirects`, `is_hostname_syntax` → `has_hostname_syntax`,
`redirect_headers` → `headers_for_redirect`. The plan's Task 2 code block still shows the original names
and a `previous_scheme` parameter that was never used; it is left as the record of what was asked for.

### Task 3 — migration 0002

**Commits:** `0317c16`, `a9f7fec`.

**Not yet verified.** Docker Desktop's daemon was not running, so every integration test — including
`test_the_secrets_table_and_retention_columns_exist`, the only thing that executes this migration — was
deselected from every run. `977 passed, 210 deselected` is a real number and it proves nothing about this
task. **The migration SQL has never been executed against a Postgres.** Re-run `uv run pytest -q` with
Docker up before treating Task 3 as done, and before starting Task 4, which writes to these tables.

**`CHECK (length(name) <= 64)` was added to `secrets.name`, deliberately.** `name` is half of a btree
primary key, and an oversized btree key is not a validation error but a `ProgramLimitExceeded` — a 500.
Plan 2a hit exactly that with a 3 KB idempotency key. The API will constrain names to
`^[A-Z][A-Z0-9_]{0,63}$` (Task 5), so the column agrees with the API rather than trusting it. Two reviewers
flagged this as an unapproved deviation; it was not — the implementer was explicitly asked to consider that
failure mode, and reported it. Keep it.

**The downgrade is re-runnable.** `DROP INDEX IF EXISTS`, because a downgrade is what an operator reaches
for when something has already gone wrong, and a partially-applied one should not be a dead end.

**Still owed, and it needs Docker:** proof that the query planner actually chooses `runs_retention_idx` for
the retention sweep's real query, via `EXPLAIN` against a populated `runs` table. An index the planner
ignores costs write throughput and misleads every future reader. The review lens assigned to this reported
"cannot verify without populated runs table" instead of populating one, and presented static analysis as
if it were verification — the finding is real but unproven either way.

**Update (2026-09-18).** A parallel session unblocked this: `3679c48` added `ENGINE_TEST_DATABASE_URL` /
`ENGINE_TEST_REDIS_URL`, so the integration tests run against an already-running server where Docker is
not available. Running them found that
`test_a_secret_name_longer_than_the_api_allows_is_refused` **had never executed** — it used
`async with pool.connection() as conn, pytest.raises(CheckViolation):`, and `pytest.raises` is a plain
context manager, so it cannot be the second item of an `async with`. Fixed in the same commit. That is the
predicted failure arriving exactly as predicted: a test written while the only harness that could run it
was unavailable, asserted to be green on the strength of `-m "not integration"`.

The `EXPLAIN` check on `runs_retention_idx` is **still owed** — nothing has run it yet.

### Task 6 — post-review note

Two things in the plan's Task 6 were wrong, and both were found by running rather than reading.

**"Existing callers keep working because the new argument has a default" is false for the test doubles.**
`render_fields` and `RenderPool.__call__` do default the new argument, but `_rendered` calls
`deps.render(fields, outputs, deps.secret_nonce)` with three positional arguments, and the four render
hooks in `tests/test_compiler_wrapper.py` are hand-written two-parameter functions. Step 8's expectation
of a clean run was wrong: four tests failed on arity. The doubles now take the nonce explicitly, which is
the right shape anyway — they implement `RenderFn`, so they should break when its contract changes.

**Nothing proved the nonce survives the off-loop path.** The plan's two new wrapper tests both exercise
the inline renderer. The pool path is exactly where the nonce can silently go missing, and dropping it
there is invisible in ordinary use — every `http_request` template simply fails. The hook test now
asserts the forwarded nonce.

**Mutation results.** Eight mutants, one survivor on the first pass:
`substitute`'s `found.group(2) != nonce` guard could be deleted with every test still green, because the
plan's test paired this run's `API_TOKEN` marker with a *foreign* `OTHER` marker — a name that is absent
from `values` either way, so the lookup misses and the output is identical with or without the check. The
property that matters is the same name under a foreign nonce: text carried over from another run, or
tenant text that guessed a name, must not be filled with this run's value. Both `substitute` and
`find_names` now have that case, and all eight mutants are killed. The plan's version of this test was
the §10 pattern again: a test that passes whether or not the thing it names is there.

### Task 7 — post-review note

**Step 2's xfail instruction was wrong in two ways, and both hid a vacuous test.** It named only the two
"allowed" cases, but `analyze` stops at `UNKNOWN_NODE_TYPE` before reference checking ever runs, so *all
four* `http_request` cases are blocked until Task 9 — including the two that assert a secret is refused.
Worse, the two "allowed" assertions as written (`"SECRET_NOT_ALLOWED" not in codes`) are *vacuously true*
today for exactly that reason: no reference check ran, so of course nothing was reported. Marked
`strict=True` they fail as XPASS, which is how this surfaced. They now assert `UNKNOWN_NODE_TYPE` is
absent as well, so they fail today for the stated reason and can only pass once the node exists and the
door is genuinely open. A fifth case was added for `{{ secret.A.B }}`, which the `len(ref.path) == 1`
rule refuses and the plan's tests never covered.

**Two mutants survive, and both are equivalent given today's node set — not test debt to pay now.**
Dropping the field rule (`field_ok = True`) and dropping the node-type rule both leave every test green,
because the two halves of the condition are only separable when a node exists that satisfies one and not
the other. No node type currently produces a `url`, `body` or `headers.*` template field — the `end`
node's paths are `outputs.{key}`, never a bare `body` — and until Task 9 no node is `http_request`. The
rules are each other's only witness.

**What Task 9 owes because of this.** Removing the strict xfail markers makes the node-type rule
observable. The field rule still will not be, unless Task 9 adds a case for an `http_request` template
field that is *not* `url`/`body`/`headers.*` — if the node has one. If it has no such field, say so in
its note rather than leaving the gap unrecorded: a rule with no possible witness is a rule no test
protects, and the next person to add a `body` field to some other node type will never hear about it.

### Task 8 — post-review note

The plan's two tests leave the actual defence unguarded. `redact_headers` is a list plus a lookup rule,
and both halves can be broken without either test noticing: they name four headers between them, so
dropping `proxy-authorization` or `x-auth-token` from `SECRET_HEADERS` stays green, and switching the
exact lookup to substring matching stays green too. Three cases were added — every name in the list,
a name that merely *contains* one (`x-cookie-policy`, which must not be redacted), and two spellings of
one header collapsing to a single redacted entry. Four mutants, all killed.

The substring case is the one worth keeping in mind: blanket matching looks safer and is not. It hides
ordinary fields from whoever is debugging a failed run, which is the cost that makes people turn
redaction off.

### Task 9 — post-review note

**One real gap, found by mutation: nothing checked that the *validator* honours `policy_for`.** Step 5
changes `structure.py` to resolve a node's effective policy through `spec.policy_for(config)`, but the
plan's tests only call `HttpRequestNode().policy_for(...)` directly. Reverting `structure.py` to
`spec.default_policy` left all 1,303 tests green — and that revert is precisely the bug `policy_for`
exists to prevent: a POST would validate and execute with three attempts, so a retry can double a
payment (MVP design 5.4). `tests/test_validator_refs.py` now resolves the policy through `analyze` for
all six methods, and checks that an explicit `policy` on the node still overrides the per-method default.

**Task 7's two equivalent mutants are still equivalent, and now provably so.** `http_request`'s template
fields are exactly `url`, `headers.*` and `body` — all three are on the allowed list — so there is no
`http_request` field that witnesses the field rule, which is what Task 7's note asked this task to check.
No other node type produces a `url`/`body`/`headers.*` field either, so the node-type rule has no witness
of its own. The two halves of that condition remain each other's only evidence, by construction rather
than by oversight. Anyone adding a `body` or `url` template field to another node type breaks that
assumption silently: the node-type check is what would then be doing the work, and no test would notice
if it were removed.

**Added beyond the plan.** The plan's tests cover the happy path of redaction; the cases that actually
leak are on the failure paths and the boundaries:

- a 5xx body quoting the credential it just rejected (the node raises before returning output, so the
  *message* is what must not carry it);
- a secret reflected into a response header that is not a known credential name (`x-echo`), which
  header-name redaction does not cover;
- a foreign-nonce marker in the rendered text, which must not make the node resolve a secret at all;
- a missing secret stopping the request from being sent, rather than putting the marker on the wire;
- the idempotency key differing between runs as well as between execution points;
- a secret containing CRLF, against the **real** `GuardedClient`. This one pins an ordering that spans
  two modules: the node substitutes before calling the client, so the true value meets the client's
  RFC 7230 value check and is refused as `EgressBlocked("header")`. Substituting after that check would
  smuggle a second header line onto the wire, and no single-module test would see it. The assertion names
  the category, because an allowlist or DNS refusal is also `HTTP_BLOCKED` and would otherwise let the
  test pass with the header check deleted.

**14 mutants, 12 killed, 2 equivalent as above.**

**One unexplained stall.** The full suite hung once at the start of this task (>500s against a usual 80s)
and could not be reproduced on the next four runs. Both stalls so far followed a pytest run that had been
killed mid-flight, which fits a leftover backend holding a lock that `TRUNCATE` then waits for forever —
unconfirmed, since the evidence is gone by the time it is noticed. `tests/conftest.py` now sets
`lock_timeout = '15s'` before the truncate, so the next occurrence fails loudly with a lock error instead
of hanging the whole suite silently (plan convention 5: bound every wait). If a stall happens again
*without* a lock error, the cause is something else and this note is wrong.
