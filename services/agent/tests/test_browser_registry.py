"""Unit tests for the closed registry. No database, no browser, no processes.

What these protect is the shape of the capability, not any one operation: that
the set of things a browser can be asked to do is fixed, that a consequential
operation cannot exist without a reconciliation path, and that a caller cannot
reach past the registry by naming something clever.
"""

import inspect
from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.browser.adapters import appointment_fixture
from app.browser.operations import public_page
from app.browser.registry import (
    Effect,
    OperationRegistry,
    OperationTarget,
    Reconciliation,
    RetryPolicy,
    build_registry,
)
from app.domain.browser_dispatch import BrowserEffect
from app.domain.sites import site_trust

REGISTRY = build_registry()

EXPECTED_OPERATIONS = {
    "search_appointments",
    "read_available_slots",
    "prepare_booking",
    "commit_booking",
    "lookup_booking",
    # Milestone 6: the read-only clinic-information workflow.
    "read_doctor_profiles",
    # Milestone 7a: one generic, read-only public page inspection.
    "inspect_public_page",
    # Milestone 7b: the five read-only research operations. `public_search` is
    # deliberately absent: it is a bounded JSON GET the runtime makes itself,
    # so no search credential and no search-result markup reaches the browser.
    "research_navigate",
    "research_observe",
    "research_scroll",
    "research_history",
    "research_tab",
    # Milestone 8a S3: the five ACCOUNT_READ operations, in the persistent
    # profile's own read session. No search, no scroll-by-key, no click.
    "authenticated_navigate",
    "authenticated_observe",
    "authenticated_reveal",
    "authenticated_history",
    "authenticated_tab",
}

AUTHENTICATED_OPERATIONS = {
    "authenticated_navigate",
    "authenticated_observe",
    "authenticated_reveal",
    "authenticated_history",
    "authenticated_tab",
}

RESEARCH_OPERATIONS = {
    "research_navigate",
    "research_observe",
    "research_scroll",
    "research_history",
    "research_tab",
}


def test_the_registry_holds_exactly_the_reviewed_operations() -> None:
    assert set(REGISTRY.names()) == EXPECTED_OPERATIONS
    assert len(REGISTRY) == len(EXPECTED_OPERATIONS)


@pytest.mark.parametrize(
    "name",
    [
        "evaluate",
        "javascript",
        "goto",
        "click",
        "page.evaluate",
        "commit_booking2",
        "COMMIT_BOOKING",
        "",
        "../commit_booking",
    ],
)
def test_nothing_outside_the_registry_resolves(name: str) -> None:
    """There is no naming trick that reaches code the registry does not list."""
    assert REGISTRY.get(name) is None
    assert name not in REGISTRY


def test_every_operation_declares_a_full_contract() -> None:
    for name in REGISTRY.names():
        operation = REGISTRY.get(name)
        assert operation is not None
        assert operation.description
        assert operation.preconditions, f"{name} declares no preconditions"
        assert operation.postconditions, f"{name} declares no postconditions"
        assert operation.timeout_meaning, f"{name} does not say what a timeout means"
        assert operation.timeout_seconds > 0


def test_exactly_one_operation_can_change_the_outside_world() -> None:
    consequential = [
        name
        for name in REGISTRY.names()
        if (operation := REGISTRY.get(name)) and operation.effect is Effect.CONSEQUENTIAL
    ]
    assert consequential == ["commit_booking"]


def test_the_consequential_operation_may_not_be_blindly_retried() -> None:
    commit = REGISTRY.get("commit_booking")
    assert commit is not None
    assert commit.retry is RetryPolicy.RECONCILE_BEFORE_RETRY
    assert commit.reconciliation is Reconciliation.LOOKUP_BOOKING


def test_the_reconciliation_operation_is_read_only() -> None:
    """Reconciliation that could act would be a retry wearing a different name."""
    lookup = REGISTRY.get("lookup_booking")
    assert lookup is not None
    assert lookup.effect is Effect.READ_ONLY
    assert lookup.retry is RetryPolicy.SAFE_TO_RETRY


def test_lookup_never_touches_the_submission_flag() -> None:
    """A read-only operation has no code path that can mark a submission."""
    source = inspect.getsource(appointment_fixture.lookup_booking)
    assert "submitted" not in source
    assert ".click(" not in source


def test_no_operation_exposes_arbitrary_scripting() -> None:
    """The adapter must not contain a general escape hatch into the page."""
    for module in (appointment_fixture, public_page):
        source = inspect.getsource(module)
        for forbidden in (
            "page.evaluate", "evaluate_all", "evaluate_handle", "add_script_tag", "add_init_script",
            "expose_function", "set_extra_http_headers", "add_cookies", "storage_state",
        ):
            assert forbidden not in source, f"{forbidden} would be a generic scripting primitive"


def test_the_public_page_operation_is_read_only_and_never_retried_without_approval() -> None:
    operation = REGISTRY.get("inspect_public_page")
    assert operation is not None
    assert operation.target is OperationTarget.PUBLIC_PAGE
    assert operation.effect is Effect.READ_ONLY
    assert operation.retry is RetryPolicy.NEW_APPROVAL_REQUIRED
    assert operation.reconciliation is Reconciliation.NOT_REQUIRED
    # Every other non-research operation keeps resolving its origin from the
    # worker's own allowlist.
    others = [
        REGISTRY.get(name)
        for name in REGISTRY.names()
        if name != "inspect_public_page"
        and name not in RESEARCH_OPERATIONS
        and name not in AUTHENTICATED_OPERATIONS
    ]
    assert all(op is not None and op.target is OperationTarget.REVIEWED_SITE for op in others)


