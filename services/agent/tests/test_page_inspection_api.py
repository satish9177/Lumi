"""Page inspection over the runtime API, without a browser.

The browser half is proven in test_public_page_worker.py and
test_page_inspection_ledger.py. Here the observation a worker would have
returned is stored through the same `finish_attempt` evidence hook the executor
uses, so the ledger rules can be exercised quickly and deterministically:
destination refusals, one card per exact proposal, grounding, stale
observations, idempotent answers and the one-answer trigger.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.config import Settings
from app.domain.action_status import AttemptOutcome
from app.domain.browser_dispatch import BrowserEffect
from app.domain.page_observation import (
    INSPECT_PUBLIC_PAGE,
    PUBLIC_WEB_SITE,
    PageObservation,
    TextBlock,
    compute_content_hash,
)
from app.main import create_app
from app.repositories.actions import AttemptRecord
from app.repositories.browser import BrowserRepository
from app.repositories.observations import ObservationRepository
from tests.conftest import TEST_RUNTIME_TOKEN, running_app

TEST_ORIGIN = "http://127.0.0.1:8811"
PAGE = f"{TEST_ORIGIN}/profiles/rated"
QUESTION = "What is my contest rating?"
DISCLOSURE = {"recipients": ["scripted"], "max_text_chars": 12_000}
BLOCKS = ["lumi_fixture_coder", "Contest rating", "1,842", "Global rank", "12,345", "Problems solved", "367"]


@dataclass
class Runtime:
    app: FastAPI
    client: httpx.AsyncClient


@pytest.fixture
async def runtime(migrated_database_url: str, engine: AsyncEngine) -> AsyncIterator[Runtime]:
    settings = Settings(
        database_url=SecretStr(migrated_database_url),
        runtime_token=TEST_RUNTIME_TOKEN,
        public_inspection_hosts="github.com",
        inspection_test_origins=TEST_ORIGIN,
    )
    app = create_app(settings)
    async with running_app(app) as client:
        yield Runtime(app, client)


def _ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code in (200, 201), response.text
    body: dict[str, Any] = response.json()
    return body


async def _task(client: httpx.AsyncClient, url: str = PAGE, question: str = QUESTION) -> httpx.Response:
    return await client.post(
        "/tasks",
        json={"request": {"type": "page_inspection", "text": f"{url} {question}", "url": url, "question": question,
                          "source": "text", "request_id": "req_test0001"}},
    )


async def _card(client: httpx.AsyncClient, url: str = PAGE) -> dict[str, Any]:
    task = _ok(await _task(client, url))
    return _ok(await client.post(f"/tasks/{task['id']}/inspection/prepare", json={"disclosure": DISCLOSURE}))


def _observation(blocks: list[str], url: str = PAGE) -> PageObservation:
    text_blocks = [TextBlock(id=f"b{index + 1}", text=value) for index, value in enumerate(blocks)]
    return PageObservation(
        observation_id=uuid.uuid4(),
        requested_url=url,
        final_url=url,
        title="Profile",
        document_epoch=1,
        settled=True,
        observed_at=datetime.now(UTC),
        blocks=text_blocks,
        links=[],
        truncated=False,
        total_text_chars=sum(len(value) for value in blocks),
        total_link_count=0,
        content_hash=compute_content_hash(final_url=url, title="Profile", blocks=text_blocks, links=[]),
    )


async def _observed(runtime: Runtime, blocks: list[str] = BLOCKS) -> tuple[dict[str, Any], PageObservation]:
    """Approve, claim, and store an observation exactly as the executor would."""
    client = runtime.client
    card = await _card(client)
    approved = _ok(await client.post(f"/actions/{card['id']}/approve", json={"expected_revision": card["revision"]}))
    executing = _ok(await client.post(f"/actions/{card['id']}/attempts", json={"expected_revision": approved["revision"]}))
    observation = _observation(blocks)
    engine: AsyncEngine = runtime.app.state.engine
    worker, dispatch = uuid.uuid4(), uuid.uuid4()
    attempt_id = uuid.UUID(executing["attempts"][0]["id"])
    async with engine.begin() as connection:
        repository = BrowserRepository(connection)
        await repository.register_worker_generation(
            worker_generation=worker,
            runtime_generation=runtime.app.state.runtime_generation.id,
            worker_started_at=datetime.now(UTC),
        )
        await repository.insert_dispatch(
            dispatch_id=dispatch, action_id=uuid.UUID(card["id"]), attempt_id=attempt_id,
            worker_generation=worker, operation=INSPECT_PUBLIC_PAGE, site=PUBLIC_WEB_SITE,
            effect=BrowserEffect.READ_ONLY,
        )

    async def store(connection: AsyncConnection, finished: AttemptRecord) -> None:
        await ObservationRepository(connection).insert(
            observation=observation, task_id=uuid.UUID(card["task_id"]), action_id=uuid.UUID(card["id"]),
            attempt_id=finished.id, dispatch_id=dispatch, worker_generation=worker,
        )

    await runtime.app.state.action_service.finish_attempt(
        uuid.UUID(card["id"]), outcome=AttemptOutcome.SUCCEEDED,
        result={"observation_id": str(observation.observation_id)}, record=store,
    )
    return _ok(await client.get(f"/actions/{card['id']}/inspection")), observation


def _answer(observation: PageObservation, **answer: Any) -> dict[str, Any]:
    return {
        "observation_id": str(observation.observation_id),
        "content_hash": observation.content_hash,
        "answer": answer,
        "provider": "scripted",
        "model": "scripted-rules",
    }


# ---- task creation: the destination is checked before anything is stored ---------


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///C:/Windows/win.ini", "scheme_not_allowed"),
        ("javascript:alert(1)", "scheme_not_allowed"),
        ("https://localhost/", "local_host"),
        ("https://169.254.169.254/latest/meta-data/", "ip_literal"),
        ("http://127.0.0.1:9999/", "https_required"),
        ("https://user:pw@github.com/", "credentials_in_url"),
        ("https://leetcode.com/u/someone/", "destination_not_allowed"),
        ("HTTPS://github.com/x", "not_canonical"),
        ("https://github.com/x#frag", "not_canonical"),
    ],
)
async def test_a_refused_destination_never_becomes_a_task(runtime: Runtime, url: str, reason: str) -> None:
    response = await _task(runtime.client, url)
    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "destination_not_allowed",
        "message": "That page is not an allowed inspection destination.",
        "reason": reason,
    }
    engine: AsyncEngine = runtime.app.state.engine
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM tasks")) == 0


@pytest.mark.parametrize(
    "request_body",
    [
        {"type": "page_inspection", "url": PAGE},
        {"type": "page_inspection", "url": PAGE, "question": ""},
        {"type": "page_inspection", "url": PAGE, "question": "x" * 501},
        {"type": "page_inspection", "url": PAGE, "question": "rating", "selector": "#rating"},
        {"type": "page_inspection", "url": PAGE, "question": "rating", "script": "alert(1)"},
        {"type": "page_inspection", "question": "rating"},
    ],
)
async def test_malformed_inspection_requests_are_refused(runtime: Runtime, request_body: dict[str, Any]) -> None:
    response = await runtime.client.post("/tasks", json={"request": request_body})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_an_unconfigured_runtime_offers_no_inspection(client: httpx.AsyncClient) -> None:
    response = await _task(client)
    assert response.status_code == 422
    assert response.json()["error"]["reason"] == "https_required"


# ---- the approval card ---------------------------------------------------------------


async def test_prepare_binds_url_host_question_and_disclosure_into_one_exact_approval(runtime: Runtime) -> None:
    card = await _card(runtime.client)
    assert card["tool_name"] == "inspect_public_page"
    assert card["status"] == "WAITING_APPROVAL"
    assert card["risk_tier"] == "R1"
    assert card["proposal"] == {
        "schema_version": 1,
        "operation": "inspect_public_page",
        "effect": "public_read",
        "url": PAGE,
        "host": "127.0.0.1",
        "question": QUESTION,
        "policy_version": "public-url-v1",
        "limits": {"max_blocks": 200, "max_text_chars": 12_000, "max_links": 20, "max_redirects": 5},
        "disclosure": DISCLOSURE,
    }
    approval = card["approval"]
    assert approval["status"] == "PENDING"
    assert approval["proposal_digest"] == card["proposal_digest"]
    assert approval["action_revision"] == card["revision"]


async def test_a_duplicate_request_shows_the_same_card_and_a_different_one_is_refused(runtime: Runtime) -> None:
    client = runtime.client
    task = _ok(await _task(client))
    first = _ok(await client.post(f"/tasks/{task['id']}/inspection/prepare", json={"disclosure": DISCLOSURE}))
    again = _ok(await client.post(f"/tasks/{task['id']}/inspection/prepare", json={"disclosure": DISCLOSURE}))
    assert again["id"] == first["id"] and again["revision"] == first["revision"]
    different = await client.post(
        f"/tasks/{task['id']}/inspection/prepare",
        json={"disclosure": {"recipients": ["gemini"], "max_text_chars": 12_000}},
    )
    assert different.status_code == 409
    assert different.json()["error"]["code"] == "action_already_open"
    actions = _ok(await client.get(f"/tasks/{task['id']}/actions"))["actions"]
    assert len(actions) == 1


async def test_the_disclosure_cannot_name_an_unknown_recipient_or_smuggle_fields(runtime: Runtime) -> None:
    task = _ok(await _task(runtime.client))
    for body in (
        {"disclosure": {"recipients": ["evil"], "max_text_chars": 100}},
        {"disclosure": {"recipients": [], "max_text_chars": 100}},
        {"disclosure": {"recipients": ["gemini", "gemini"], "max_text_chars": 100}},
        {"disclosure": DISCLOSURE, "url": "https://github.com/"},
        {"disclosure": DISCLOSURE, "approve": True},
    ):
        response = await runtime.client.post(f"/tasks/{task['id']}/inspection/prepare", json=body)
        assert response.status_code == 422, body


async def test_a_destination_withdrawn_after_approval_consumes_nothing(
    migrated_database_url: str, engine: AsyncEngine
) -> None:
    configured = Settings(
        database_url=SecretStr(migrated_database_url), runtime_token=TEST_RUNTIME_TOKEN,
        inspection_test_origins=TEST_ORIGIN,
    )
    async with running_app(create_app(configured)) as client:
        card = await _card(client)
        approved = _ok(await client.post(f"/actions/{card['id']}/approve", json={"expected_revision": card["revision"]}))

    # The trusted configuration changes before the approved inspection runs.
    narrowed = Settings(
        database_url=SecretStr(migrated_database_url), runtime_token=TEST_RUNTIME_TOKEN,
        public_inspection_hosts="github.com",
    )
    async with running_app(create_app(narrowed)) as client:
        response = await client.post(
            f"/actions/{card['id']}/browser-execution", json={"expected_revision": approved["revision"]}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "destination_not_allowed"
        action = _ok(await client.get(f"/actions/{card['id']}"))
    assert action["status"] == "APPROVED" and action["approval"]["status"] == "APPROVED"
    assert action["attempts"] == []


async def test_an_inspection_cannot_be_reconciled_into_a_verdict(runtime: Runtime) -> None:
    card = await _card(runtime.client)
    response = await runtime.client.post(f"/actions/{card['id']}/browser-reconciliation")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "browser_execution_not_supported"


# ---- answers ---------------------------------------------------------------------------


async def test_a_grounded_answer_is_recorded_once_and_announced_without_page_text(runtime: Runtime) -> None:
    view, observation = await _observed(runtime)
    assert view["observation"]["content_hash"] == observation.content_hash
    assert view["observation"]["provenance"] == "untrusted_environment"
    assert view["answer"] is None
    action_id = view["action"]["id"]
    body = _answer(observation, status="answered", answer="Your contest rating is 1,842.",
                   evidence=[{"block": "b2", "quote": "Contest rating"}, {"block": "b3", "quote": "1,842"}])
    recorded = _ok(await runtime.client.post(f"/actions/{action_id}/inspection/answer", json=body))
    assert recorded["answer"]["status"] == "answered"
    assert recorded["answer"]["answer"] == "Your contest rating is 1,842."

    # A second, different answer does not replace the first.
    other = _answer(observation, status="not_found", answer="Could not verify this from the inspected page.", evidence=[])
    again = _ok(await runtime.client.post(f"/actions/{action_id}/inspection/answer", json=other))
    assert again["answer"] == recorded["answer"]

    events = _ok(await runtime.client.get(f"/tasks/{view['action']['task_id']}/events"))["events"]
    answered = [event for event in events if event["event_type"] == "task.page_answer_recorded"]
    assert len(answered) == 1
    assert answered[0]["payload"]["answer_status"] == "answered"
    assert "1,842" not in str(answered[0]["payload"])


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ({"status": "answered", "answer": "Your contest rating is 1,842.", "evidence": []}, "no_evidence"),
        ({"status": "answered", "answer": "Rating 1,842", "evidence": [{"block": "b9", "quote": "1,842"}]}, "unknown_block"),
        ({"status": "answered", "answer": "Rating 1,842", "evidence": [{"block": "b2", "quote": "Contest rating: 1,842"}]}, "quote_not_in_block"),
        # The rank's number, cited under the rating label: the number is not in the quote.
        ({"status": "answered", "answer": "Your contest rating is 12,345.", "evidence": [{"block": "b2", "quote": "Contest rating"}]}, "number_not_in_evidence"),
        ({"status": "answered", "answer": "Your contest rating is 9999.", "evidence": [{"block": "b3", "quote": "1,842"}]}, "number_not_in_evidence"),
    ],
)
async def test_an_ungrounded_answer_is_refused(runtime: Runtime, answer: dict[str, Any], reason: str) -> None:
    view, observation = await _observed(runtime)
    response = await runtime.client.post(
        f"/actions/{view['action']['id']}/inspection/answer", json=_answer(observation, **answer)
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "answer_not_grounded"
    assert response.json()["error"]["reason"] == reason
    assert _ok(await runtime.client.get(f"/actions/{view['action']['id']}/inspection"))["answer"] is None


async def test_answer_payloads_are_closed(runtime: Runtime) -> None:
    view, observation = await _observed(runtime)
    url = f"/actions/{view['action']['id']}/inspection/answer"
    good = _answer(observation, status="not_found", answer="Could not verify this from the inspected page.", evidence=[])
    for body in (
        {**good, "provider": "somewhere"},
        {**good, "answer": {**good["answer"], "next_action": {"operation": "inspect_public_page"}}},
        {**good, "answer": {**good["answer"], "status": "approved"}},
        {**good, "answer": {**good["answer"], "answer": "x" * 601}},
        {**good, "approve": True},
    ):
        assert (await runtime.client.post(url, json=body)).status_code == 422, body


async def test_an_answer_for_a_changed_or_superseded_observation_is_stale(runtime: Runtime) -> None:
    view, observation = await _observed(runtime)
    action_id = view["action"]["id"]
    body = _answer(observation, status="not_found", answer="Could not verify this from the inspected page.", evidence=[])
    wrong_hash = {**body, "content_hash": "0" * 64}
    response = await runtime.client.post(f"/actions/{action_id}/inspection/answer", json=wrong_hash)
    assert (response.status_code, response.json()["error"]["code"]) == (409, "stale_observation")

    wrong_observation = {**body, "observation_id": str(uuid.uuid4())}
    response = await runtime.client.post(f"/actions/{action_id}/inspection/answer", json=wrong_observation)
    assert (response.status_code, response.json()["error"]["code"]) == (409, "observation_not_available")
    response = await runtime.client.post(f"/actions/{action_id}/inspection/answer", json=body)
    assert response.status_code == 200


async def test_a_repeat_on_the_same_task_is_a_new_action_and_the_old_answer_goes_stale(runtime: Runtime) -> None:
    client = runtime.client
    view, observation = await _observed(runtime)
    task_id = view["action"]["task_id"]
    # A second exact approval is required: the first is consumed.
    again = _ok(await client.post(f"/tasks/{task_id}/inspection/prepare", json={"disclosure": DISCLOSURE}))
    assert again["id"] != view["action"]["id"]
    assert again["status"] == "WAITING_APPROVAL" and again["approval"]["status"] == "PENDING"
    first = _ok(await client.get(f"/actions/{view['action']['id']}"))
    assert first["attempts"][0]["approval_id"] != again["approval"]["id"]

    approved = _ok(await client.post(f"/actions/{again['id']}/approve", json={"expected_revision": again["revision"]}))
    await client.post(f"/actions/{again['id']}/attempts", json={"expected_revision": approved["revision"]})
    newer = _observation(BLOCKS)
    engine: AsyncEngine = runtime.app.state.engine
    worker, dispatch = uuid.uuid4(), uuid.uuid4()
    executing = _ok(await client.get(f"/actions/{again['id']}"))
    async with engine.begin() as connection:
        repository = BrowserRepository(connection)
        await repository.register_worker_generation(
            worker_generation=worker, runtime_generation=runtime.app.state.runtime_generation.id,
            worker_started_at=datetime.now(UTC),
        )
        await repository.insert_dispatch(
            dispatch_id=dispatch, action_id=uuid.UUID(again["id"]),
            attempt_id=uuid.UUID(executing["attempts"][0]["id"]), worker_generation=worker,
            operation=INSPECT_PUBLIC_PAGE, site=PUBLIC_WEB_SITE, effect=BrowserEffect.READ_ONLY,
        )

    async def store(connection: AsyncConnection, finished: AttemptRecord) -> None:
        await ObservationRepository(connection).insert(
            observation=newer, task_id=uuid.UUID(task_id), action_id=uuid.UUID(again["id"]),
            attempt_id=finished.id, dispatch_id=dispatch, worker_generation=worker,
        )

    await runtime.app.state.action_service.finish_attempt(
        uuid.UUID(again["id"]), outcome=AttemptOutcome.SUCCEEDED, result={}, record=store
    )
    stale = await client.post(
        f"/actions/{view['action']['id']}/inspection/answer",
        json=_answer(observation, status="not_found", answer="Could not verify this from the inspected page.", evidence=[]),
    )
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "stale_observation")
    fresh = await client.post(
        f"/actions/{again['id']}/inspection/answer",
        json=_answer(newer, status="answered", answer="1,842", evidence=[{"block": "b3", "quote": "1,842"}]),
    )
    assert fresh.status_code == 200


async def test_stored_evidence_is_immutable_and_answered_once_in_the_database(runtime: Runtime) -> None:
    view, observation = await _observed(runtime)
    engine: AsyncEngine = runtime.app.state.engine
    with pytest.raises(DBAPIError, match="immutable evidence"):
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE page_observations SET projection = '{}'::jsonb WHERE id = :id"),
                {"id": observation.observation_id},
            )
    await runtime.client.post(
        f"/actions/{view['action']['id']}/inspection/answer",
        json=_answer(observation, status="answered", answer="1,842", evidence=[{"block": "b3", "quote": "1,842"}]),
    )
    with pytest.raises(DBAPIError, match="already has a recorded answer"):
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE page_observations SET answer_model = 'other' WHERE id = :id"),
                {"id": observation.observation_id},
            )
