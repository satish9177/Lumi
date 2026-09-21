"""Milestone 9 S2 domain rules: the provider projection, the grant scope and grounding.

No database. These prove what a provider may be shown and what its answer may claim.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.desktop_disclosure import (
    ALLOWED_FIELDS,
    MAX_ANSWER_CHARS,
    MAX_PROJECTED_NODES,
    MAX_PROJECTED_TEXT_BYTES,
    AnswerNotGroundedError,
    DesktopDisclosureRefusal,
    DesktopDiscloseScope,
    DisplayTarget,
    build_projection,
    parse_read_result,
    verify_grounding,
)
from tests.desktop_disclosure_support import (
    SECRET_DIGITS,
    SECRET_EMAIL,
    VISIBLE,
    WINDOW_A,
    WINDOW_B,
    default_nodes,
    node,
    observation,
)

OBSERVED = datetime(2026, 9, 21, 10, 0, 0, tzinfo=UTC)
GENERATION = uuid.uuid4()


def snapshot_of(nodes: list[Any], **extra: Any) -> dict[str, Any]:
    return observation(nodes, worker_generation=GENERATION, **extra).model_dump(mode="json")


def project(nodes: list[Any]) -> Any:
    return build_projection(snapshot_of(nodes), observed_at=OBSERVED)


# ---- redaction ------------------------------------------------------------------------------------


def test_identifiers_are_redacted_and_the_visible_marker_survives() -> None:
    nodes = [
        node(1, name="Contacts"),
        node(2, text=f"Contact: {SECRET_EMAIL}", parent=1),
        node(3, text=f"Call {SECRET_DIGITS}", parent=1),
        node(4, text="Card 4242 4242 4242 4242", parent=1),
        node(5, text="Phone +91 98765 43210", parent=1),
        node(6, text=VISIBLE, parent=1),
    ]
    projection = project(nodes)
    dumped = json.dumps(projection.payload, ensure_ascii=False)
    assert VISIBLE in dumped
    for raw in (SECRET_EMAIL, SECRET_DIGITS, "4242 4242 4242 4242", "98765 43210"):
        assert raw not in dumped
    assert "⟦email:1⟧" in dumped and "⟦card:4242⟧" in dumped and "⟦digits:9999⟧" in dumped
    assert projection.redaction_count >= 4


def test_name_and_text_are_redacted_independently() -> None:
    projection = project([node(1, name=f"Mail {SECRET_EMAIL}", text=f"Reply to {SECRET_EMAIL}")])
    assert SECRET_EMAIL not in json.dumps(projection.payload)


# ---- what a provider never receives -----------------------------------------------------------------


def test_the_projection_has_only_the_reviewed_fields() -> None:
    projection = project(default_nodes())
    allowed = set(ALLOWED_FIELDS)
    for projected in projection.payload["nodes"]:
        assert set(projected) <= allowed
    assert set(projection.payload) == {
        "schema_version", "classification", "trust", "observed_at", "truncated", "truncation", "node_count", "nodes",
    }
    text = json.dumps(projection.payload).lower()
    for forbidden in (
        "window_title", "windowtitle", "application_label", "pid", "hwnd", "automationid", "automation_id",
        "classname", "class_name", "frameworkid", "runtimeid", "coordinate", "bounding", "patterns", "focusable",
        "worker_generation", "surface_ref", "surface_epoch", "fingerprint", "observation_id",
    ):
        assert f'"{forbidden}"' not in text


def test_node_and_text_caps_are_declared_as_truncation() -> None:
    many = [node(index, name=f"row {index}") for index in range(1, 181)]
    capped = project(many)
    assert capped.node_count == MAX_PROJECTED_NODES == len(capped.payload["nodes"])
    assert capped.truncated and "nodes" in capped.truncation

    heavy = [node(index, name="n" * 120, text="t" * 120) for index in range(1, 61)]
    text_capped = project(heavy)
    assert text_capped.text_bytes <= MAX_PROJECTED_TEXT_BYTES
    assert text_capped.node_count < 60
    assert text_capped.truncated and "text" in text_capped.truncation

    small = project([node(1, name="one")])
    assert not small.truncated and small.truncation == ()


def test_an_s1_truncated_observation_stays_marked_truncated() -> None:
    truncated = build_projection(
        snapshot_of([node(1, name="x")], truncated=True), observed_at=OBSERVED
    )
    assert truncated.truncated is True


def test_a_projected_parent_is_always_a_projected_node() -> None:
    nodes = [node(index, name=f"row {index}", parent=None if index == 1 else index - 1) for index in range(1, 150)]
    projection = project(nodes)
    kept = {item["control_ref"] for item in projection.payload["nodes"]}
    for item in projection.payload["nodes"]:
        assert item.get("parent_ref") is None or item["parent_ref"] in kept


def test_the_projection_and_its_digest_are_deterministic() -> None:
    snapshot = snapshot_of(default_nodes())
    first = build_projection(snapshot, observed_at=OBSERVED)
    second = build_projection(snapshot, observed_at=OBSERVED)
    assert first.payload == second.payload and first.digest == second.digest
    changed = build_projection(snapshot_of(default_nodes("OTHER_MARKER")), observed_at=OBSERVED)
    assert changed.digest != first.digest


def test_a_snapshot_that_is_not_the_closed_schema_is_refused() -> None:
    snapshot = snapshot_of(default_nodes())
    snapshot["nodes"][0]["hwnd"] = 1234
    with pytest.raises(DesktopDisclosureRefusal) as refused:
        build_projection(snapshot, observed_at=OBSERVED)
    assert refused.value.code == "observation_invalid"


# ---- the grant scope ------------------------------------------------------------------------------------


def scope_kwargs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "observation_id": uuid.uuid4(),
        "snapshot_digest": "a" * 64,
        "observed_at": OBSERVED,
        "worker_generation": uuid.uuid4(),
        "surface_ref": "s1",
        "surface_epoch": 1,
        "recipient": "openai",
        "model": "gpt-test",
        "display": DisplayTarget(application_label="Editor", window_title="notes"),
    }
    values.update(overrides)
    return values


def test_a_scope_is_closed_single_use_and_has_no_native_identity() -> None:
    scope = DesktopDiscloseScope(**scope_kwargs())
    dumped = scope.model_dump(mode="json")
    assert scope.max_provider_calls == 1 and scope.failover == "none"
    assert scope.allowed_fields == ALLOWED_FIELDS
    assert dumped["kind"] == "desktop_disclose" and dumped["classification"] == "desktop_private"
    for forbidden in ("hwnd", "pid", "process_path", "automation_id", "class_name", "runtime_id", "coordinates"):
        assert forbidden not in dumped
    assert scope.digest == DesktopDiscloseScope(**scope_kwargs(**{
        "observation_id": scope.observation_id, "worker_generation": scope.worker_generation,
    })).digest


@pytest.mark.parametrize("extra", [{"hwnd": 5}, {"pid": 5}, {"title": "x"}, {"process_path": "C:\\a.exe"}])
def test_a_scope_refuses_unknown_keys(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DesktopDiscloseScope(**scope_kwargs(**extra))


def test_a_scope_refuses_any_other_field_set_or_call_budget() -> None:
    with pytest.raises(ValidationError):
        DesktopDiscloseScope(**scope_kwargs(allowed_fields=("control_ref", "name")))
    with pytest.raises(ValidationError):
        DesktopDiscloseScope(**scope_kwargs(allowed_fields=(*ALLOWED_FIELDS, "hwnd")))
    with pytest.raises(ValidationError):
        DesktopDiscloseScope(**scope_kwargs(max_provider_calls=2))
    with pytest.raises(ValidationError):
        DesktopDiscloseScope(**scope_kwargs(failover="next"))


# ---- the read-only result -------------------------------------------------------------------------------------


def valid_answer(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "answer",
        "answer": "3 failing tests.",
        "evidence": [{"control_ref": "u2", "quote": "3 failing tests"}],
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "extra",
    [
        {"operation": "invoke"}, {"tool": "desktop"}, {"action": "click"}, {"invoke": "u5"}, {"click": True},
        {"coordinates": [1, 2]}, {"approval": "granted"}, {"provider": "gemini"}, {"grant": "x"}, {"setValue": "x"},
    ],
)
def test_a_result_with_any_action_shaped_key_is_refused_whole(extra: dict[str, Any]) -> None:
    with pytest.raises(DesktopDisclosureRefusal):
        parse_read_result(valid_answer(**extra))
    with pytest.raises(DesktopDisclosureRefusal):
        parse_read_result({"schema_version": 1, "kind": "cannot_answer", "reason": "not_in_snapshot", **extra})


def test_an_evidence_entry_with_an_extra_key_is_refused() -> None:
    with pytest.raises(DesktopDisclosureRefusal):
        parse_read_result(valid_answer(evidence=[{"control_ref": "u2", "quote": "3", "action": "invoke"}]))


@pytest.mark.parametrize(
    "payload",
    [
        "not a dict",
        {},
        {"kind": "act", "schema_version": 1},
        {"schema_version": 1, "kind": "cannot_answer", "reason": "because I said so"},
        {"schema_version": 2, "kind": "cannot_answer", "reason": "not_in_snapshot"},
        valid_answer(evidence=[]),
        valid_answer(evidence=[{"control_ref": "u201", "quote": "x"}]),
        valid_answer(evidence=[{"control_ref": "u2", "quote": "x"}] * 7),
        valid_answer(answer="x" * (MAX_ANSWER_CHARS + 1)),
        valid_answer(answer="   "),
        valid_answer(answer="bad\x00text"),
    ],
)
def test_a_malformed_or_out_of_bounds_result_is_refused(payload: Any) -> None:
    with pytest.raises(DesktopDisclosureRefusal):
        parse_read_result(payload)


def test_a_valid_answer_and_every_cannot_answer_reason_parse() -> None:
    assert parse_read_result(valid_answer()).kind == "answer"
    for reason in ("not_in_snapshot", "snapshot_incomplete", "unclear_question", "not_supported"):
        assert parse_read_result({"schema_version": 1, "kind": "cannot_answer", "reason": reason}).kind == "cannot_answer"
    bounded = valid_answer(answer="x" * MAX_ANSWER_CHARS)
    assert parse_read_result(bounded).kind == "answer"


# ---- grounding ----------------------------------------------------------------------------------------------------


def test_grounding_accepts_an_answer_quoting_the_projection() -> None:
    projection = project(default_nodes())
    verify_grounding(projection, parse_read_result(valid_answer()))
    verify_grounding(projection, parse_read_result(valid_answer(
        answer="The module is parser.", evidence=[{"control_ref": "u3", "quote": f"parser {VISIBLE}"}]
    )))
    verify_grounding(projection, parse_read_result(
        {"schema_version": 1, "kind": "cannot_answer", "reason": "not_in_snapshot"}
    ))


def test_grounding_refuses_an_invented_control_ref() -> None:
    projection = project(default_nodes())
    with pytest.raises(AnswerNotGroundedError) as refused:
        verify_grounding(projection, parse_read_result(valid_answer(
            evidence=[{"control_ref": "u99", "quote": "3 failing tests"}]
        )))
    assert refused.value.code == "unknown_control"


def test_grounding_refuses_a_quote_that_is_not_in_that_control() -> None:
    projection = project(default_nodes())
    with pytest.raises(AnswerNotGroundedError) as refused:
        verify_grounding(projection, parse_read_result(valid_answer(
            evidence=[{"control_ref": "u2", "quote": "12 failing tests"}]
        )))
    assert refused.value.code == "quote_not_in_control"
    # A quote that exists on a different control does not verify for this one.
    with pytest.raises(AnswerNotGroundedError):
        verify_grounding(projection, parse_read_result(valid_answer(
            evidence=[{"control_ref": "u2", "quote": "parser"}]
        )))


def test_a_quote_of_the_raw_unredacted_text_cannot_verify() -> None:
    projection = project(default_nodes())
    for raw in (SECRET_EMAIL, SECRET_DIGITS):
        with pytest.raises(AnswerNotGroundedError) as refused:
            verify_grounding(projection, parse_read_result(valid_answer(
                answer="Contact found.", evidence=[{"control_ref": "u4", "quote": raw}]
            )))
        assert refused.value.code == "quote_not_in_control"
    # The redacted form, which the provider actually saw, does verify.
    verify_grounding(projection, parse_read_result(valid_answer(
        answer="A contact email exists.", evidence=[{"control_ref": "u4", "quote": "⟦email:1⟧"}]
    )))


def test_a_quote_from_another_observation_cannot_verify() -> None:
    window_a = project([node(1, name="Title", text=WINDOW_A)])
    window_b = project([node(1, name="Title", text=WINDOW_B)])
    verify_grounding(window_a, parse_read_result(valid_answer(
        answer="It is A.", evidence=[{"control_ref": "u1", "quote": WINDOW_A}]
    )))
    with pytest.raises(AnswerNotGroundedError):
        verify_grounding(window_a, parse_read_result(valid_answer(
            answer="It is B.", evidence=[{"control_ref": "u1", "quote": WINDOW_B}]
        )))
    assert WINDOW_B in json.dumps(window_b.payload)
    assert WINDOW_B not in json.dumps(window_a.payload)


def test_an_invented_number_is_refused() -> None:
    projection = project(default_nodes())
    with pytest.raises(AnswerNotGroundedError) as refused:
        verify_grounding(projection, parse_read_result(valid_answer(answer="7 failing tests.")))
    assert refused.value.code == "number_not_in_evidence"
    with pytest.raises(AnswerNotGroundedError):
        verify_grounding(projection, parse_read_result(valid_answer(answer="3 failing tests in 2 modules.")))
