"""Milestone 9 S1: the desktop worker's HTTP boundary, driven in-process over ASGI.

The credential, the generation fence, the two typed routes, unknown paths, loopback-only
access and timeout poisoning. Fakes stand in for the desktop so this runs on any platform;
`test_desktop_worker_process.py` runs the same boundary as a real subprocess on Windows.
"""

import asyncio
import base64
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.desktop.protocol import (
    WORKER_TOKEN_HEADER,
    DesktopObservation,
    SurfaceListResponse,
    WorkerErrorBody,
    WorkerIdentity,
)
from app.desktop.dpi import PhysicalRect
from app.desktop.registry import AppRegistry
from app.desktop.worker import WorkerSettings, create_worker_app
from tests.desktop_fakes import FakeBackend, FakeCapturePlatform, FakePlatform, FakeProbe, Node, window_tree

TOKEN = "worker-token-with-enough-entropy-1234"
MARKER = "M9_S1_DESKTOP_PRIVATE_MARKER_71A"


class Harness:
    def __init__(self) -> None:
        self.probe = FakeProbe()
        self.probe.add_process(4001, image="editor.exe")
        self.probe.add_window(1, 4001, title="Notes")
        self.backend = FakeBackend()
        self.backend.trees[1] = window_tree("Notes", Node(control_type="Edit", name="Body", value=MARKER))
        self.exits: list[int] = []
        self.timeout = 0.5
        self.platform = FakePlatform(self.probe, self.backend, worker_pid=os.getpid())
        self.capture_platform = FakeCapturePlatform()
        self.capture_platform.add_window(
            1, window_rect=PhysicalRect(0, 0, 820, 620), client_rect=PhysicalRect(8, 39, 812, 612)
        )

    def app(self) -> FastAPI:
        return create_worker_app(
            WorkerSettings(token=SecretStr(TOKEN), observation_timeout_seconds=self.timeout),
            probe_factory=lambda: self.probe,
            backend_factory=lambda: self.backend,
            platform_factory=lambda: self.platform,
            capture_platform_factory=lambda: self.capture_platform,
            registry=AppRegistry(),
            exit_process=lambda code: self.exits.append(code),
        )


@asynccontextmanager
async def running(harness: Harness) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    app = harness.app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1", headers={WORKER_TOKEN_HEADER: TOKEN}
        ) as client:
            yield client, app.state.desktop


async def test_health_reports_the_generation_and_exactly_the_reviewed_operations() -> None:
    async with running(Harness()) as (client, state):
        response = await client.get("/health")
        identity = WorkerIdentity.model_validate(response.json())
        assert identity.worker_generation == state.generation
        assert identity.operations == [
            "surfaces", "observe", "input_baseline", "focus", "scroll", "launch",
            "set_value", "select", "invoke", "capture",
        ]


async def test_no_credential_and_a_wrong_credential_are_refused_on_every_route() -> None:
    async with running(Harness()) as (authorised, state):
        body = {"expected_worker_generation": str(state.generation)}
        transport = authorised._transport  # the same in-process app, without the default credential
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            attempts: list[dict[str, str] | dict[bytes, bytes]] = [
                {}, {WORKER_TOKEN_HEADER: "wrong"}, {WORKER_TOKEN_HEADER: TOKEN + "x"},
                {WORKER_TOKEN_HEADER.encode(): "é".encode("latin-1")},
            ]
            for headers in attempts:
                for method, path in (("GET", "/health"), ("POST", "/v1/desktop/surfaces"), ("POST", "/v1/desktop/observe")):
                    response = await client.request(method, path, json=body if method == "POST" else None, headers=headers)
                    assert response.status_code == 401, (method, path, headers)
                    assert WorkerErrorBody.model_validate(response.json()).code == "unauthenticated"
                    assert TOKEN not in response.text
            # An unknown route is 404 whether or not a credential is presented.
            assert (await client.get("/v1/desktop/execute")).status_code == 404


async def test_the_credential_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import hmac

    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    async with running(Harness()) as (client, _):
        assert (await client.get("/health")).status_code == 200
        client.headers[WORKER_TOKEN_HEADER] = "x" * 36
        assert (await client.get("/health")).status_code == 401
    assert [call[0] for call in calls] == [TOKEN.encode(), b"x" * 36], "every credential check goes through compare_digest"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"), ("GET", "/docs"), ("GET", "/openapi.json"), ("POST", "/v1/desktop/execute"),
        ("POST", "/v1/desktop/action"), ("POST", "/v1/desktop/automation"), ("POST", "/v1/desktop/toggle"),
        ("POST", "/v1/desktop/drag"), ("POST", "/v1/desktop/send-input"), ("POST", "/v1/desktop/mouse"),
        ("POST", "/v1/desktop/keys"), ("POST", "/v1/desktop/run"), ("POST", "/v1/desktop/launch-path"),
        ("POST", "/v1/desktop/click"), ("POST", "/v1/desktop/type"), ("GET", "/v1/desktop/surfaces"),
        ("PUT", "/v1/desktop/observe"), ("POST", "/v1/dispatch"), ("POST", "/v1/sessions/open"),
    ],
)
async def test_unknown_routes_are_404_and_no_action_route_exists(method: str, path: str) -> None:
    async with running(Harness()) as (client, _):
        response = await client.request(method, path, json={})
        assert response.status_code in (404, 405)
        assert response.status_code == 404 or path in ("/v1/desktop/surfaces", "/v1/desktop/observe")


