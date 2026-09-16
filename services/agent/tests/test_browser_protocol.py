"""Unit tests for the trust boundary: credentials, generations, classification.

These run against the worker app in-process with a fake browser, and against the
pure classification functions. They cover the decisions that must be right
*before* any real browser is involved -- who may call the worker, whose answers
the runtime will accept, and what each kind of failure is allowed to claim.
"""

import uuid
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.browser.client import BrowserWorkerClient
from app.browser.errors import (
    BrowserWorkerLostResponseError,
    BrowserWorkerRejectedError,
    BrowserWorkerUnavailableError,
    StaleWorkerResultError,
)
from app.browser.protocol import (
    WORKER_TOKEN_HEADER,
    DispatchRequest,
    DispatchResponse,
    OperationStatus,
)
from app.browser.session import WorkerGeneration, generate_worker_token, token_matches
from app.browser.worker import DispatchLedger, DuplicateDispatchError, _classify_failure
from app.domain.action_status import AttemptOutcome
from app.domain.browser_dispatch import DispatchStatus, attempt_outcome_for
from app.services.browser_execution import _outcome_from_error, _reconciliation_verdict, Outcome

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---- the credential ---------------------------------------------------------


def test_a_minted_token_has_real_entropy() -> None:
    first, second = generate_worker_token(), generate_worker_token()
    assert first.get_secret_value() != second.get_secret_value()
    assert len(first.get_secret_value()) >= 32


@pytest.mark.parametrize("presented", [None, "", "wrong", "lumi-", " "])
def test_only_the_exact_token_matches(presented: str | None) -> None:
    expected = SecretStr("a-sufficiently-long-worker-token")
    assert token_matches(presented, expected) is False
    assert token_matches(expected.get_secret_value(), expected) is True


def test_a_secret_token_does_not_render_itself() -> None:
    """A credential that prints itself ends up in a log line eventually."""
    token = generate_worker_token()
    assert token.get_secret_value() not in repr(token)
    assert token.get_secret_value() not in str(token)


def test_the_credential_travels_in_a_header_not_a_url() -> None:
    client = BrowserWorkerClient(
        base_url="http://127.0.0.1:1",
        token=SecretStr("a-sufficiently-long-worker-token"),
        runtime_generation=uuid.uuid4(),
        timeout_seconds=1.0,
    )
    try:
        assert WORKER_TOKEN_HEADER in client._client.headers
    finally:
        pass


# ---- the dispatch ledger ----------------------------------------------------


def _response(dispatch_id: uuid.UUID) -> DispatchResponse:
    return DispatchResponse(
        dispatch_id=dispatch_id,
        runtime_generation=uuid.uuid4(),
        worker_generation=uuid.uuid4(),
        operation="commit_booking",
        status=OperationStatus.OK,
        duration_ms=1,
    )


def test_a_dispatch_in_flight_is_refused_not_repeated() -> None:
    ledger = DispatchLedger()
    dispatch_id = uuid.uuid4()
    assert ledger.claim(dispatch_id) is None
    with pytest.raises(DuplicateDispatchError):
        ledger.claim(dispatch_id)


def test_a_finished_dispatch_replays_instead_of_running_again() -> None:
    ledger = DispatchLedger()
    dispatch_id = uuid.uuid4()
    ledger.claim(dispatch_id)
    ledger.complete(dispatch_id, _response(dispatch_id))
    replayed = ledger.claim(dispatch_id)
    assert replayed is not None
    assert replayed.dispatch_id == dispatch_id


def test_a_released_dispatch_can_be_claimed_again() -> None:
    """A dispatch that crashed before completing does not wedge the worker."""
    ledger = DispatchLedger()
    dispatch_id = uuid.uuid4()
    ledger.claim(dispatch_id)
    ledger.release(dispatch_id)
    assert ledger.claim(dispatch_id) is None


# ---- failure classification -------------------------------------------------


