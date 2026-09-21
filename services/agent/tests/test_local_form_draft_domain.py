"""The S6 vocabulary: the strict worker input, the safe result, the draft digest, statuses."""

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.form_prepare import (
    MANIFEST_POLICY_VERSION,
    DisclosureManifest,
    ManifestField,
    parse_manifest,
)
from app.domain.local_form_draft import (
    DraftFieldRecord,
    DraftStatus,
    FillFieldInput,
    FillFormInput,
    FillResult,
    HandoverProposal,
    VerifiedField,
    check_verified_hash,
    choice_verified_hash,
    draft_digest,
    text_verified_hash,
)

DIGEST = "a" * 64


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def text_field(**over: object) -> dict[str, object]:
    return {
        "element_ref": "e1", "element_identity_hash": DIGEST, "control_type": "text",
        "text_value": "hello", "value_digest": sha("hello"), **over,
    }


def form(*fields: dict[str, object] | dict[str, str]) -> dict[str, object]:
    return {
        "site": "example.test", "expected_account_fingerprint": DIGEST, "observation_id": str(uuid.uuid4()),
        "tab": "t1", "document_epoch": 1, "form_epoch": 1, "form_ref": "f1", "manifest_digest": DIGEST,
        "fields": list(fields),
    }


def test_the_execution_input_carries_exactly_one_variant_per_control() -> None:
    assert FillFormInput.model_validate(form(text_field()))
    choice = {"element_ref": "e2", "element_identity_hash": DIGEST, "control_type": "select_single", "option_ref": "op1", "option_identity_hash": DIGEST}
    check = {"element_ref": "e3", "element_identity_hash": DIGEST, "control_type": "checkbox", "checked": False}
    assert len(FillFormInput.model_validate(form(text_field(), choice, check)).fields) == 3
    for bad in (
        text_field(option_ref="op1", option_identity_hash=DIGEST),
        text_field(checked=True),
        {**choice, "checked": True},
        {"element_ref": "e3", "element_identity_hash": DIGEST, "control_type": "checkbox"},
        {**text_field(), "control_type": "button"},
        {**text_field(), "control_type": "password"},
        {**text_field(), "control_type": "file"},
        {**text_field(), "control_type": "submit_like"},
        {**text_field(), "control_type": "select_multi"},
    ):
        with pytest.raises(ValidationError):
            FillFormInput.model_validate(form(bad))


def test_the_worker_refuses_a_value_that_is_not_the_one_that_was_approved() -> None:
    with pytest.raises(ValidationError):
        FillFormInput.model_validate(form(text_field(text_value="a different value")))


@pytest.mark.parametrize(
    "extra",
    ["selector", "xpath", "url", "origin", "javascript", "script", "dom_id", "name", "class", "button", "submit", "file_path"],
)
def test_no_selector_url_script_or_submit_target_can_be_smuggled_in(extra: str) -> None:
    with pytest.raises(ValidationError):
        FillFormInput.model_validate({**form(text_field()), extra: "x"})
    with pytest.raises(ValidationError):
        FillFormInput.model_validate(form({**text_field(), extra: "x"}))


def test_bounds_are_hard() -> None:
    twelve = [text_field(element_ref=f"e{n}") for n in range(1, 13)]
    assert FillFormInput.model_validate(form(*twelve))
    with pytest.raises(ValidationError):
        FillFormInput.model_validate(form(*[text_field(element_ref=f"e{n}") for n in range(1, 14)]))
    with pytest.raises(ValidationError):
        FillFormInput.model_validate(form())
    with pytest.raises(ValidationError):
        FillFormInput.model_validate(form(text_field(), text_field()))


def test_a_raw_value_never_appears_in_a_repr() -> None:
    secret = "SECRET_VALUE_XYZ"
    field = text_field(text_value=secret, value_digest=sha(secret))
    assert secret not in repr(FillFieldInput.model_validate(field))
    assert secret not in str(FillFormInput.model_validate(form(field)))


def test_the_result_is_consistent_and_carries_no_value() -> None:
    verified = [VerifiedField(element_ref="e1", verified_local_value_hash=DIGEST)]
    ok = FillResult(draft_complete=True, fields_attempted=1, fields_verified=1, verified_fields=verified, dirty=True)
    assert ok.model_dump().keys() >= {"draft_complete", "fields_attempted", "fields_verified", "verified_fields", "error_code", "dirty"}
    for bad in (
        dict(draft_complete=True, fields_attempted=1, fields_verified=0, verified_fields=[], dirty=True),
        dict(draft_complete=True, fields_attempted=1, fields_verified=1, verified_fields=verified, dirty=False),
        dict(draft_complete=False, fields_attempted=1, fields_verified=0),
        dict(draft_complete=False, fields_attempted=1, fields_verified=2, verified_fields=verified * 2, error_code="x"),
    ):
        with pytest.raises(ValidationError):
            FillResult(**bad)
    with pytest.raises(ValidationError):
        FillResult(draft_complete=False, fields_attempted=0, fields_verified=0, error_code="not a code!")


