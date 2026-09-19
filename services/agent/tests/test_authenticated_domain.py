"""The closed vocabulary of authenticated reading (Milestone 8a S3), no I/O.

Scope, steps, observations, the registry's invariants and the site-scope
predicate. What is being pinned is the *shape* of what a model, a page or a
caller can express: there is no field for the things that must never be
expressible, and the invariants that keep account reading from being mistaken
for something safer than it is are constructor errors, not conventions.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.browser import site_scope
from app.browser.registry import (
    BrowserOperation,
    Effect,
    OperationRegistry,
    OperationTarget,
    Reconciliation,
    RetryPolicy,
    build_registry,
)
from app.domain.authenticated import (
    MAX_AUTH_TEXT_CHARS,
    AuthenticatedBudgets,
    AuthenticatedDisclosure,
    AuthenticatedObservation,
    AuthenticatedProposal,
    AuthenticatedReadScope,
    AuthenticatedRefusal,
    AuthOperation,
    CredentialSurfaceResult,
    WorkerReadResult,
    parse_authenticated_step,
)
from app.domain.login_takeover import CredentialSignal, hash_identity
from app.domain.research import ObservedLink, TextBlock, compute_content_hash

FINGERPRINT = hash_identity("fixture-account-1")
PROFILE = uuid.uuid4()


def scope(**overrides: Any) -> AuthenticatedReadScope:
    fields: dict[str, Any] = {
        "profile_id": PROFILE, "site": "github.com",
        "allowed_origins": ["https://github.com", "https://www.github.com"],
        "allowed_operations": list(AuthOperation),
        "disclosure": AuthenticatedDisclosure(recipient="gemini"),
        "account_fingerprint": FINGERPRINT, "profile_revoke_epoch": 0,
    }
    fields.update(overrides)
    return AuthenticatedReadScope(**fields)


def observation(blocks: list[str], **overrides: Any) -> AuthenticatedObservation:
    text_blocks = [TextBlock(id=f"b{index + 1}", text=value) for index, value in enumerate(blocks)]
    fields: dict[str, Any] = {
        "observation_id": uuid.uuid4(), "kind": "page", "operation": AuthOperation.OBSERVE, "sequence": 1,
        "profile_id": PROFILE, "tab": "t1", "host": "github.com", "title": "Repositories",
        "observed_at": datetime.now(UTC), "blocks": text_blocks, "open_tabs": ["t1"],
        "content_hash": compute_content_hash(
            kind="page", final_url="github.com", title="Repositories", blocks=text_blocks, links=[], results=[]),
    }
    fields.update(overrides)
    return AuthenticatedObservation(**fields)


# ---- the scope -------------------------------------------------------------------------


def test_the_scope_is_bound_to_one_profile_account_epoch_and_provider() -> None:
    value = scope()
    dumped = value.model_dump(mode="json")
    assert dumped["kind"] == "authenticated_read" and dumped["classification"] == "account_private"
    assert dumped["methods"] == ["GET", "HEAD"]
    assert dumped["disclosure"]["recipient"] == "gemini" and dumped["disclosure"]["failover"] == "none"
    assert dumped["disclosure"]["max_text_chars"] == 4_000 and dumped["disclosure"]["max_blocks"] == 60
    assert dumped["budgets"]["max_vision_calls"] == 0
    assert dumped["website_side_effects_possible"] is True
    assert value.digest == scope().digest
    for changed in (
        scope(profile_revoke_epoch=1), scope(account_fingerprint=hash_identity("someone-else")),
        scope(disclosure=AuthenticatedDisclosure(recipient="openai")), scope(site="example.com"),
    ):
        assert changed.digest != value.digest


@pytest.mark.parametrize(
    "override",
    [
        {"methods": ["GET", "POST"]}, {"methods": []}, {"methods": ["PUT"]},
        {"classification": "public"}, {"website_side_effects_possible": False},
        {"allowed_operations": []}, {"allowed_operations": [AuthOperation.OBSERVE, AuthOperation.OBSERVE]},
        {"disclosure": {"recipient": "gemini", "max_text_chars": 4_001}},
        {"disclosure": {"recipient": "gemini", "max_blocks": 61}},
        {"disclosure": {"recipient": "gemini", "failover": "any"}},
        {"disclosure": {"recipient": "gemini", "identifiers_reduced": False}},
        {"disclosure": {"recipient": ["gemini", "openai"]}},
        {"disclosure": {"recipients": ["gemini"]}},
        {"disclosure": {"recipient": "https://evil.example"}},
        {"budgets": {"max_vision_calls": 1}}, {"budgets": {"max_steps": 13}}, {"budgets": {"max_tabs": 4}},
        {"account_fingerprint": "not-a-hash"}, {"profile_revoke_epoch": -1},
        {"allowed_origins": []}, {"unexpected": True},
    ],
)
def test_the_scope_refuses_anything_outside_the_reviewed_maxima(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        scope(**override)


def test_a_grant_may_narrow_its_limits_but_never_widen_them() -> None:
    narrow = scope(disclosure=AuthenticatedDisclosure(recipient="gemini", max_text_chars=1_000, max_blocks=10),
                   budgets=AuthenticatedBudgets(max_steps=3, max_tabs=1))
    assert narrow.disclosure.max_text_chars == 1_000 and narrow.budgets.max_steps == 3
    assert MAX_AUTH_TEXT_CHARS == 4_000


def test_the_scope_names_no_url_cookie_or_provider_key() -> None:
    fields = set(AuthenticatedReadScope.model_fields)
    for forbidden in ("url", "urls", "selector", "cookies", "headers", "storage_state", "provider", "recipients", "vision"):
        assert forbidden not in fields


# ---- the step vocabulary ------------------------------------------------------------------


def test_the_operation_set_is_exactly_the_five() -> None:
    assert [operation.value for operation in AuthOperation] == ["navigate", "observe", "reveal", "tab", "history"]


@pytest.mark.parametrize(
    "step",
    [
        {"operation": "navigate", "tab": "t1", "target": {"kind": "link", "observation": "o1", "ref": "l1"}},
        {"operation": "observe", "tab": "t2"},
        {"operation": "reveal", "tab": "t1", "target": {"kind": "block", "observation": "o2", "ref": "b3"}},
        {"operation": "reveal", "tab": "t1", "target": {"kind": "link", "observation": "o2", "ref": "l3"}},
        {"operation": "history", "tab": "t1", "direction": "forward"},
        {"operation": "tab", "action": "open"},
        {"operation": "tab", "action": "close", "tab": "t2"},
    ],
)
def test_a_well_formed_step_parses(step: dict[str, Any]) -> None:
    assert parse_authenticated_step(step).operation.value == step["operation"]


@pytest.mark.parametrize(
    "step",
    [
        {"operation": "observe", "tab": "t1", "url": "https://evil.example"},
        {"operation": "observe", "tab": "t1", "host": "evil.example"},
        {"operation": "observe", "tab": "t1", "selector": "a"},
        {"operation": "observe", "tab": "t1", "provider": "openai"},
        {"operation": "observe", "tab": "t1", "method": "POST"},
        {"operation": "observe", "tab": "t1", "javascript": "1"},
        {"operation": "observe", "tab": "t4"},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "link", "observation": "o1", "ref": "l1", "href": "x"}},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "seed", "ref": "s1"}},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "result", "observation": "o1", "ref": "r1"}},
        {"operation": "reveal", "tab": "t1", "target": {"kind": "coordinate", "observation": "o1", "ref": "b1"}},
        {"operation": "tab", "action": "open", "tab": "t2"}, {"operation": "tab", "action": "close"},
        {"operation": "history", "tab": "t1", "direction": "sideways"},
        {"operation": "click", "tab": "t1"}, {"operation": "type", "tab": "t1"}, {"operation": "scroll", "tab": "t1"},
        {"operation": "public_search", "query": "x"}, {"operation": "prepare_form"}, {"operation": "upload"},
        {}, {"tab": "t1"}, "observe", None,
    ],
)
def test_anything_outside_the_vocabulary_is_refused_whole(step: Any) -> None:
    with pytest.raises(AuthenticatedRefusal):
        parse_authenticated_step(step)


def test_a_proposal_carries_no_address_and_binds_the_classification() -> None:
    proposal = AuthenticatedProposal(
        operation=AuthOperation.NAVIGATE, step_number=1, policy_version="authenticated-read-v1",
        grant_id=uuid.uuid4(), scope_digest="a" * 64, profile_id=PROFILE, profile_revoke_epoch=0,
        step={"operation": "navigate", "tab": "t1", "target": {"kind": "link", "observation": "o1", "ref": "l1"}},
        tab="t1", expected_document_epoch=1,
    )
    dumped = proposal.model_dump(mode="json")
    assert dumped["classification"] == "account_private" and dumped["kind"] == "authenticated_read_step"
    assert "url" not in dumped and "destination_url" not in dumped and "destination_host" not in dumped
    with pytest.raises(ValidationError):
        AuthenticatedProposal(**{**dumped, "step": {"operation": "click"}})


# ---- observations -------------------------------------------------------------------------


def test_an_observation_is_redacted_classified_and_carries_no_address() -> None:
    value = observation(["lumi-notes - Private", "Customer ⟦digits:2345⟧"])
    dumped = value.model_dump(mode="json")
    assert dumped["classification"] == "account_private" and dumped["provenance"] == "untrusted_environment"
    assert not any(key in dumped for key in ("url", "final_url", "requested_url", "redirects", "results", "query"))
    assert value.block("b1") is not None and value.block("b9") is None and value.ref == "o1"


@pytest.mark.parametrize(
    "leak",
    ["mail satish@example.test", "call +91 9876543210", "id 123456789012345", "card 4242424242424242"],
)
def test_an_observation_still_containing_an_identifier_cannot_exist(leak: str) -> None:
    with pytest.raises(ValidationError):
        observation([leak])
    with pytest.raises(ValidationError):
        observation(["fine"], title=leak)


def test_an_observation_with_a_link_label_that_leaks_is_refused() -> None:
    blocks = [TextBlock(id="b1", text="ok")]
    links = [ObservedLink(id="l1", text="mail satish@example.test", host="github.com")]
    with pytest.raises(ValidationError):
        AuthenticatedObservation(
            observation_id=uuid.uuid4(), kind="page", operation=AuthOperation.OBSERVE, sequence=1,
            profile_id=PROFILE, tab="t1", host="github.com", title="t", observed_at=datetime.now(UTC),
            blocks=blocks, links=links, open_tabs=["t1"],
            content_hash=compute_content_hash(kind="page", final_url="github.com", title="t", blocks=blocks, links=links, results=[]))


def test_the_private_text_budget_is_enforced_by_the_shape() -> None:
    blocks = [f"{'x' * 400} {index}" for index in range(11)]
    with pytest.raises(ValidationError):
        observation(blocks)
    with pytest.raises(ValidationError):
        observation(["ok"], content_hash="0" * 64)  # The hash must match the content.
    with pytest.raises(ValidationError):
        observation(["ok"], host=None)
    with pytest.raises(ValidationError):
        observation(["ok"], classification="public")


def test_a_worker_result_is_exactly_one_of_three_shapes() -> None:
    surface = CredentialSurfaceResult(
        signals=[CredentialSignal.PASSWORD_FIELD], tab="t1", document_epoch=1, observed_at=datetime.now(UTC))
    assert WorkerReadResult(credential_surface=surface).observation is None
    with pytest.raises(ValidationError):
        WorkerReadResult()
    with pytest.raises(ValidationError):
        WorkerReadResult(credential_surface=surface, observation=observation(["ok"]))
    dumped = WorkerReadResult(credential_surface=surface).model_dump_json()
    assert "title" not in dumped and "blocks" not in dumped and "text" not in dumped


# ---- the registry -------------------------------------------------------------------------


def _operation(**overrides: Any) -> BrowserOperation:
    values: dict[str, Any] = {
        "name": "authenticated_probe", "description": "d", "input_model": AuthenticatedBudgets,
        "output_model": AuthenticatedBudgets, "effect": Effect.ACCOUNT_READ, "retry": RetryPolicy.OBSERVE_THEN_REPLAN,
        "reconciliation": Reconciliation.NOT_REQUIRED, "timeout_seconds": 1.0, "timeout_meaning": "m",
        "preconditions": (), "postconditions": (), "handler": None, "target": OperationTarget.AUTHENTICATED_SESSION,
    }
    values.update(overrides)
    return BrowserOperation(**values)


def test_a_well_formed_account_read_operation_registers() -> None:
    assert len(OperationRegistry((_operation(),))) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"effect": Effect.READ_ONLY},
        {"effect": Effect.PREPARE},
        {"effect": Effect.CONSEQUENTIAL},
        {"retry": RetryPolicy.SAFE_TO_RETRY},
        {"retry": RetryPolicy.NEW_APPROVAL_REQUIRED},
        {"retry": RetryPolicy.RECONCILE_BEFORE_RETRY},
        {"reconciliation": Reconciliation.LOOKUP_BOOKING},
        {"target": OperationTarget.RESEARCH_SESSION},
        {"target": OperationTarget.PUBLIC_PAGE},
        {"target": OperationTarget.REVIEWED_SITE},
    ],
)
def test_the_registry_refuses_a_malformed_account_read(override: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        OperationRegistry((_operation(**override),))


def test_no_operation_targeting_the_authenticated_session_may_declare_reconciliation() -> None:
    """There is no authoritative verifier for 'did the site record my visit'."""
    with pytest.raises(ValueError, match="reconciliation"):
        OperationRegistry((_operation(reconciliation=Reconciliation.LOOKUP_BOOKING),))


def test_the_served_registry_marks_every_account_read_as_such() -> None:
    registry = build_registry()
    names = [name for name in registry.names() if name.startswith("authenticated_")]
    assert names == [
        "authenticated_history", "authenticated_navigate", "authenticated_observe",
        "authenticated_reveal", "authenticated_tab",
    ]
    for name in names:
        operation = registry.get(name)
        assert operation is not None
        assert operation.effect is Effect.ACCOUNT_READ and operation.effect.value != "READ_ONLY"
        assert operation.target is OperationTarget.AUTHENTICATED_SESSION
        assert operation.retry is RetryPolicy.OBSERVE_THEN_REPLAN
        assert operation.reconciliation is Reconciliation.NOT_REQUIRED
    # And nothing else may target the authenticated session.
    others = [
        name for name in registry.names()
        if name not in names and registry.get(name).target is OperationTarget.AUTHENTICATED_SESSION  # type: ignore[union-attr]
    ]
    assert others == []


def test_the_effect_is_named_account_read_and_never_read_only() -> None:
    assert Effect.ACCOUNT_READ.value == "ACCOUNT_READ"
    assert {Effect.ACCOUNT_READ.value, Effect.READ_ONLY.value} == {"ACCOUNT_READ", "READ_ONLY"}


# ---- site scope -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "site", "expected"),
    [
        ("https://github.com/x", "github.com", True),
        ("https://www.github.com/x", "github.com", True),
        ("https://gist.github.com/x?y=1", "github.com", True),
        ("https://github.io/x", "github.com", False),
        ("https://evil-github.com/", "github.com", False),
        ("https://github.com.evil.example/", "github.com", False),
        ("https://user.github.io/", "user.github.io", True),
        ("https://other.github.io/", "user.github.io", False),
        ("https://bbc.co.uk/", "bbc.co.uk", True),
        ("https://evil.co.uk/", "bbc.co.uk", False),
        ("http://127.0.0.1/", "127.0.0.1", False),  # An address has no registrable domain.
        ("not a url", "github.com", False),
        ("https:///path", "github.com", False),
    ],
)
def test_site_scope_uses_the_registrable_domain(url: str, site: str, expected: bool) -> None:
    assert site_scope.in_site(url, site) is expected
