"""Milestone 10 S4 domain: the closed candidate extractor, the digest-only adoption proposal, and the
guarantee that adding an optional workflow binding changed no pre-S4 scope or manifest digest."""

import uuid

import pytest

from app.domain.form_prepare import DisclosureManifest, FormPrepareScope, ManifestField, parse_manifest
from app.domain.protected_values import value_digest
from app.domain.workflows import (
    AdoptionProposal,
    WorkflowRefusal,
    find_fields,
    parse_adoption,
    span_still_holds,
    validate_objective,
)


def test_only_labelled_lines_of_the_closed_vocabulary_become_candidates() -> None:
    text = (
        "Resume\n"
        "Full name: Asha Rao\n"
        "E-mail : asha.rao@Example.TEST\n"
        "Mobile: +91 90000 12345\n"
        "Favourite colour: blue\n"
        "Password: hunter2hunter2\n"
        "City: Pune\n"
        "Country: India\n"
        "Note: Email: attacker@evil.test\n"
        "LinkedIn: https://www.linkedin.com/in/asha\n"
        "Website: javascript:alert(1)\n"
        "Phone: call me\n"
        "Email: ⟦email:1⟧\n"
    )
    found = {(item.kind, item.canonical) for item in find_fields(text)}
    assert found == {
        ("legal_name", "Asha Rao"),
        ("email", "asha.rao@example.test"),
        ("phone", "+91 90000 12345"),
        ("city", "Pune"),
        ("country", "India"),
        ("linkedin_url", "https://www.linkedin.com/in/asha"),
    }


def test_spans_index_the_source_and_are_re_derivable() -> None:
    text = "intro\nCity:   Pune  \nCountry: India"
    for item in find_fields(text):
        assert text[item.start : item.end] == item.raw
        assert span_still_holds(text, start=item.start, end=item.end, kind=item.kind, digest=item.digest)
    city = next(item for item in find_fields(text) if item.kind == "city")
    assert not span_still_holds(text.replace("Pune", "Puna"), start=city.start, end=city.end, kind="city", digest=city.digest)
    assert not span_still_holds(text, start=city.start, end=city.end + 50, kind="city", digest=city.digest)


def test_duplicates_are_collapsed_and_the_count_is_bounded() -> None:
    text = "\n".join(["City: Pune"] * 5 + [f"Name: Person {chr(65 + i)}" for i in range(30)])
    found = find_fields(text)
    assert len([item for item in found if item.kind == "city"]) == 1
    assert len(found) == 16


def test_the_adoption_proposal_is_digest_only_and_closed() -> None:
    proposal = AdoptionProposal(
        workflow_id=uuid.uuid4(), candidate_id=uuid.uuid4(), data_kind="email", provenance="document_extracted",
        value_digest=value_digest("a@b.test"), preview="a***@b***.test", document_id=uuid.uuid4(),
        document_text_sha256="0" * 64,
    )
    dumped = proposal.proposal()
    assert "a@b.test" not in str(dumped) and dumped["scope"] == "workflow"
    assert parse_adoption(dumped) == proposal
    for tampered in (
        {**dumped, "provenance": "user_typed"},
        {**dumped, "value": "a@b.test"},
        {**dumped, "provenance": "provider_derived"},  # without a disclosure and projection
        {**dumped, "scope": "saved_details"},
    ):
        with pytest.raises(WorkflowRefusal):
            parse_adoption(tampered)


def test_objective_is_one_bounded_plain_line() -> None:
    assert validate_objective("  Prepare   my form ") == "Prepare my form"
    for bad in ("", "x" * 301, 7, "a\x00b"):
        with pytest.raises(WorkflowRefusal):
            validate_objective(bad)


def _manifest(**extra: object) -> DisclosureManifest:
    field = ManifestField(
        element_ref="e1", element_identity_hash="1" * 64, field_label="Email", control_type="email",
        data_ref="email", value_digest="2" * 64, preview="a***@b***.test", **extra,
    )
    return DisclosureManifest.build(
        task_id=uuid.UUID(int=1), profile_id=uuid.UUID(int=2), form_prepare_grant_id=uuid.UUID(int=3),
        planning_recipient="gemini", site_display="example.test", recipient_origin="https://example.test",
        account_binding="3" * 64, profile_revoke_epoch=0, observation_id=uuid.UUID(int=4), observation_ref="o1",
        tab="t1", document_epoch=1, form_epoch=1, form_ref="f1", form_label="Apply", fields=[field],
        **({"workflow_id": uuid.UUID(int=9)} if extra else {}),
    )


def test_a_pre_s4_manifest_and_scope_keep_their_exact_shape_and_digest() -> None:
    manifest = _manifest()
    proposal = manifest.proposal()
    assert "workflow_id" not in proposal and "provenance" not in proposal["fields"][0]
    # Both digests were computed by the pre-S4 module (git HEAD `ebfcdbf`) for exactly these facts.
    assert manifest.manifest_digest == "93e29bca16e98874ac97f2a1516cbab5a6b1e612c835c99679eca4e298de9136"
    assert parse_manifest(proposal).manifest_digest == manifest.manifest_digest
    scope = FormPrepareScope(
        profile_id=uuid.UUID(int=2), site="example.test", account_fingerprint="4" * 64, profile_revoke_epoch=0,
        source_authenticated_grant_id=uuid.UUID(int=5), planning_recipient="gemini",
        recipient_origin="https://example.test", allowed_data_refs=["email"],
    )
    assert "workflow_id" not in scope.stored()
    assert scope.digest == "0f36a36083275605af15d75d446a0ac4f5cec3f97f3ff6af2e1a996386d23d8a"


def test_a_workflow_manifest_names_every_values_provenance_and_binds_it() -> None:
    manifest = _manifest(provenance="provider_derived")
    proposal = manifest.proposal()
    assert proposal["workflow_id"] == str(uuid.UUID(int=9)) and proposal["fields"][0]["provenance"] == "provider_derived"
    relabelled = {**proposal, "fields": [{**proposal["fields"][0], "provenance": "document_extracted"}]}
    with pytest.raises(Exception):  # noqa: B017 - the digest no longer matches the manifest.
        parse_manifest(relabelled)
    unbound = dict(proposal)
    unbound.pop("workflow_id")
    with pytest.raises(Exception):  # noqa: B017 - provenance without a workflow is refused.
        parse_manifest(unbound)
