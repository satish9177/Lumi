"""The runtime contract the Electron main process depends on.

Python and TypeScript must not be able to silently disagree about the desktop
bridge, so the contract is generated from the real Pydantic models and enums,
written to `src/shared/agent-runtime-contract.json`, and tested from both sides:

* `tests/test_desktop_contract.py` fails if the committed file is stale;
* `src/main/services/agent-wire.test.ts` checks the TypeScript enums and wire
  parsers against the same file, including every example below.

Examples are built through the response models, so each one is a payload the
runtime can actually emit. Regenerate with:

    uv run python -m app.api.contract --write
"""

import argparse
import json
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.api.schemas import (
    ActionListResponse,
    ActionResponse,
    ApprovalResponse,
    AttemptResponse,
    BookingSearchResponse,
    BookingSlotResponse,
    CancelBookingTaskResponse,
    ClinicInfoResponse,
    DoctorProfileResponse,
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    PrepareBookingBody,
    ReviseBookingCriteriaBody,
    ReviseBookingCriteriaResponse,
    TaskEventListResponse,
    TaskEventResponse,
    TaskResponse,
)
from app.config import AGENT_ROOT
from app.domain.action_status import ActionStatus, ApprovalStatus, AttemptOutcome, RiskTier
from app.domain.booking import CHANGED_FACT_FIELDS, BookingProposal
from app.domain.booking_criteria import BookingCriteria
from app.domain.browser_dispatch import DispatchStatus, LookupStatus
from app.domain.digest import proposal_digest
from app.domain.task_status import TaskEventType, TaskStatus

CONTRACT_PATH = AGENT_ROOT.parents[1] / "src" / "shared" / "agent-runtime-contract.json"

#: Every `error.code` the runtime can return. `tests/test_desktop_contract.py`
#: scans the error handlers so a new code cannot be added without listing it.
ERROR_CODES = (
    "action_already_open",
    "action_not_found",
    "action_proposal_conflict",
    "approval_not_usable",
    "authentication_required",
    "booking_criteria_mismatch",
    "booking_slot_unavailable",
    "browser_execution_not_supported",
    "browser_observation_failed",
    "browser_worker_not_configured",
    "browser_worker_unavailable",
    "concurrent_modification",
    "invalid_action_transition",
    "invalid_booking_criteria",
    "invalid_booking_proposal",
    "invalid_host",
    "invalid_request",
    "no_unfinished_attempt",
    "origin_not_allowed",
    "stale_action_revision",
    "stale_revision",
    "task_already_booked",
    "task_has_unresolved_action",
    "task_kind_mismatch",
    "task_not_accepting_actions",
    "task_not_cancellable",
    "task_not_found",
)

_MODELS: tuple[type[BaseModel], ...] = (
    ActionListResponse,
    ActionResponse,
    BookingCriteria,
    BookingProposal,
    BookingSearchResponse,
    CancelBookingTaskResponse,
    ClinicInfoResponse,
    ErrorResponse,
    HealthResponse,
    PrepareBookingBody,
    ReviseBookingCriteriaBody,
    ReviseBookingCriteriaResponse,
    TaskEventListResponse,
    TaskResponse,
)

_T0 = datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC)
_TASK = uuid.UUID("00000000-0000-4000-8000-000000000001")
_ACTION = uuid.UUID("00000000-0000-4000-8000-000000000002")
_APPROVAL = uuid.UUID("00000000-0000-4000-8000-000000000003")
_ATTEMPT = uuid.UUID("00000000-0000-4000-8000-000000000004")
_GENERATION = uuid.UUID("00000000-0000-4000-8000-000000000005")
_WORKER = uuid.UUID("00000000-0000-4000-8000-000000000006")
_DISPATCH = uuid.UUID("00000000-0000-4000-8000-000000000007")
_OBSERVATION = uuid.UUID("00000000-0000-4000-8000-000000000008")


def _enum(members: type[StrEnum]) -> list[str]:
    return [member.value for member in members]


def _at(seconds: int) -> datetime:
    return _T0 + timedelta(seconds=seconds)


def _proposal() -> dict[str, Any]:
    return BookingProposal.model_validate(
        {
            "site": "appointment_fixture",
            "slot_id": "slot-a-1830",
            "doctor": "Dr A",
            "time": "2026-09-19T18:30:00+05:30",
            "price": 800,
            "currency": "INR",
        }
    ).model_dump(mode="json")


