"""Browser integration: a real Chromium, driving a real site, over a real ledger.

Nothing is mocked here. Each test starts the fixture site and the browser worker
as separate processes, drives the full lifecycle through the runtime's services,
and then checks the *site's* authoritative counters -- not the page, and not the
worker's own report -- for how many bookings exist and how many submissions were
issued.

The assertion that appears in almost every test is the same triple:

    exactly one execution attempt
    exactly one browser submission (or zero)
    exactly one booking (or zero)

A test that only checked the action's status would pass for an agent that booked
twice and noticed once.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser.protocol import WORKER_TOKEN_HEADER
from app.domain.action_status import ActionStatus, AttemptOutcome, RiskTier
from app.domain.booking import booking_reference
from app.domain.browser_dispatch import BrowserEffect, DispatchStatus
from app.domain.errors import InvalidActionTransitionError
from app.domain.task_status import TaskStatus
from app.repositories.browser import BrowserRepository
from app.services.actions import ActionService, ActionView
from app.services.browser_execution import BrowserExecutionService, BrowserWorkerConfig
from app.services.runtime import RuntimeGeneration
from app.services.tasks import TaskService
from tests.browser_harness import (
    Process,
    SiteControl,
    WorkerProcess,
    booking_proposal,
    browser_worker,
    fixture_site,
)

pytestmark = pytest.mark.browser

SLOT_A = booking_proposal("slot-a-1830", "Dr A", "2026-09-19T18:30:00+05:30", 800)


# ---- processes, shared across the module ------------------------------------


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Process]:
    logs = tmp_path_factory.mktemp("browser")
    with fixture_site(logs / "site.log") as process:
        yield process


@pytest.fixture(scope="module")
def worker(site: Process, tmp_path_factory: pytest.TempPathFactory) -> Iterator[WorkerProcess]:
    logs = tmp_path_factory.mktemp("browser")
    with browser_worker(logs / "worker.log", site_origin=site.base_url) as process:
        yield process


@pytest.fixture
def control(site: Process) -> SiteControl:
    site_control = SiteControl(site.base_url)
    site_control.reset()
    return site_control


@pytest.fixture
def browser_service(
    engine: AsyncEngine,
    action_service: ActionService,
    runtime_generation: RuntimeGeneration,
    worker: WorkerProcess,
) -> BrowserExecutionService:
    return BrowserExecutionService(
        engine,
        actions=action_service,
        runtime_generation=runtime_generation.id,
        worker=BrowserWorkerConfig(
            base_url=worker.base_url, token=worker.token, timeout_seconds=60.0
        ),
    )


# ---- driving the ledger -----------------------------------------------------


async def approve(
    action_service: ActionService,
    task_service: TaskService,
    proposal: dict[str, Any],
    *,
    idempotency_key: str = "booking-001",
) -> ActionView:
    task = await task_service.create_task({"type": "appointment_booking"})
    view, _ = await action_service.propose_action(
        task.id,
        idempotency_key=idempotency_key,
        tool_name="commit_booking",
        risk_tier=RiskTier.R2,
        proposal=proposal,
    )
    view = await action_service.request_approval(view.action.id)
    return await action_service.approve_action(view.action.id)


async def dispatch_rows(engine: AsyncEngine, action_id: uuid.UUID) -> list[Any]:
    async with engine.connect() as connection:
        return await BrowserRepository(connection).list_dispatches(action_id)


def counts(control: SiteControl, reference: str) -> tuple[int, int]:
    """(bookings, submissions) as the *site* knows them."""
    state = control.state()
    return state["booking_count"], state["submissions_by_reference"].get(reference, 0)


# ---- 1. the happy path ------------------------------------------------------


async def test_an_approved_booking_is_made_exactly_once_and_verified(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    approved = await approve(action_service, task_service, SLOT_A)
    action_id = approved.action.id
    reference = booking_reference(action_id)

    final = await browser_service.execute_booking(action_id)

    assert final.action.status is ActionStatus.SUCCEEDED
    assert (await task_service.get_task(approved.action.task_id)).status is TaskStatus.READY
    assert len(final.attempts) == 1
    attempt = final.attempts[0]
    assert attempt.outcome is AttemptOutcome.SUCCEEDED
    # A verified postcondition, not merely a click that returned.
    assert attempt.result is not None
    assert attempt.result["postcondition_verified"] is True
    assert attempt.result["booking_id"] == "BK-0001"
    assert attempt.result["submitted"] is True

    assert counts(control, reference) == (1, 1)

    # One consequential dispatch, bound to that one attempt.
    rows = await dispatch_rows(engine, action_id)
    assert len(rows) == 1
    assert rows[0].attempt_id == attempt.id
    assert rows[0].effect is BrowserEffect.CONSEQUENTIAL
    assert rows[0].status is DispatchStatus.OK
    assert rows[0].submitted is True


async def test_the_persisted_result_holds_no_page_markup(
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    """The ledger records what happened, never a copy of the site."""
    approved = await approve(action_service, task_service, SLOT_A)
    final = await browser_service.execute_booking(approved.action.id)
    stored = str(final.attempts[0].result)
    for markup in ("<html", "<body", "<form", "<!doctype", "</p>"):
        assert markup not in stored.lower()


# ---- 2. a price that changed after approval ---------------------------------


async def test_a_changed_price_blocks_the_booking_entirely(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    """The approval authorises Rs 800. It cannot be spent on Rs 950."""
    approved = await approve(action_service, task_service, SLOT_A)
    action_id = approved.action.id
    reference = booking_reference(action_id)
    control.set_faults(price_overrides={"slot-a-1830": 950})

    final = await browser_service.execute_booking(action_id)

    assert final.action.status is ActionStatus.FAILED
    attempt = final.attempts[0]
    assert attempt.outcome is AttemptOutcome.FAILED
    assert attempt.error_code == "approved_values_changed"
    assert attempt.result is not None
    assert attempt.result["changed_facts"] == [
        {"field": "price", "approved": "800", "observed": "950"}
    ]
    assert attempt.result["submitted"] is False

    # Zero side effects: not booked, and never even submitted.
    assert counts(control, reference) == (0, 0)
    rows = await dispatch_rows(engine, action_id)
    assert rows[0].status is DispatchStatus.CHANGED_RESOURCE
    assert rows[0].submitted is False


async def test_the_consumed_approval_cannot_authorise_the_new_price(
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    """A changed resource does not leave a reusable approval lying around."""
    approved = await approve(action_service, task_service, SLOT_A)
    control.set_faults(price_overrides={"slot-a-1830": 950})
    final = await browser_service.execute_booking(approved.action.id)

    assert final.approval is None  # Consumed by the attempt that refused.
    with pytest.raises(InvalidActionTransitionError):
        await browser_service.execute_booking(approved.action.id)
    assert counts(control, booking_reference(approved.action.id)) == (0, 0)


# ---- 3. a slot that disappeared ---------------------------------------------


async def test_a_disappeared_slot_is_a_known_failure_with_no_side_effect(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    approved = await approve(action_service, task_service, SLOT_A)
    action_id = approved.action.id
    control.set_faults(removed_slots=["slot-a-1830"])

    final = await browser_service.execute_booking(action_id)

    assert final.action.status is ActionStatus.FAILED
    assert final.attempts[0].error_code == "slot_unavailable"
    assert counts(control, booking_reference(action_id)) == (0, 0)
    rows = await dispatch_rows(engine, action_id)
    assert rows[0].status is DispatchStatus.RESOURCE_UNAVAILABLE
    assert rows[0].submitted is False


# ---- 4. a page that changed under the worker --------------------------------


async def test_a_page_that_changes_between_preparation_and_commit_is_caught(
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
    worker: WorkerProcess,
) -> None:
    """Re-observation, not a cached value, is what decides.

    The fixture serves the first slot-page view at the approved price and every
    later view at a different one. `prepare_booking` takes the first view, so
    `commit_booking` sees the changed page -- exactly the situation a stale
    element handle or a value cached during preparation would sail straight
    through.
    """
    import httpx

    approved = await approve(action_service, task_service, SLOT_A)
    action_id = approved.action.id
    control.set_faults(stale_page_reloads=1)

    # Preparation observes the page while it still matches.
    prepared = httpx.post(
        f"{worker.base_url}/v1/dispatch",
        headers={WORKER_TOKEN_HEADER: worker.token.get_secret_value()},
        json={
            "dispatch_id": str(uuid.uuid4()),
            "runtime_generation": str(uuid.uuid4()),
            "expected_worker_generation": worker.identity()["worker_generation"],
            "action_id": str(action_id),
            "attempt_id": None,
            "operation": "prepare_booking",
            "site": "appointment_fixture",
            "input": {"slot_id": "slot-a-1830", "reference": booking_reference(action_id)},
        },
        timeout=60,
    ).json()
    assert prepared["observation"]["slot"]["price"] == 800

    final = await browser_service.execute_booking(action_id)

    assert final.action.status is ActionStatus.FAILED
    assert final.attempts[0].error_code == "approved_values_changed"
    assert counts(control, booking_reference(action_id)) == (0, 0)


# ---- 5. hostile page content ------------------------------------------------


async def test_page_text_telling_lumi_to_ignore_its_rules_has_no_authority(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    """The page says to book something else, at a price nobody approved.

    Nothing about the outcome may change. The booking that happens is the
    approved one, at the approved price, and the injected instruction does not
    appear anywhere in the ledger -- it is content on a page Lumi read, not
    input to any decision Lumi made.
    """
    approved = await approve(action_service, task_service, SLOT_A)
    action_id = approved.action.id
    reference = booking_reference(action_id)
    control.set_faults(hostile_text=True)

    final = await browser_service.execute_booking(action_id)

    assert final.action.status is ActionStatus.SUCCEEDED
    # The approved booking, and only the approved booking.
    state = control.state()
    assert state["booking_count"] == 1
    booking = state["bookings"][0]
    assert booking["slot_id"] == "slot-a-1830"
    assert booking["price"] == 800
    assert booking["reference"] == reference
    assert state["submissions_by_reference"] == {reference: 1}

    # The proposal was never rewritten, and the approval chain is unchanged.
    assert final.action.proposal == SLOT_A
    assert final.action.proposal_digest == approved.action.proposal_digest
    assert len(final.attempts) == 1

    # And the injected text is nowhere in what Lumi persisted.
    persisted = str(final.attempts[0].result) + str(await dispatch_rows(engine, action_id))
    assert "IGNORE YOUR PREVIOUS INSTRUCTIONS" not in persisted
    assert "9500" not in persisted


async def test_hostile_text_on_the_confirmation_page_does_not_change_the_verdict(
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    """The fixture renders the injection on the receipt page too."""
    approved = await approve(action_service, task_service, SLOT_A)
    control.set_faults(hostile_text=True)
    final = await browser_service.execute_booking(approved.action.id)
    assert final.attempts[0].result is not None
    assert final.attempts[0].result["receipt"]["price"] == 800


# ---- 6. duplicate dispatch --------------------------------------------------


async def test_a_second_execution_of_one_approved_action_is_refused(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    """The ledger refuses before a second browser is ever asked for."""
    approved = await approve(action_service, task_service, SLOT_A)
    action_id = approved.action.id
    reference = booking_reference(action_id)

    first = await browser_service.execute_booking(action_id)
    assert first.action.status is ActionStatus.SUCCEEDED

    with pytest.raises(InvalidActionTransitionError):
        await browser_service.execute_booking(action_id)

    assert counts(control, reference) == (1, 1)
    assert len((await action_service.get_action(action_id)).attempts) == 1
    rows = await dispatch_rows(engine, action_id)
    assert len([row for row in rows if row.effect is BrowserEffect.CONSEQUENTIAL]) == 1


async def test_concurrent_executions_of_one_action_produce_one_booking(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    """Two callers race for one approval. One wins; the site sees one submission."""
    import asyncio

    approved = await approve(action_service, task_service, SLOT_A)
    action_id = approved.action.id
    reference = booking_reference(action_id)

    results = await asyncio.gather(
        browser_service.execute_booking(action_id),
        browser_service.execute_booking(action_id),
        return_exceptions=True,
    )

    succeeded = [r for r in results if isinstance(r, ActionView)]
    assert len(succeeded) == 1, results
    assert succeeded[0].action.status is ActionStatus.SUCCEEDED
    assert counts(control, reference) == (1, 1)
    assert len((await action_service.get_action(action_id)).attempts) == 1


async def test_the_worker_replays_a_repeated_dispatch_id_instead_of_re_booking(
    worker: WorkerProcess, control: SiteControl
) -> None:
    """The worker's own defence, tested directly at the RPC boundary.

    The ledger already makes a duplicate dispatch unreachable from the runtime.
    This checks the worker does not rely on that: given the same dispatch id
    twice, it replays rather than driving a second browser.
    """
    import httpx

    identity = worker.identity()
    dispatch_id = str(uuid.uuid4())
    action_id = str(uuid.uuid4())
    reference = f"lumi-replay-{uuid.uuid4()}"
    body = {
        "dispatch_id": dispatch_id,
        "runtime_generation": str(uuid.uuid4()),
        "expected_worker_generation": identity["worker_generation"],
        "action_id": action_id,
        "attempt_id": str(uuid.uuid4()),
        "operation": "commit_booking",
        "site": "appointment_fixture",
        "input": {
            "reference": reference,
            "proposal": {**SLOT_A, "slot_id": "slot-b-1915", "doctor": "Dr B", "price": 950,
                         "time": "2026-09-19T19:15:00+05:30"},
        },
    }
    headers = {WORKER_TOKEN_HEADER: worker.token.get_secret_value()}

    first = httpx.post(f"{worker.base_url}/v1/dispatch", json=body, headers=headers, timeout=60)
    second = httpx.post(f"{worker.base_url}/v1/dispatch", json=body, headers=headers, timeout=60)

    assert first.json()["status"] == "OK"
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert second.json()["observation"] == first.json()["observation"]

    state = control.state()
    assert state["submissions_by_reference"][reference] == 1
    assert len([b for b in state["bookings"] if b["reference"] == reference]) == 1


# ---- 7. the worker's own trust boundary -------------------------------------


@pytest.fixture
def worker_headers(worker: WorkerProcess) -> dict[str, str]:
    return {WORKER_TOKEN_HEADER: worker.token.get_secret_value()}


def _dispatch_body(worker: WorkerProcess, **overrides: Any) -> dict[str, Any]:
    return {
        "dispatch_id": str(uuid.uuid4()),
        "runtime_generation": str(uuid.uuid4()),
        "expected_worker_generation": worker.identity()["worker_generation"],
        "action_id": str(uuid.uuid4()),
        "attempt_id": str(uuid.uuid4()),
        "operation": "read_available_slots",
        "site": "appointment_fixture",
        "input": {"slot_id": "slot-a-1830"},
        **overrides,
    }


@pytest.mark.parametrize("header", [{}, {WORKER_TOKEN_HEADER: "guess"}])
def test_the_worker_refuses_a_request_without_the_credential(
    worker: WorkerProcess, header: dict[str, str]
) -> None:
    import httpx

    response = httpx.post(
        f"{worker.base_url}/v1/dispatch", json=_dispatch_body(worker), headers=header, timeout=30
    )
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"
    # It does not reveal which worker it is to a caller it did not authenticate.
    assert response.json()["worker_generation"] is None


def test_health_is_authenticated_too(worker: WorkerProcess) -> None:
    import httpx

    assert httpx.get(f"{worker.base_url}/health", timeout=30).status_code == 401


def test_the_worker_refuses_an_operation_outside_the_registry(
    worker: WorkerProcess, worker_headers: dict[str, str]
) -> None:
    import httpx

    response = httpx.post(
        f"{worker.base_url}/v1/dispatch",
        json=_dispatch_body(worker, operation="evaluate", input={}),
        headers=worker_headers,
        timeout=30,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_operation"


def test_the_worker_refuses_a_site_outside_its_allowlist(
    worker: WorkerProcess, worker_headers: dict[str, str]
) -> None:
    """The anti-SSRF boundary: a proposal cannot choose where the browser goes."""
    import httpx

    response = httpx.post(
        f"{worker.base_url}/v1/dispatch",
        json=_dispatch_body(worker, site="evil_site"),
        headers=worker_headers,
        timeout=30,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "site_not_allowed"


def test_the_worker_refuses_a_dispatch_for_another_worker_generation(
    worker: WorkerProcess, worker_headers: dict[str, str]
) -> None:
    import httpx

    response = httpx.post(
        f"{worker.base_url}/v1/dispatch",
        json=_dispatch_body(worker, expected_worker_generation=str(uuid.uuid4())),
        headers=worker_headers,
        timeout=30,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "stale_worker_generation"


def test_a_consequential_operation_without_an_attempt_is_refused(
    worker: WorkerProcess, worker_headers: dict[str, str], control: SiteControl
) -> None:
    """Consequential work happens only on behalf of a persisted attempt."""
    import httpx

    response = httpx.post(
        f"{worker.base_url}/v1/dispatch",
        json=_dispatch_body(
            worker,
            operation="commit_booking",
            attempt_id=None,
            input={"reference": "lumi-no-attempt", "proposal": SLOT_A},
        ),
        headers=worker_headers,
        timeout=30,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "attempt_required"
    assert control.state()["submissions"] == 0


def test_the_worker_reads_the_credential_from_the_documented_header(
    worker: WorkerProcess,
) -> None:
    """A rename on either side must not quietly turn authentication off."""
    import httpx

    schema = httpx.get(f"{worker.base_url}/openapi.json", timeout=30).json()
    for path in ("/health", "/v1/dispatch"):
        for method in schema["paths"][path].values():
            names = {parameter["name"] for parameter in method.get("parameters", [])}
            assert WORKER_TOKEN_HEADER in names


def test_the_worker_exposes_no_generic_automation_endpoints(worker: WorkerProcess) -> None:
    """There is no evaluate/javascript/click-anything surface to find."""
    import httpx

    schema = httpx.get(f"{worker.base_url}/openapi.json", timeout=30).json()
    assert sorted(schema["paths"]) == ["/health", "/v1/dispatch"]


def test_the_worker_refuses_input_that_does_not_match_the_operation(
    worker: WorkerProcess, worker_headers: dict[str, str]
) -> None:
    import httpx

    response = httpx.post(
        f"{worker.base_url}/v1/dispatch",
        json=_dispatch_body(worker, input={"slot_id": "slot-a-1830", "script": "alert(1)"}),
        headers=worker_headers,
        timeout=30,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_operation_input"


# ---- 8. reconciliation on a healthy path ------------------------------------


async def test_reconciliation_finds_a_booking_without_making_another(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    approved = await approve(action_service, task_service, SLOT_A)
    action_id = approved.action.id
    reference = booking_reference(action_id)

    # Reach OUTCOME_UNKNOWN the honest way: the site creates the booking and
    # never answers, so the worker times out after submitting.
    control.set_faults(drop_response_for_reference=reference, drop_response_mode="abort")
    executed = await browser_service.execute_booking(action_id)

    assert executed.action.status is ActionStatus.OUTCOME_UNKNOWN
    assert executed.attempts[0].outcome is AttemptOutcome.OUTCOME_UNKNOWN
    assert counts(control, reference) == (1, 1)

    control.set_faults()  # The site answers again.
    reconciled = await browser_service.reconcile_booking(action_id)

    assert reconciled.action.status is ActionStatus.SUCCEEDED
    assert (await task_service.get_task(approved.action.task_id)).status is TaskStatus.READY
    # The lookup did not book anything, and did not create an attempt.
    assert counts(control, reference) == (1, 1)
    assert len(reconciled.attempts) == 1

    rows = await dispatch_rows(engine, action_id)
    assert [row.operation for row in rows] == ["commit_booking", "lookup_booking"]
    lookup = rows[1]
    assert lookup.effect is BrowserEffect.READ_ONLY
    assert lookup.attempt_id is None  # A lookup is not an execution attempt.
    assert lookup.submitted is False


async def test_reconciliation_reports_a_genuine_absence_as_a_known_failure(
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    """The mirror image of the acceptance test: lost response, nothing created.

    The site drops the response *before* creating anything. From the browser the
    two cases are indistinguishable -- a submission went out and no answer came
    back -- which is exactly why the worker must report OUTCOME_UNKNOWN for both
    and let an authoritative lookup decide. Here the lookup finds nothing, and
    because this fixture guarantees that absence means absence, the action is
    resolved as a genuine FAILED.
    """
    approved = await approve(action_service, task_service, SLOT_A)
    action_id = approved.action.id
    reference = booking_reference(action_id)

    control.set_faults(
        drop_response_before_commit_for_reference=reference, drop_response_mode="abort"
    )
    executed = await browser_service.execute_booking(action_id)

    assert executed.action.status is ActionStatus.OUTCOME_UNKNOWN
    assert executed.attempts[0].result is not None
    assert executed.attempts[0].result["submitted"] is True
    # A submission was issued, and it created nothing.
    assert counts(control, reference) == (0, 1)

    control.set_faults()
    reconciled = await browser_service.reconcile_booking(action_id)

    assert reconciled.action.status is ActionStatus.FAILED
    assert (await task_service.get_task(approved.action.task_id)).status is TaskStatus.READY
    assert len(reconciled.attempts) == 1
    assert counts(control, reference) == (0, 1)


async def test_a_site_that_definitively_refuses_is_a_known_failure(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    """A refusal page after the click is a negative acknowledgement, not silence.

    `submitted` is still recorded as true -- a button really was pressed -- but
    the outcome is knowable because the site said so itself.
    """
    approved = await approve(action_service, task_service, SLOT_A)
    action_id = approved.action.id
    reference = booking_reference(action_id)
    control.set_faults(reject_submissions=True)

    final = await browser_service.execute_booking(action_id)

    assert final.action.status is ActionStatus.FAILED
    assert final.attempts[0].error_code == "rejected_by_site"
    assert final.attempts[0].result is not None
    assert final.attempts[0].result["submitted"] is True
    assert counts(control, reference) == (0, 1)
    rows = await dispatch_rows(engine, action_id)
    assert rows[0].status is DispatchStatus.RESOURCE_UNAVAILABLE
    assert rows[0].submitted is True


async def test_an_unanswerable_lookup_leaves_the_action_unknown(
    action_service: ActionService,
    task_service: TaskService,
    browser_service: BrowserExecutionService,
    control: SiteControl,
) -> None:
    """Failing to look is never downgraded into a failure."""
    approved = await approve(action_service, task_service, SLOT_A)
    action_id = approved.action.id
    reference = booking_reference(action_id)

    control.set_faults(drop_response_for_reference=reference, drop_response_mode="abort")
    executed = await browser_service.execute_booking(action_id)
    assert executed.action.status is ActionStatus.OUTCOME_UNKNOWN

    control.set_faults(lookup_unavailable=True)
    reconciled = await browser_service.reconcile_booking(action_id)

    assert reconciled.action.status is ActionStatus.OUTCOME_UNKNOWN
    assert (
        await task_service.get_task(approved.action.task_id)
    ).status is TaskStatus.OUTCOME_UNKNOWN
    # Still one booking, still one submission, still one attempt.
    assert counts(control, reference) == (1, 1)
    assert len(reconciled.attempts) == 1

    # And it can still be resolved later, once the site can answer.
    control.set_faults()
    resolved = await browser_service.reconcile_booking(action_id)
    assert resolved.action.status is ActionStatus.SUCCEEDED
    assert counts(control, reference) == (1, 1)


# ---- 9. the worker dying ----------------------------------------------------


async def test_a_worker_that_is_not_running_fails_before_any_approval_is_spent(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    runtime_generation: RuntimeGeneration,
    control: SiteControl,
) -> None:
    """Connection refused means the dispatch never left. Nothing happened.

    Because the handshake happens before the approval is claimed, the approval
    is still there afterwards and the action is still executable -- no attempt
    was started, so there is nothing to reconcile.
    """
    from app.browser.errors import BrowserWorkerUnavailableError
    from tests.browser_harness import free_port

    approved = await approve(action_service, task_service, SLOT_A)
    service = BrowserExecutionService(
        engine,
        actions=action_service,
        runtime_generation=runtime_generation.id,
        worker=BrowserWorkerConfig(
            base_url=f"http://127.0.0.1:{free_port()}",
            token=SecretStr("a-sufficiently-long-worker-token"),
            timeout_seconds=5.0,
        ),
    )

    with pytest.raises(BrowserWorkerUnavailableError):
        await service.execute_booking(approved.action.id)

    current = await action_service.get_action(approved.action.id)
    assert current.action.status is ActionStatus.APPROVED
    assert current.attempts == ()
    assert current.approval is not None
    assert control.state()["submissions"] == 0


async def test_a_wrong_credential_is_refused_before_any_approval_is_spent(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    runtime_generation: RuntimeGeneration,
    worker: WorkerProcess,
    control: SiteControl,
) -> None:
    from app.browser.errors import BrowserWorkerRejectedError

    approved = await approve(action_service, task_service, SLOT_A)
    service = BrowserExecutionService(
        engine,
        actions=action_service,
        runtime_generation=runtime_generation.id,
        worker=BrowserWorkerConfig(
            base_url=worker.base_url,
            token=SecretStr("a-sufficiently-long-wrong-token"),
            timeout_seconds=30.0,
        ),
    )

    with pytest.raises(BrowserWorkerRejectedError):
        await service.execute_booking(approved.action.id)

    current = await action_service.get_action(approved.action.id)
    assert current.action.status is ActionStatus.APPROVED
    assert current.attempts == ()
    assert control.state()["submissions"] == 0
