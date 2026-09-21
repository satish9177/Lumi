"""Milestone 9 S2: the disclosure routes over real HTTP and real PostgreSQL.

The desktop worker is the one stand-in (`FakeDesktop`, real persistence of real observations); the
service, repository, routes, error handlers and database are the production ones.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.domain.desktop_disclosure import ALLOWED_FIELDS
from app.main import create_app
from app.services.desktop import DesktopService
from app.services.desktop_disclosure import DesktopDisclosureService
from app.services.runtime import RuntimeGeneration
from tests.conftest import running_app
from tests.desktop_disclosure_support import SECRET_DIGITS, SECRET_EMAIL, VISIBLE, FakeDesktop

OBJECTIVE = "What is failing in this window?"
TITLE = "Editor - notes.txt"
PRIVATE = (SECRET_EMAIL, SECRET_DIGITS, TITLE, VISIBLE, OBJECTIVE)

FORBIDDEN_VERBS = ("focus", "invoke", "type", "select", "scroll", "click", "launch", "execute", "input", "key", "mouse")

GROUNDED = {
    "schema_version": 1,
    "kind": "answer",
    "answer": "3 failing tests.",
    "evidence": [{"control_ref": "u2", "quote": "3 failing tests"}],
}


@pytest.fixture
def quiet_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"public_inspection_hosts": "", "inspection_test_origins": ""})


@pytest.fixture
async def desktop_app(
    quiet_settings: Settings, engine: AsyncEngine, runtime_generation: RuntimeGeneration
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeDesktop]]:
    app = create_app(quiet_settings)
    async with running_app(app) as client:
        fake = FakeDesktop(engine, runtime_generation.id)
        app.state.desktop_disclosure_service = DesktopDisclosureService(
            engine, desktop=cast(DesktopService, fake), grant_ttl_seconds=600
        )
        yield client, fake


def create_body(fake: FakeDesktop, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "objective": OBJECTIVE,
        "recipient": "openai",
        "model": "gpt-test",
        "worker_generation": str(fake.worker_generation),
        "surface_ref": "s1",
        "surface_epoch": 1,
    }
    body.update(overrides)
    return body


async def open_and_grant(client: httpx.AsyncClient, fake: FakeDesktop) -> tuple[str, dict[str, Any]]:
    created = await client.post("/desktop/read-tasks", json=create_body(fake))
    assert created.status_code == 201, created.text
    read = created.json()
    granted = await client.post(
        f"/desktop/read-tasks/{read['task_id']}/grant",
        json={"grant_id": read["card"]["grant_id"], "expected_revision": read["card"]["grant_revision"]},
    )
    assert granted.status_code == 200, granted.text
    return read["task_id"], granted.json()


# ---- the route surface ----------------------------------------------------------------------------------


def test_the_runtime_exposes_exactly_these_desktop_routes_and_no_verb(quiet_settings: Settings) -> None:
    paths = create_app(quiet_settings).openapi()["paths"]
    # S3's action routes are pinned, exactly, by `test_desktop_runtime`; this pins the S1/S2 read/disclosure set.
    desktop = {
        path: sorted(methods) for path, methods in paths.items() if "desktop" in path and not path.startswith("/desktop/actions")
    }
    assert desktop == {
        "/desktop/surfaces": ["get"],
        "/desktop/observations": ["post"],
        "/desktop/read-tasks": ["post"],
        "/desktop/read-tasks/latest": ["get"],
        "/desktop/read-tasks/{task_id}": ["get"],
        "/desktop/read-tasks/{task_id}/grant": ["post"],
        "/desktop/read-tasks/{task_id}/revoke": ["post"],
        "/desktop/read-tasks/{task_id}/disclosure": ["post"],
        "/desktop/read-tasks/{task_id}/result": ["post"],
    }


def test_no_route_anywhere_names_a_desktop_action(quiet_settings: Settings) -> None:
    app = create_app(quiet_settings)
    for route in app.routes:
        path = str(getattr(route, "path", "")).lower()
        if "desktop" not in path:
            continue
        segments = [segment for segment in path.split("/") if segment and not segment.startswith("{")]
        assert not any(verb == segment for verb in FORBIDDEN_VERBS for segment in segments), path


@pytest.mark.parametrize(
    "extra",
    [{"hwnd": 5}, {"pid": 5}, {"snapshot": {"nodes": []}}, {"digest": "a" * 64}, {"snapshot_digest": "a" * 64},
     {"process_path": "C:\\a.exe"}, {"selector": "//Button"}, {"action": "click"}, {"x": 1, "y": 1}],
)
async def test_a_create_request_accepts_a_surface_a_question_and_nothing_else(
    desktop_app: tuple[httpx.AsyncClient, FakeDesktop], extra: dict[str, Any]
) -> None:
    client, fake = desktop_app
    response = await client.post("/desktop/read-tasks", json=create_body(fake, **extra))
    assert response.status_code == 422 and fake.observe_calls == 0


@pytest.mark.parametrize(
    "body",
    [
        {"objective": "", "recipient": "openai", "model": "m", "surface_ref": "s1", "surface_epoch": 1},
        {"recipient": "not-a-provider"},
        {"model": "bad model!"},
        {"surface_ref": "s17"},
        {"surface_epoch": 0},
        {"objective": "x" * 501},
    ],
)
async def test_a_malformed_create_request_is_a_validation_error_and_reads_nothing(
    desktop_app: tuple[httpx.AsyncClient, FakeDesktop], body: dict[str, Any]
) -> None:
    client, fake = desktop_app
    response = await client.post("/desktop/read-tasks", json=create_body(fake, **body))
    assert response.status_code == 422 and fake.observe_calls == 0


# ---- capability disabled ----------------------------------------------------------------------------------------


async def test_with_the_capability_disabled_nothing_is_created(
    quiet_settings: Settings, engine: AsyncEngine
) -> None:
    async with running_app(create_app(quiet_settings)) as client:
        response = await client.post(
            "/desktop/read-tasks",
            json={
                "objective": OBJECTIVE, "recipient": "openai", "model": "gpt-test",
                "worker_generation": str(uuid.uuid4()), "surface_ref": "s1", "surface_epoch": 1,
            },
        )
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "desktop_refused" and error["reason"] == "desktop_automation_disabled"
        assert OBJECTIVE not in response.text
        for table in ("tasks", "task_grants", "desktop_disclosures", "desktop_observations"):
            async with engine.connect() as connection:
                assert (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar() == 0
        assert (await client.get("/desktop/read-tasks/latest")).json() == {"read": None}


# ---- the whole flow --------------------------------------------------------------------------------------------------


async def test_the_whole_flow_over_http(desktop_app: tuple[httpx.AsyncClient, FakeDesktop]) -> None:
    client, fake = desktop_app
    assert (await client.get("/desktop/read-tasks/latest")).json() == {"read": None}

    created = await client.post("/desktop/read-tasks", json=create_body(fake))
    assert created.status_code == 201
    read = created.json()
    assert read["phase"] == "awaiting_approval" and read["card"]["recipient"] == "openai"
    assert read["card"]["window_title"] == TITLE and read["disclosure"] is None and read["answer"] is None
    for hidden in ("snapshot", "snapshot_digest", "hwnd", "pid", "worker_generation", "surface_ref"):
        assert hidden not in read["card"]

    latest = (await client.get("/desktop/read-tasks/latest")).json()["read"]
    assert latest["task_id"] == read["task_id"]

    task_id = read["task_id"]
    granted_response = await client.post(
        f"/desktop/read-tasks/{task_id}/grant",
        json={"grant_id": read["card"]["grant_id"], "expected_revision": read["card"]["grant_revision"]},
    )
    assert granted_response.json()["phase"] == "approved"

    claim = await client.post(f"/desktop/read-tasks/{task_id}/disclosure")
    assert claim.status_code == 200
    body = claim.json()
    assert set(body) == {"disclosure_id", "task_id", "objective", "recipient", "model", "projection"}
    projection = body["projection"]
    assert set(projection) == {
        "schema_version", "classification", "trust", "observed_at", "truncated", "truncation", "node_count", "nodes",
    }
    assert projection["classification"] == "desktop_private" and projection["trust"] == "untrusted_environment"
    for projected in projection["nodes"]:
        assert set(projected) <= set(ALLOWED_FIELDS)
    assert VISIBLE in claim.text and SECRET_EMAIL not in claim.text and SECRET_DIGITS not in claim.text
    assert TITLE not in claim.text

    second = await client.post(f"/desktop/read-tasks/{task_id}/disclosure")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "desktop_disclosure_state_changed"
    assert second.json()["error"]["reason"] == "grant_not_active"

    recorded = await client.post(
        f"/desktop/read-tasks/{task_id}/result",
        json={"disclosure_id": body["disclosure_id"], "result": GROUNDED},
    )
    assert recorded.status_code == 200
    done = recorded.json()
    assert done["phase"] == "answered" and done["task_status"] == "SUCCEEDED"
    assert done["answer"]["answer"] == "3 failing tests." and done["answer"]["evidence"][0]["control_ref"] == "u2"
    assert done["disclosure"]["status"] == "SUCCEEDED"

    again = await client.post(
        f"/desktop/read-tasks/{task_id}/result",
        json={"disclosure_id": body["disclosure_id"], "result": GROUNDED},
    )
    assert again.status_code == 409 and again.json()["error"]["reason"] == "disclosure_already_recorded"


async def test_errors_carry_a_code_and_reason_and_no_desktop_text(
    desktop_app: tuple[httpx.AsyncClient, FakeDesktop]
) -> None:
    client, fake = desktop_app
    created = (await client.post("/desktop/read-tasks", json=create_body(fake))).json()
    task_id, card = created["task_id"], created["card"]

    early = await client.post(f"/desktop/read-tasks/{task_id}/disclosure")
    assert early.status_code == 409
    wrong_revision = await client.post(
        f"/desktop/read-tasks/{task_id}/grant",
        json={"grant_id": card["grant_id"], "expected_revision": card["grant_revision"] + 5},
    )
    assert wrong_revision.status_code == 409 and wrong_revision.json()["error"]["reason"] == "grant_changed"
    unknown = await client.post(
        f"/desktop/read-tasks/{task_id}/grant",
        json={"grant_id": str(uuid.uuid4()), "expected_revision": 1},
    )
    assert unknown.json()["error"]["reason"] == "grant_not_found"
    missing = await client.get(f"/desktop/read-tasks/{uuid.uuid4()}")
    assert missing.status_code == 404

    for response in (early, wrong_revision, unknown, missing):
        error = response.json()["error"]
        assert error["code"] and error["message"]
        for private in PRIVATE:
            assert private not in response.text

    granted = (await client.post(
        f"/desktop/read-tasks/{task_id}/grant",
        json={"grant_id": card["grant_id"], "expected_revision": card["grant_revision"]},
    )).json()
    assert granted["phase"] == "approved"
    disclosure = (await client.post(f"/desktop/read-tasks/{task_id}/disclosure")).json()
    malformed = await client.post(
        f"/desktop/read-tasks/{task_id}/result", json={"disclosure_id": disclosure["disclosure_id"]}
    )
    assert malformed.status_code == 422
    assert malformed.json()["error"] == {
        "code": "desktop_disclosure_refused", "message": "That desktop request was refused.", "reason": "result_malformed",
    }
    bad_failure = await client.post(
        f"/desktop/read-tasks/{task_id}/result",
        json={"disclosure_id": disclosure["disclosure_id"], "failure": "answer_not_grounded"},
    )
    assert bad_failure.status_code == 422 and bad_failure.json()["error"]["code"] == "invalid_request"


async def test_an_action_shaped_result_is_recorded_as_a_failed_attempt_over_http(
    desktop_app: tuple[httpx.AsyncClient, FakeDesktop]
) -> None:
    client, fake = desktop_app
    task_id, _ = await open_and_grant(client, fake)
    disclosure = (await client.post(f"/desktop/read-tasks/{task_id}/disclosure")).json()
    response = await client.post(
        f"/desktop/read-tasks/{task_id}/result",
        json={"disclosure_id": disclosure["disclosure_id"], "result": {**GROUNDED, "operation": "invoke", "tool": "desktop"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["phase"] == "failed" and body["answer"] is None
    assert body["disclosure"]["error_code"] == "invalid_output"


async def test_the_disclosure_routes_require_the_runtime_credential(
    quiet_settings: Settings, engine: AsyncEngine
) -> None:
    async with running_app(create_app(quiet_settings)) as authorised:
        anonymous = httpx.AsyncClient(transport=authorised._transport, base_url="http://127.0.0.1")  # noqa: SLF001
        async with anonymous:
            task = uuid.uuid4()
            assert (await anonymous.get("/desktop/read-tasks/latest")).status_code == 401
            assert (await anonymous.post(f"/desktop/read-tasks/{task}/disclosure")).status_code == 401
            assert (await anonymous.post(f"/desktop/read-tasks/{task}/result", json={})).status_code == 401