def _action(
    status: ActionStatus,
    revision: int,
    *,
    approval: ApprovalResponse | None = None,
    attempts: list[AttemptResponse] | None = None,
) -> dict[str, Any]:
    proposal = _proposal()
    return ActionResponse(
        id=_ACTION,
        task_id=_TASK,
        idempotency_key="commit_booking-1",
        tool_name="commit_booking",
        risk_tier=RiskTier.R2,
        proposal=proposal,
        proposal_digest=proposal_digest(proposal),
        status=status,
        revision=revision,
        created_at=_at(1),
        updated_at=_at(revision),
        approval=approval,
        attempts=attempts or [],
    ).model_dump(mode="json")


def _attempt(outcome: AttemptOutcome | None, result: dict[str, Any] | None, error: str | None) -> AttemptResponse:
    return AttemptResponse(
        id=_ATTEMPT,
        action_id=_ACTION,
        attempt_number=1,
        approval_id=_APPROVAL,
        runtime_generation=_GENERATION,
        started_at=_at(4),
        finished_at=_at(5) if outcome is not None else None,
        outcome=outcome,
        result=result,
        error_code=error,
    )


def _dispatch_summary(status: DispatchStatus, **extra: Any) -> dict[str, Any]:
    return {
        "operation": "commit_booking",
        "status": status.value,
        "worker_generation": str(_WORKER),
        "dispatch_id": str(_DISPATCH),
        "duration_ms": 1234,
        "submitted": status is DispatchStatus.OK,
        "replayed": False,
        "observation_id": str(_OBSERVATION),
        **extra,
    }


def _event(sequence: int, event_type: TaskEventType, payload: dict[str, Any]) -> TaskEventResponse:
    return TaskEventResponse(
        id=sequence,
        task_id=_TASK,
        sequence=sequence,
        task_revision=sequence,
        event_type=event_type.value,
        payload=payload,
        created_at=_at(sequence),
    )


def _action_payload(status: ActionStatus, revision: int, **extra: Any) -> dict[str, Any]:
    return {
        "action_id": str(_ACTION),
        "tool_name": "commit_booking",
        "risk_tier": "R2",
        "proposal_digest": proposal_digest(_proposal()),
        "action_status": status.value,
        "action_revision": revision,
        **extra,
    }


def _voice_criteria(max_price: int = 1000) -> BookingCriteria:
    return BookingCriteria(
        specialty="Dermatology",
        day="Saturday",
        earliest_time="17:00",
        latest_time="21:00",
        max_price=max_price,
        max_price_currency="INR",
    )


def _voice_task(
    *, revision: int = 1, sequence: int = 1, status: TaskStatus = TaskStatus.CREATED
) -> TaskResponse:
    return TaskResponse(
        id=_TASK,
        status=status,
        revision=revision,
        last_event_sequence=sequence,
        request={
            "type": "appointment_booking",
            "text": "Find me a dermatologist Saturday evening under 1000",
            "source": "voice",
            "voice_turn_id": "item_example01",
            **_voice_criteria().request_fields(),
        },
        created_at=_at(0),
        updated_at=_at(revision),
    )


def _slot(slot_id: str, doctor: str, time: str, price: int) -> dict[str, Any]:
    return BookingSlotResponse(
        slot_id=slot_id,
        doctor=doctor,
        specialty="Dermatology",
        time=datetime.fromisoformat(time),
        price=price,
        currency="INR",
    ).model_dump(mode="json")


def _profile() -> DoctorProfileResponse:
    return DoctorProfileResponse(
        doctor_id="dr-a",
        doctor="Dr A",
        specialty="Dermatology",
        clinic="Lakeview Skin Clinic",
        address="12 Lake Road, Hyderabad",
        hours="Mon-Sat 10:00-20:00",
        consultation_fee=800,
        currency="INR",
        languages=["English", "Telugu", "Hindi"],
        walk_ins=False,
    )