def record(ref: str, verified: str) -> DraftFieldRecord:
    return DraftFieldRecord(
        element_ref=ref, element_identity_hash=DIGEST, control_type="text", data_ref="email",
        value_digest=DIGEST, verified_local_value_hash=verified, written_at=datetime.now(UTC),
    )


def digest(fields: list[DraftFieldRecord], **over: object) -> str:
    base: dict[str, object] = dict(
        task_id=uuid.UUID(int=1), profile_id=uuid.UUID(int=2), action_id=uuid.UUID(int=3),
        manifest_digest=DIGEST, dispatch_id=uuid.UUID(int=4), observation_id=uuid.UUID(int=5), tab="t1",
        document_epoch=1, form_epoch=1, form_ref="f1", status=DraftStatus.PREPARED, fields=fields,
    )
    return draft_digest(**{**base, **over})  # type: ignore[arg-type]


def test_the_draft_digest_binds_everything_and_no_raw_value() -> None:
    a, b = record("e1", sha("x")), record("e2", sha("y"))
    base = digest([a, b])
    assert digest([b, a]) == base
    assert digest([a.model_copy(update={"written_at": datetime(2020, 1, 1, tzinfo=UTC)}), b]) == base
    changed = {
        "verified hash": digest([record("e1", sha("z")), b]),
        "a field": digest([a]),
        "status": digest([a, b], status=DraftStatus.STALE),
        "task": digest([a, b], task_id=uuid.UUID(int=9)),
        "profile": digest([a, b], profile_id=uuid.UUID(int=9)),
        "action": digest([a, b], action_id=uuid.UUID(int=9)),
        "dispatch": digest([a, b], dispatch_id=uuid.UUID(int=9)),
        "manifest": digest([a, b], manifest_digest="c" * 64),
        "observation": digest([a, b], observation_id=uuid.UUID(int=9)),
        "document epoch": digest([a, b], document_epoch=2),
        "form epoch": digest([a, b], form_epoch=2),
        "form": digest([a, b], form_ref="f2"),
        "tab": digest([a, b], tab="t2"),
    }
    assert base not in changed.values() and len(set(changed.values())) == len(changed), changed


def test_verified_hashes_are_stable_and_distinguish_states() -> None:
    assert text_verified_hash("abc") == sha("abc")
    assert check_verified_hash(DIGEST, True) != check_verified_hash(DIGEST, False)
    assert choice_verified_hash(DIGEST) != choice_verified_hash("b" * 64)


def test_the_draft_status_set_is_closed() -> None:
    assert {status.value for status in DraftStatus} == {"PREPARED", "STALE", "DISCARDED", "HANDED_OVER"}


def test_the_handover_proposal_is_safe_references_only() -> None:
    proposal = HandoverProposal(
        task_id=uuid.uuid4(), profile_id=uuid.uuid4(), draft_id=uuid.uuid4(), draft_digest=DIGEST,
        site_display="example.test", field_count=3, partial=False,
    )
    assert set(proposal.model_dump()) == {
        "schema_version", "kind", "policy_version", "classification", "task_id", "profile_id",
        "draft_id", "draft_digest", "site_display", "field_count", "partial",
    }
    with pytest.raises(ValidationError):
        HandoverProposal.model_validate({**proposal.model_dump(mode="json"), "value": "x"})


def test_new_manifests_are_v2_and_a_v1_manifest_still_parses_but_is_a_different_thing() -> None:
    field = ManifestField(element_ref="e1", element_identity_hash=DIGEST, field_label="x", control_type="checkbox", checked=True)
    common = dict(
        task_id=uuid.uuid4(), profile_id=uuid.uuid4(), form_prepare_grant_id=uuid.uuid4(),
        planning_recipient="gemini", site_display="example.test", recipient_origin="https://example.test",
        account_binding=DIGEST, profile_revoke_epoch=0, observation_id=uuid.uuid4(), observation_ref="o1",
        tab="t1", document_epoch=1, form_epoch=1, form_ref="f1", form_label=None, fields=[field],
    )
    current = DisclosureManifest.build(**common)
    legacy = DisclosureManifest.build(**common, policy_version="form-prepare-v1")
    assert current.policy_version == MANIFEST_POLICY_VERSION == "form-prepare-v2"
    assert legacy.policy_version == "form-prepare-v1" and legacy.manifest_digest != current.manifest_digest
    assert parse_manifest(legacy.proposal()).policy_version == "form-prepare-v1"