def test_research_operations_are_read_only_and_recovered_by_re_observing() -> None:
    """A research step is never repeated blindly: the browser may have moved."""
    for name in RESEARCH_OPERATIONS:
        operation = REGISTRY.get(name)
        assert operation is not None, name
        assert operation.target is OperationTarget.RESEARCH_SESSION
        assert operation.effect is Effect.READ_ONLY
        assert operation.retry is RetryPolicy.OBSERVE_THEN_REPLAN
        assert operation.reconciliation is Reconciliation.NOT_REQUIRED


@pytest.mark.parametrize(
    ("name", "fields"),
    [
        ("research_navigate", {"sequence", "tab", "url", "target_kind", "target_ref", "expected_document_epoch"}),
        ("research_observe", {"sequence", "tab"}),
        ("research_scroll", {"sequence", "tab", "direction"}),
        ("research_history", {"sequence", "tab", "direction"}),
        ("research_tab", {"sequence", "action", "tab"}),
    ],
)
def test_research_inputs_hold_no_selector_script_or_method(name: str, fields: set[str]) -> None:
    operation = REGISTRY.get(name)
    assert operation is not None
    assert set(operation.input_model.model_fields) == fields
    for extra in ("selector", "xpath", "script", "javascript", "headers", "cookies", "browser_args", "method", "click", "coordinates"):
        with pytest.raises(ValidationError):
            operation.parse_input({"sequence": 1, "tab": "t1", extra: "x"})


def test_the_public_page_input_is_a_url_and_nothing_else() -> None:
    operation = REGISTRY.get("inspect_public_page")
    assert operation is not None
    assert set(operation.input_model.model_fields) == {"url"}
    for extra in ("selector", "xpath", "script", "javascript", "headers", "cookies", "browser_args", "click"):
        with pytest.raises(ValidationError):
            operation.parse_input({"url": "https://github.com/", extra: "x"})


@pytest.mark.parametrize(
    "change",
    [{"effect": Effect.CONSEQUENTIAL}, {"effect": Effect.PREPARE}, {"retry": RetryPolicy.SAFE_TO_RETRY}],
)
def test_a_public_page_operation_that_could_act_or_retry_is_rejected(change: dict[str, object]) -> None:
    operation = REGISTRY.get("inspect_public_page")
    assert operation is not None
    broken = replace(operation, **change)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        OperationRegistry((broken,))


def test_a_consequential_operation_without_reconciliation_is_rejected() -> None:
    commit = REGISTRY.get("commit_booking")
    assert commit is not None
    broken = replace(commit, reconciliation=Reconciliation.NOT_REQUIRED)
    with pytest.raises(ValueError, match="no reconciliation path"):
        OperationRegistry((broken,))


def test_a_consequential_operation_marked_retryable_is_rejected() -> None:
    commit = REGISTRY.get("commit_booking")
    assert commit is not None
    broken = replace(commit, retry=RetryPolicy.SAFE_TO_RETRY)
    with pytest.raises(ValueError, match="must not be safe to retry"):
        OperationRegistry((broken,))


def test_duplicate_names_are_rejected() -> None:
    commit = REGISTRY.get("commit_booking")
    assert commit is not None
    with pytest.raises(ValueError, match="duplicate browser operations"):
        OperationRegistry((commit, commit))


# ---- typed input validation -------------------------------------------------


def test_commit_input_refuses_unknown_fields() -> None:
    """An extra field is a field nobody reviewed. It is not quietly ignored."""
    commit = REGISTRY.get("commit_booking")
    assert commit is not None
    with pytest.raises(ValidationError):
        commit.parse_input(
            {
                "reference": "lumi-1",
                "proposal": {
                    "site": "appointment_fixture",
                    "slot_id": "slot-a-1830",
                    "doctor": "Dr A",
                    "time": "2026-09-19T18:30:00+05:30",
                    "price": 800,
                    "currency": "INR",
                },
                "javascript": "alert(1)",
            }
        )


def test_commit_input_requires_a_complete_proposal() -> None:
    commit = REGISTRY.get("commit_booking")
    assert commit is not None
    with pytest.raises(ValidationError):
        commit.parse_input({"reference": "lumi-1", "proposal": {"slot_id": "slot-a-1830"}})


def test_the_adapter_and_the_domain_agree_about_trusting_absence() -> None:
    """One flag, two places it is read. They must not drift apart."""
    trust = site_trust(appointment_fixture.SITE_NAME)
    assert (
        trust.lookup_absence_is_authoritative
        is appointment_fixture.LOOKUP_ABSENCE_IS_AUTHORITATIVE
    )
    assert trust.rationale


def test_an_unreviewed_site_is_trusted_with_nothing() -> None:
    """Absence proves nothing about a site nobody has made a claim about."""
    trust = site_trust("some_real_clinic_website")
    assert trust.lookup_absence_is_authoritative is False


def test_effect_is_the_domain_enum() -> None:
    """The database and the registry must classify effects identically."""
    assert Effect is BrowserEffect