class _Context:
    def __init__(self, submitted: bool) -> None:
        self.submitted = submitted


def test_a_timeout_before_submission_is_a_known_failure() -> None:
    status, code = _classify_failure(_Context(submitted=False), "timeout")  # type: ignore[arg-type]
    assert status is OperationStatus.FAILED_BEFORE_EFFECT
    assert code == "timeout_before_submission"


def test_a_timeout_after_submission_is_never_a_failure() -> None:
    """The single most important classification in the milestone."""
    status, code = _classify_failure(_Context(submitted=True), "timeout")  # type: ignore[arg-type]
    assert status is OperationStatus.OUTCOME_UNKNOWN
    assert code == "timeout_after_submission"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (OperationStatus.OK, AttemptOutcome.SUCCEEDED),
        (OperationStatus.CHANGED_RESOURCE, AttemptOutcome.FAILED),
        (OperationStatus.RESOURCE_UNAVAILABLE, AttemptOutcome.FAILED),
        (OperationStatus.FAILED_BEFORE_EFFECT, AttemptOutcome.FAILED),
        (OperationStatus.OUTCOME_UNKNOWN, AttemptOutcome.OUTCOME_UNKNOWN),
    ],
)
def test_operation_statuses_map_to_the_outcome_they_can_support(
    status: OperationStatus, expected: AttemptOutcome
) -> None:
    assert attempt_outcome_for(status) is expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (BrowserWorkerUnavailableError("refused"), AttemptOutcome.FAILED),
        (BrowserWorkerRejectedError("unknown_operation"), AttemptOutcome.FAILED),
        (BrowserWorkerLostResponseError("ReadTimeout"), AttemptOutcome.OUTCOME_UNKNOWN),
        (StaleWorkerResultError("wrong generation"), AttemptOutcome.OUTCOME_UNKNOWN),
    ],
)
def test_rpc_failures_claim_only_what_they_can_prove(
    error: Exception, expected: AttemptOutcome
) -> None:
    outcome = _outcome_from_error(error)  # type: ignore[arg-type]
    assert outcome.outcome is expected
    assert outcome.submitted is False


# ---- the runtime refuses answers addressed to someone else ------------------


def _client(runtime_generation: uuid.UUID) -> BrowserWorkerClient:
    return BrowserWorkerClient(
        base_url="http://127.0.0.1:1",
        token=SecretStr("a-sufficiently-long-worker-token"),
        runtime_generation=runtime_generation,
        timeout_seconds=1.0,
    )


def _request(worker_generation: uuid.UUID, runtime_generation: uuid.UUID) -> DispatchRequest:
    return DispatchRequest(
        dispatch_id=uuid.uuid4(),
        runtime_generation=runtime_generation,
        expected_worker_generation=worker_generation,
        action_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        operation="commit_booking",
        site="appointment_fixture",
        input={},
    )


@pytest.mark.parametrize(
    "corruption",
    ["runtime_generation", "worker_generation", "dispatch_id", "operation"],
)
async def test_a_result_not_addressed_to_this_runtime_is_discarded(corruption: str) -> None:
    runtime_generation, worker_generation = uuid.uuid4(), uuid.uuid4()
    request = _request(worker_generation, runtime_generation)
    answer: dict[str, Any] = {
        "dispatch_id": str(request.dispatch_id),
        "runtime_generation": str(runtime_generation),
        "worker_generation": str(worker_generation),
        "operation": request.operation,
        "status": "OK",
        "observation": {},
        "error_code": None,
        "duration_ms": 5,
        "submitted": True,
        "replayed": False,
    }
    answer[corruption] = "lookup_booking" if corruption == "operation" else str(uuid.uuid4())

    client = _client(runtime_generation)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=answer)),
        base_url="http://worker.test",
    )
    try:
        with pytest.raises(StaleWorkerResultError):
            await client.dispatch(request)
    finally:
        await client.aclose()