@pytest.mark.parametrize(
    "path", ["/v1/desktop/invoke", "/v1/desktop/set-value", "/v1/desktop/select", "/v1/desktop/capture"]
)
async def test_s4_mutation_routes_exist_and_validate_their_body_but_nothing_else_desktop_shaped_does(path: str) -> None:
    """S4/S5 review and open exactly these four routes (S3's `focus`/`scroll`/`launch` all already
    exist too); an empty body is a validation error (422, the route is real and typed), never a 404."""
    async with running(Harness()) as (client, _):
        response = await client.post(path, json={})
        assert response.status_code == 422


async def test_the_route_table_is_exactly_health_two_reads_and_seven_reviewed_effects() -> None:
    schema = Harness().app().openapi()["paths"]
    assert {(path, tuple(sorted(methods))) for path, methods in schema.items()} == {
        ("/health", ("get",)),
        ("/v1/desktop/surfaces", ("post",)),
        ("/v1/desktop/observe", ("post",)),
        ("/v1/desktop/input-baseline", ("post",)),
        ("/v1/desktop/focus", ("post",)),
        ("/v1/desktop/scroll", ("post",)),
        ("/v1/desktop/launch", ("post",)),
        ("/v1/desktop/set-value", ("post",)),
        ("/v1/desktop/select", ("post",)),
        ("/v1/desktop/invoke", ("post",)),
        ("/v1/desktop/capture", ("post",)),
    }