def examples() -> dict[str, Any]:
    pending = ApprovalResponse(
        id=_APPROVAL,
        action_id=_ACTION,
        action_revision=2,
        proposal_digest=proposal_digest(_proposal()),
        status=ApprovalStatus.PENDING,
        created_at=_at(2),
        expires_at=_at(302),
        approved_at=None,
        rejected_at=None,
        consumed_at=None,
    )
    evidence_found = {
        "source": "browser_lookup",
        "site": "appointment_fixture",
        "reference": f"lumi-{_ACTION}",
        "operation": "lookup_booking",
        "observation_id": str(_OBSERVATION),
        "lookup": LookupStatus.FOUND.value,
        "booking_id": "BK-0001",
        "booking_count": 1,
        "booking": {
            "booking_id": "BK-0001",
            "reference": f"lumi-{_ACTION}",
            "slot_id": "slot-a-1830",
            "doctor": "Dr A",
            "time": "2026-09-19T18:30:00+05:30",
            "price": 800,
            "currency": "INR",
        },
    }
    return {
        "health": HealthResponse(
            status="ok", database="ok", runtime_generation=_GENERATION
        ).model_dump(mode="json"),
        "task": TaskResponse(
            id=_TASK,
            status=TaskStatus.WAITING_APPROVAL,
            revision=3,
            last_event_sequence=3,
            request={
                "type": "appointment_booking",
                "text": "Book an appointment",
                "specialty": "Dermatology",
                "day": "Saturday",
            },
            created_at=_at(0),
            updated_at=_at(3),
        ).model_dump(mode="json"),
        "search": BookingSearchResponse(
            task_id=_TASK,
            slots=[
                BookingSlotResponse(
                    slot_id="slot-a-1830",
                    doctor="Dr A",
                    specialty="Dermatology",
                    time=datetime.fromisoformat("2026-09-19T18:30:00+05:30"),
                    price=800,
                    currency="INR",
                )
            ],
        ).model_dump(mode="json"),
        "action_waiting_approval": _action(ActionStatus.WAITING_APPROVAL, 2, approval=pending),
        "action_succeeded": _action(
            ActionStatus.SUCCEEDED,
            5,
            attempts=[
                _attempt(
                    AttemptOutcome.SUCCEEDED,
                    _dispatch_summary(
                        DispatchStatus.OK,
                        page="confirmation",
                        booking_id="BK-0001",
                        reference=f"lumi-{_ACTION}",
                        postcondition_verified=True,
                        changed_facts=[],
                        receipt={
                            "booking_id": "BK-0001",
                            "reference": f"lumi-{_ACTION}",
                            "doctor": "Dr A",
                            "price": 800,
                            "currency": "INR",
                        },
                    ),
                    None,
                )
            ],
        ),
        "action_changed_price": _action(
            ActionStatus.FAILED,
            5,
            attempts=[
                _attempt(
                    AttemptOutcome.FAILED,
                    _dispatch_summary(
                        DispatchStatus.CHANGED_RESOURCE,
                        page="slot_detail",
                        reference=f"lumi-{_ACTION}",
                        postcondition_verified=False,
                        changed_facts=[{"field": "price", "approved": "800", "observed": "950"}],
                    ),
                    "approved_values_changed",
                )
            ],
        ),
        "action_outcome_unknown": _action(
            ActionStatus.OUTCOME_UNKNOWN,
            5,
            attempts=[_attempt(AttemptOutcome.OUTCOME_UNKNOWN, None, "runtime_restart")],
        ),
        "events": TaskEventListResponse(
            task_id=_TASK,
            events=[
                _event(1, TaskEventType.TASK_CREATED, {"status": "CREATED"}),
                _event(
                    2,
                    TaskEventType.ACTION_PROPOSED,
                    _action_payload(
                        ActionStatus.PROPOSED,
                        1,
                        idempotency_key="commit_booking-1",
                        requires_approval=True,
                    ),
                ),
                _event(
                    3,
                    TaskEventType.ACTION_APPROVAL_REQUESTED,
                    _action_payload(ActionStatus.WAITING_APPROVAL, 2, approval_ttl_seconds=300),
                ),
                _event(
                    4,
                    TaskEventType.ACTION_APPROVED,
                    _action_payload(ActionStatus.APPROVED, 3, approval_id=str(_APPROVAL)),
                ),
                _event(
                    5,
                    TaskEventType.ACTION_EXECUTION_STARTED,
                    _action_payload(
                        ActionStatus.EXECUTING,
                        4,
                        attempt_id=str(_ATTEMPT),
                        attempt_number=1,
                        approval_id=str(_APPROVAL),
                        runtime_generation=str(_GENERATION),
                    ),
                ),
                _event(
                    6,
                    TaskEventType.ACTION_OUTCOME_UNKNOWN,
                    _action_payload(
                        ActionStatus.OUTCOME_UNKNOWN,
                        5,
                        attempt_id=str(_ATTEMPT),
                        attempt_number=1,
                        outcome="OUTCOME_UNKNOWN",
                        error_code="runtime_restart",
                        reason="runtime_restart",
                        recovered_from_generation=str(_GENERATION),
                        browser_dispatch_id=str(_DISPATCH),
                    ),
                ),
                _event(
                    7,
                    TaskEventType.ACTION_RECONCILIATION_STARTED,
                    _action_payload(ActionStatus.RECONCILING, 6),
                ),
                _event(
                    8,
                    TaskEventType.ACTION_RECONCILED,
                    _action_payload(
                        ActionStatus.SUCCEEDED,
                        7,
                        result="SUCCEEDED",
                        evidence=evidence_found,
                        reason="reconciliation",
                    ),
                ),
            ],
        ).model_dump(mode="json"),
        "task_voice": _voice_task().model_dump(mode="json"),
        "criteria_revised": ReviseBookingCriteriaResponse(
            task=_voice_task(revision=5, sequence=5),
            invalidated_action_ids=[_ACTION],
        ).model_dump(mode="json"),
        "task_cancelled": CancelBookingTaskResponse(
            task=_voice_task(revision=6, sequence=6, status=TaskStatus.CANCELLED),
            rejected_action_ids=[],
        ).model_dump(mode="json"),
        "events_refinement": TaskEventListResponse(
            task_id=_TASK,
            events=[
                _event(1, TaskEventType.TASK_CREATED, {"status": "CREATED"}),
                _event(
                    2,
                    TaskEventType.TASK_SEARCH_COMPLETED,
                    {
                        "criteria": _voice_criteria().model_dump(mode="json"),
                        "slots": [
                            _slot("slot-a-1830", "Dr A", "2026-09-19T18:30:00+05:30", 800),
                            _slot("slot-b-1915", "Dr B", "2026-09-19T19:15:00+05:30", 950),
                        ],
                        "observed_count": 2,
                        "excluded_count": 0,
                    },
                ),
                _event(
                    3,
                    TaskEventType.TASK_CRITERIA_UPDATED,
                    {
                        "criteria": _voice_criteria(max_price=900).model_dump(mode="json"),
                        "invalidated_action_ids": [str(_ACTION)],
                        "reason": "criteria_changed",
                    },
                ),
                _event(
                    4,
                    TaskEventType.ACTION_REJECTED,
                    _action_payload(
                        ActionStatus.REJECTED, 3, approval_id=str(_APPROVAL), reason="criteria_changed"
                    ),
                ),
                _event(
                    5,
                    TaskEventType.TASK_CANCELLED,
                    {
                        "from_status": "READY",
                        "to_status": "CANCELLED",
                        "rejected_action_ids": [],
                        "reason": "user_cancelled",
                    },
                ),
            ],
        ).model_dump(mode="json"),
        "task_dated": TaskResponse(
            id=_TASK,
            status=TaskStatus.READY,
            revision=2,
            last_event_sequence=2,
            request={
                "type": "appointment_booking",
                "text": "Find a dermatologist this weekend",
                "source": "text",
                "request_id": "req_example01",
                **BookingCriteria(
                    specialty="Dermatology", date_from="2026-09-19", date_to="2026-09-20"
                ).request_fields(),
            },
            created_at=_at(0),
            updated_at=_at(2),
        ).model_dump(mode="json"),
        "clinic_info": ClinicInfoResponse(
            task=TaskResponse(
                id=_TASK,
                status=TaskStatus.CREATED,
                revision=2,
                last_event_sequence=2,
                request={
                    "type": "clinic_info",
                    "text": "Which languages does Dr A speak?",
                    "doctor": "Dr A",
                    "topic": "languages",
                    "source": "voice",
                    "voice_turn_id": "item_example02",
                },
                created_at=_at(0),
                updated_at=_at(2),
            ),
            profiles=[_profile()],
        ).model_dump(mode="json"),
        "events_info": TaskEventListResponse(
            task_id=_TASK,
            events=[
                _event(1, TaskEventType.TASK_CREATED, {"status": "CREATED"}),
                _event(
                    2,
                    TaskEventType.TASK_INFO_LOOKUP_COMPLETED,
                    {
                        "query": {"specialty": "", "doctor": "Dr A", "topic": "languages"},
                        "profiles": [_profile().model_dump(mode="json")],
                        "observed_count": 1,
                    },
                ),
            ],
        ).model_dump(mode="json"),
        "error_stale": ErrorResponse(
            error=ErrorDetail(
                code="stale_action_revision",
                message="Action is at revision 3, not 2.",
                current_revision=3,
            )
        ).model_dump(mode="json", exclude_none=True),
    }


def build_contract() -> dict[str, Any]:
    return {
        "version": 1,
        "enums": {
            "ActionStatus": _enum(ActionStatus),
            "ApprovalStatus": _enum(ApprovalStatus),
            "AttemptOutcome": _enum(AttemptOutcome),
            "ChangedFactField": list(CHANGED_FACT_FIELDS),
            "DispatchStatus": _enum(DispatchStatus),
            "LookupStatus": _enum(LookupStatus),
            "RiskTier": _enum(RiskTier),
            "TaskEventType": _enum(TaskEventType),
            "TaskStatus": _enum(TaskStatus),
        },
        "errorCodes": list(ERROR_CODES),
        "schemas": {model.__name__: model.model_json_schema() for model in _MODELS},
        "examples": examples(),
    }


def render_contract() -> str:
    return json.dumps(build_contract(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the desktop runtime contract")
    parser.add_argument("--write", action="store_true")
    arguments = parser.parse_args()
    rendered = render_contract()
    if arguments.write:
        Path(CONTRACT_PATH).write_bytes(rendered.encode("ascii"))
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
