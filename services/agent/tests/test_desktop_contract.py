"""The committed desktop contract matches the runtime that serves it."""

import re
from pathlib import Path

from app.api import contract
from app.api.schemas import (
    ActionResponse,
    BookingSearchResponse,
    CancelBookingTaskResponse,
    ErrorResponse,
    ReviseBookingCriteriaResponse,
    TaskEventListResponse,
    TaskResponse,
)
from app.domain.booking import CHANGED_FACT_FIELDS, BookingProposal, ObservedSlot, changed_facts

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def test_committed_contract_is_current() -> None:
    committed = contract.CONTRACT_PATH.read_text(encoding="ascii")
    assert committed == contract.render_contract(), (
        "src/shared/agent-runtime-contract.json is stale; run "
        "`uv run python -m app.api.contract --write` and update the TypeScript side."
    )


def test_examples_are_valid_runtime_payloads() -> None:
    examples = contract.examples()
    TaskResponse.model_validate(examples["task"])
    TaskEventListResponse.model_validate(examples["events"])
    TaskEventListResponse.model_validate(examples["events_refinement"])
    TaskResponse.model_validate(examples["task_voice"])
    ReviseBookingCriteriaResponse.model_validate(examples["criteria_revised"])
    CancelBookingTaskResponse.model_validate(examples["task_cancelled"])
    BookingSearchResponse.model_validate(examples["search"])
    ErrorResponse.model_validate(examples["error_stale"])
    for name in ("action_waiting_approval", "action_succeeded", "action_changed_price", "action_outcome_unknown"):
        action = ActionResponse.model_validate(examples[name])
        BookingProposal.model_validate(action.proposal)


def test_every_emitted_error_code_is_declared() -> None:
    sources = (APP_ROOT / "api" / "errors.py").read_text() + (APP_ROOT / "api" / "security.py").read_text()
    emitted = set(re.findall(r'(?:_simple|_fixed)\(\s*status\.\w+,\s*"([a-z_]+)"', sources))
    emitted |= set(re.findall(r'code="([a-z_]+)"', sources))
    emitted |= set(re.findall(r'_reject\([^)]*?"([a-z_]+)"', sources, flags=re.S))
    assert emitted, "the scan found no codes; the pattern is broken"
    assert emitted <= set(contract.ERROR_CODES)


def test_changed_fact_fields_are_the_fields_compared() -> None:
    from datetime import datetime

    approved = BookingProposal(
        site="appointment_fixture", slot_id="a", doctor="A",
        time=datetime.fromisoformat("2026-09-19T18:30:00+05:30"), price=1, currency="INR",
    )
    observed = ObservedSlot(
        slot_id="b", doctor="B",
        time=datetime.fromisoformat("2026-09-19T19:30:00+05:30"), price=2, currency="USD",
    )
    assert tuple(change.field for change in changed_facts(approved, observed)) == CHANGED_FACT_FIELDS
