"""The two network layers of the S6 freeze, at the components that enforce them.

No browser here: these speak the proxy protocol to a real broker listener and drive the
Playwright guard with a stand-in route, so each assertion is about **what a server
received or what a request was allowed to do**, never about what Lumi reported.

* **Layer 2, the broker.** A CONNECT tunnel that was already open when the freeze began
  must not be able to carry another byte. `freeze()` alone cannot promise that, which is
  why `freeze_and_drain()` exists; this proves it with a server that counts bytes.
* **Layer 1, the guard.** A frozen guard refuses before it fetches, resolves or follows
  anything, and its in-flight count is exact: a request is refused at the door or it is
  counted, never neither, and `freeze()` waits (bounded) for the counted ones.
"""

import asyncio
import base64
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from app.browser.account_read_guard import AccountReadNetworkGuard
from app.browser.egress_broker import BrokerMode, EgressBroker
from app.domain.public_url import BROKER_POLICY_VERSION, PublicUrlPolicy


class Sink:
    """A server that records every byte it is ever sent, on every connection."""

    def __init__(self) -> None:
        self.received = b""
        self.connections = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while data := await reader.read(1_024):
                self.received += data
                writer.write(b"ack:" + data)
                await writer.drain()
        except (OSError, ConnectionError):  # pragma: no cover - teardown noise
            pass
        finally:
            writer.close()

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()


class CountingResolver:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, host: str) -> list[str]:
        self.calls += 1
        raise OSError("no such host")


async def open_tunnel(broker: EgressBroker, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", broker.port)
    token = base64.b64encode(f"{broker.credential.username}:{broker.credential.password}".encode()).decode()
    writer.write(
        f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode()
    )
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    assert head.startswith(b"HTTP/1.1 200"), head
    return reader, writer


@pytest.fixture
async def rig() -> Any:
    sink = Sink()
    await sink.start()
    resolver = CountingResolver()
    broker = EgressBroker(
        PublicUrlPolicy(version=BROKER_POLICY_VERSION, allow_any_public_host=True),
        configured_origins=frozenset({f"http://127.0.0.1:{sink.port}"}),
        resolver=resolver,
    )
    await broker.start()
    try:
        yield broker, sink, resolver
    finally:
        await broker.aclose()
        await sink.aclose()


async def test_an_already_open_tunnel_cannot_carry_another_byte_after_freeze(rig: Any) -> None:
    broker, sink, resolver = rig
    reader, writer = await open_tunnel(broker, sink.port)
    writer.write(b"before-freeze")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(64), timeout=5) == b"ack:before-freeze"
    assert broker.active_connections == 1 and sink.connections == 1
    resolutions, dials = broker.counters.resolutions, broker.counters.dial_count

    active = await broker.freeze_and_drain(timeout_seconds=3.0)

    assert active == 0 and broker.active_connections == 0 and broker.mode is BrokerMode.FROZEN
    # The keep-alive relay is gone: whatever the page sends next reaches nobody.
    try:
        writer.write(b"after-freeze-1")
        await writer.drain()
        await asyncio.sleep(0.3)
        writer.write(b"after-freeze-2")
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    await asyncio.sleep(0.5)
    assert sink.received == b"before-freeze"  # not one new byte at the server
    try:
        tail = await asyncio.wait_for(reader.read(64), timeout=5)
    except (ConnectionError, OSError):
        tail = b""  # a reset is the same fact: the tunnel is gone
    assert tail == b""
    assert broker.counters.resolutions == resolutions == 0 and resolver.calls == 0
    assert broker.counters.dial_count == dials
    writer.close()


async def test_freeze_alone_would_have_left_the_tunnel_open(rig: Any) -> None:
    """Why `freeze_and_drain` exists: the primitive that only flips the mode is not enough."""
    broker, sink, _ = rig
    reader, writer = await open_tunnel(broker, sink.port)
    assert broker.freeze() == 1  # it reports the open relay, and leaves it there
    writer.write(b"still-flows")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(64), timeout=5) == b"ack:still-flows"
    assert sink.received == b"still-flows"
    writer.close()