async def test_capture_round_trips_a_real_encoded_png() -> None:
    async with running(Harness()) as (client, state):
        listing = SurfaceListResponse.model_validate(
            (await client.post("/v1/desktop/surfaces", json={"expected_worker_generation": str(state.generation)})).json()
        )
        surface = listing.surfaces[0]
        baseline = await client.post(
            "/v1/desktop/input-baseline", json={"expected_worker_generation": str(state.generation)}
        )
        tick = baseline.json()["input_tick"]
        response = await client.post(
            "/v1/desktop/capture",
            json={
                "expected_worker_generation": str(state.generation),
                "capture_id": "11111111-1111-1111-1111-111111111111",
                "surface_ref": surface.surface_ref,
                "surface_epoch": surface.surface_epoch,
                "input_tick": tick,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["width"] > 0 and payload["height"] > 0
        image = base64.b64decode(payload["image_base64"])
        assert image[:8] == b"\x89PNG\r\n\x1a\n"


async def test_surfaces_and_observe_round_trip() -> None:
    async with running(Harness()) as (client, state):
        listing = SurfaceListResponse.model_validate(
            (await client.post("/v1/desktop/surfaces", json={"expected_worker_generation": str(state.generation)})).json()
        )
        surface = listing.surfaces[0]
        observation = DesktopObservation.model_validate(
            (
                await client.post(
                    "/v1/desktop/observe",
                    json={
                        "expected_worker_generation": str(state.generation),
                        "surface_ref": surface.surface_ref,
                        "surface_epoch": surface.surface_epoch,
                    },
                )
            ).json()
        )
        assert observation.worker_generation == state.generation
        assert next(n for n in observation.nodes if n.name == "Body").text == MARKER


async def test_a_request_for_another_worker_generation_is_refused() -> None:
    async with running(Harness()) as (client, state):
        for path, extra in (("/v1/desktop/surfaces", {}), ("/v1/desktop/observe", {"surface_ref": "s1", "surface_epoch": 1})):
            response = await client.post(path, json={"expected_worker_generation": "00000000-0000-4000-8000-000000000000", **extra})
            assert response.status_code == 409
            body = WorkerErrorBody.model_validate(response.json())
            assert body.code == "stale_worker_generation" and body.worker_generation == state.generation


@pytest.mark.parametrize(
    "extra",
    [
        {"hwnd": 1}, {"pid": 4001}, {"selector": "Button"}, {"action": "click"}, {"x": 1, "y": 2},
        {"script": "import os"}, {"process_path": "C:\\x.exe"}, {"property": "Name"},
    ],
)
async def test_the_observe_request_accepts_only_a_surface_ref_and_epoch(extra: dict[str, Any]) -> None:
    async with running(Harness()) as (client, state):
        body = {"expected_worker_generation": str(state.generation), "surface_ref": "s1", "surface_epoch": 1, **extra}
        response = await client.post("/v1/desktop/observe", json=body)
        assert response.status_code == 422


async def test_refusals_carry_a_code_and_never_observed_text() -> None:
    harness = Harness()
    harness.backend.trees[1] = window_tree(MARKER, Node(control_type="Edit", name="Password", is_password=True))
    async with running(harness) as (client, state):
        listing = (await client.post("/v1/desktop/surfaces", json={"expected_worker_generation": str(state.generation)})).json()
        surface = listing["surfaces"][0]
        response = await client.post(
            "/v1/desktop/observe",
            json={"expected_worker_generation": str(state.generation), "surface_ref": surface["surface_ref"], "surface_epoch": surface["surface_epoch"]},
        )
        assert response.status_code == 403
        assert WorkerErrorBody.model_validate(response.json()).code == "credential_surface"
        assert MARKER not in response.text


async def test_only_loopback_hosts_and_no_origin_are_accepted() -> None:
    async with running(Harness()) as (client, _):
        assert (await client.get("/health", headers={"host": "evil.example"})).status_code == 400
        assert (await client.get("/health", headers={"host": "127.0.0.1:1", "origin": "http://evil.example"})).status_code == 400
        assert (await client.get("/health", headers={"host": "localhost"})).status_code == 400
        assert (await client.get("/health", headers={"host": "127.0.0.1:8000"})).status_code == 200


async def test_a_hung_provider_times_out_poisons_the_worker_and_schedules_its_own_exit() -> None:
    harness = Harness()
    harness.timeout = 0.3
    async with running(harness) as (client, state):
        listing = (await client.post("/v1/desktop/surfaces", json={"expected_worker_generation": str(state.generation)})).json()
        surface = listing["surfaces"][0]
        # A provider that blocks the UIA thread for far longer than the deadline.
        harness.backend.on_read = lambda _: time.sleep(5)
        started = time.monotonic()
        response = await client.post(
            "/v1/desktop/observe",
            json={"expected_worker_generation": str(state.generation), "surface_ref": surface["surface_ref"], "surface_epoch": surface["surface_epoch"]},
        )
        assert time.monotonic() - started < 2.0, "the request must not wait for the hung provider"
        assert response.status_code == 504
        assert WorkerErrorBody.model_validate(response.json()).code == "desktop_observation_timeout"
        assert state.poisoned is True
        # Poisoned: everything else is refused too, because that generation cannot be trusted.
        again = await client.post("/v1/desktop/surfaces", json={"expected_worker_generation": str(state.generation)})
        assert again.status_code == 504
        await asyncio.sleep(2.3)
        assert harness.exits == [70]
        harness.backend.on_read = None


async def test_diagnostics_hold_only_counts_codes_and_ids(caplog: pytest.LogCaptureFixture) -> None:
    import re

    harness = Harness()
    harness.probe.windows[0].title = f"Title {MARKER}"
    harness.backend.trees[1] = window_tree(f"Title {MARKER}", Node(control_type="Edit", name="Body", value=MARKER))
    caplog.set_level("DEBUG")
    async with running(harness) as (client, state):
        listing = (await client.post("/v1/desktop/surfaces", json={"expected_worker_generation": str(state.generation)})).json()
        surface = listing["surfaces"][0]
        body = {"expected_worker_generation": str(state.generation), "surface_ref": surface["surface_ref"], "surface_epoch": surface["surface_epoch"]}
        assert (await client.post("/v1/desktop/observe", json=body)).status_code == 200
        harness.backend.trees[1] = window_tree("Sign in", Node(control_type="Edit", name="Password", is_password=True))
        refused = await client.post("/v1/desktop/observe", json=body)
        assert refused.status_code == 403 and WorkerErrorBody.model_validate(refused.json()).code == "credential_surface"
    worker_lines = [r.getMessage() for r in caplog.records if r.name.startswith("lumi.desktop")]
    assert any(line.startswith("desktop observation ") for line in worker_lines)
    allowed = {"generation", "surface_count", "truncated", "node_count", "depth", "duration_ms", "error_code", "observation_id"}
    for line in worker_lines:
        keys = set(re.findall(r"(\w+)=", line))
        assert keys <= allowed, (line, keys - allowed)
    joined = "\n".join(worker_lines) + caplog.text
    for private in (MARKER, "Title", "Body", "Notes", "Password", "Sign in", "editor.exe"):
        assert private not in joined, private