async def test_a_duplicate_dispatch_rejection_is_not_a_failure() -> None:
    """The worker is already doing it. Calling that FAILED would be a guess."""
    runtime_generation, worker_generation = uuid.uuid4(), uuid.uuid4()
    request = _request(worker_generation, runtime_generation)
    client = _client(runtime_generation)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                409,
                json={
                    "code": "duplicate_dispatch",
                    "message": "already running",
                    "worker_generation": str(worker_generation),
                },
            )
        ),
        base_url="http://worker.test",
    )
    try:
        with pytest.raises(BrowserWorkerLostResponseError):
            await client.dispatch(request)
    finally:
        await client.aclose()


async def test_a_refused_operation_is_a_known_failure() -> None:
    runtime_generation, worker_generation = uuid.uuid4(), uuid.uuid4()
    request = _request(worker_generation, runtime_generation)
    client = _client(runtime_generation)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                404, json={"code": "unknown_operation", "message": "no such operation"}
            )
        ),
        base_url="http://worker.test",
    )
    try:
        with pytest.raises(BrowserWorkerRejectedError):
            await client.dispatch(request)
    finally:
        await client.aclose()


# ---- reconciliation verdicts ------------------------------------------------


def _lookup(result: str, **extra: Any) -> Outcome:
    return Outcome(
        outcome=AttemptOutcome.SUCCEEDED,
        dispatch_status=DispatchStatus.OK,
        submitted=False,
        error_code=None,
        observation_id=uuid.uuid4(),
        result={"result": result, **extra},
    )


def test_a_found_booking_resolves_the_action() -> None:
    verdict, evidence = _reconciliation_verdict(
        site="appointment_fixture",
        reference="lumi-1",
        answer=_lookup("FOUND", booking_id="BK-0001", booking_count=1),
    )
    assert verdict is AttemptOutcome.SUCCEEDED
    assert evidence["booking_id"] == "BK-0001"
    assert evidence["source"] == "browser_lookup"


def test_absence_resolves_only_where_the_site_guarantees_it() -> None:
    verdict, evidence = _reconciliation_verdict(
        site="appointment_fixture", reference="lumi-1", answer=_lookup("NOT_FOUND")
    )
    assert verdict is AttemptOutcome.FAILED
    assert evidence["absence_is_authoritative"] is True
    assert evidence["rationale"]


def test_absence_on_an_unreviewed_site_stays_unknown() -> None:
    """The answer for essentially every real website."""
    verdict, evidence = _reconciliation_verdict(
        site="some_real_clinic_website", reference="lumi-1", answer=_lookup("NOT_FOUND")
    )
    assert verdict is AttemptOutcome.OUTCOME_UNKNOWN
    assert evidence["absence_is_authoritative"] is False


def test_an_unanswerable_lookup_never_resolves_anything() -> None:
    verdict, _ = _reconciliation_verdict(
        site="appointment_fixture", reference="lumi-1", answer=_lookup("UNKNOWN")
    )
    assert verdict is AttemptOutcome.OUTCOME_UNKNOWN


def test_failing_to_look_is_not_evidence_of_absence() -> None:
    unreachable = Outcome(
        outcome=AttemptOutcome.OUTCOME_UNKNOWN,
        dispatch_status=DispatchStatus.OUTCOME_UNKNOWN,
        submitted=False,
        error_code="browser_worker_unavailable",
        observation_id=None,
        result={},
    )
    verdict, evidence = _reconciliation_verdict(
        site="appointment_fixture", reference="lumi-1", answer=unreachable
    )
    assert verdict is AttemptOutcome.OUTCOME_UNKNOWN
    assert evidence["lookup"] == "UNKNOWN"


# ---- worker generations -----------------------------------------------------


def test_each_worker_run_has_its_own_identity() -> None:
    assert WorkerGeneration.mint().id != WorkerGeneration.mint().id