async def test_a_new_connection_while_frozen_is_refused_before_anything_is_registered(rig: Any) -> None:
    broker, sink, resolver = rig
    assert await broker.freeze_and_drain(timeout_seconds=2.0) == 0
    reader, writer = await asyncio.open_connection("127.0.0.1", broker.port)
    token = base64.b64encode(f"{broker.credential.username}:{broker.credential.password}".encode()).decode()
    writer.write(
        f"CONNECT secret-value.exfil.invalid:443 HTTP/1.1\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode()
    )
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(512), timeout=5)
    assert b"403" in answer and b"frozen" in answer
    assert resolver.calls == 0 and broker.counters.resolutions == 0 and broker.counters.dial_count == 0
    assert broker.active_connections == 0 and sink.connections == 0
    writer.close()


async def test_thaw_restores_the_open_state(rig: Any) -> None:
    broker, sink, _ = rig
    await broker.freeze_and_drain(timeout_seconds=2.0)
    broker.thaw()
    reader, writer = await open_tunnel(broker, sink.port)
    writer.write(b"open-again")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(64), timeout=5) == b"ack:open-again"
    writer.close()


# ---- the Playwright guard ------------------------------------------------------------------------


class FakeRequest:
    resource_type = "fetch"
    method = "GET"
    url = "https://example.com/data"
    frame = object()

    def is_navigation_request(self) -> bool:
        return False


class FakeRoute:
    """A route whose upstream fetch stays open until a test releases it."""

    def __init__(self) -> None:
        self.request = FakeRequest()
        self.fetched = False
        self.aborted: list[str] = []
        self.gate = asyncio.Event()

    async def fetch(self, **_: Any) -> Any:
        self.fetched = True
        await self.gate.wait()
        raise PlaywrightError("upstream ended")

    async def abort(self, code: str) -> None:
        self.aborted.append(code)

    async def fulfill(self, **_: Any) -> None:  # pragma: no cover - the fake always fails
        raise AssertionError("not reached")


def make_guard() -> AccountReadNetworkGuard:
    return AccountReadNetworkGuard(site="example.com")


async def test_the_guard_counts_a_request_from_the_moment_it_enters() -> None:
    guard = make_guard()
    route = FakeRoute()
    task = asyncio.ensure_future(guard._handle(route))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    assert guard.in_flight == 1 and route.fetched
    route.gate.set()
    await task
    assert guard.in_flight == 0


async def test_a_frozen_guard_refuses_before_it_fetches_and_counts_nothing() -> None:
    guard = make_guard()
    assert await guard.freeze(settle_timeout_seconds=1.0) is True
    route = FakeRoute()
    await guard._handle(route)  # type: ignore[arg-type]
    assert route.fetched is False and route.aborted == ["blockedbyclient"]
    assert guard.in_flight == 0 and guard.blocked["frozen"] == 1


async def test_freeze_waits_for_a_request_already_inside_and_reports_when_it_never_ends() -> None:
    guard = make_guard()
    inside = FakeRoute()
    task = asyncio.ensure_future(guard._handle(inside))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)

    assert await guard.freeze(settle_timeout_seconds=0.3) is False  # it never settles
    assert guard.frozen and guard.in_flight == 1  # and it stays frozen: no quiet re-open

    late = FakeRoute()
    await guard._handle(late)  # type: ignore[arg-type]
    assert late.fetched is False  # nothing new got in behind it

    inside.gate.set()
    await task
    assert guard.in_flight == 0
    assert await guard.freeze(settle_timeout_seconds=0.3) is True


async def test_a_request_racing_the_freeze_is_either_refused_or_counted() -> None:
    guard = make_guard()
    routes = [FakeRoute() for _ in range(20)]
    tasks = [asyncio.ensure_future(guard._handle(route)) for route in routes]  # type: ignore[arg-type]
    freezing = asyncio.ensure_future(guard.freeze(settle_timeout_seconds=1.0))
    await asyncio.sleep(0.05)
    for route in routes:
        route.gate.set()
    await asyncio.gather(*tasks, freezing)
    assert guard.in_flight == 0
    # Every route was either fetched (entered before the flag) or aborted (refused after it).
    assert all(route.fetched or route.aborted == ["blockedbyclient"] for route in routes)


async def test_thaw_lets_requests_in_again() -> None:
    guard = make_guard()
    await guard.freeze(settle_timeout_seconds=0.5)
    guard.thaw()
    route = FakeRoute()
    task = asyncio.ensure_future(guard._handle(route))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    assert route.fetched and guard.in_flight == 1
    route.gate.set()
    await task
