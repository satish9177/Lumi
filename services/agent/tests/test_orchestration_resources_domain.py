"""Milestone 12 S1: the closed resource-ref vocabulary (`app/domain/orchestration_resources.py`).

No database here -- pure-function properties only, matching `test_orchestration_domain.py`'s own style:
bounded validation, a closed kind vocabulary that matches the TypeScript/migration spellings, and a
capability x resource compatibility matrix that is honestly empty for every capability this runtime has
actually composed.
"""

import pytest

from app.domain.orchestration import COMPOSED_CAPABILITY_IDS, OrchestrationRefusal
from app.domain.orchestration_resources import (
    CAPABILITY_OUTPUT_RESOURCE,
    CAPABILITY_RESOURCE_REQUIREMENTS,
    MAX_RESOURCES_PER_STEP,
    MAX_SAFE_LABEL_CHARS,
    PRIVACY_CLASSES,
    RESOURCE_KINDS,
    RESOURCE_STATE_CODES,
    REGISTERABLE_RESOURCE_KINDS,
    next_ref,
    safe_label,
    validate_ref,
    validate_resource_refs,
)

# Pinned identically to migration 0023's own `_RESOURCE_KINDS` and to `app/db/tables.py`'s
# `_ORCHESTRATION_RESOURCE_KINDS`, and to `src/main/services/orchestration-wire.ts`'s `RESOURCE_KINDS`. The
# spellings cannot literally share a source file; this test is the pin.
TS_AND_MIGRATION_RESOURCE_KINDS = frozenset(
    {
        "public_url_ref", "research_result_ref",
        "account_context_ref", "account_result_ref",
        "document_ref", "document_result_ref", "transfer_ref",
        "desktop_target_ref", "desktop_snapshot_ref", "desktop_result_ref",
        "app_ref", "project_ref", "project_status_ref",
        "form_target_ref", "form_result_ref", "workflow_ref",
    }
)


def test_the_resource_kinds_match_the_migration_and_typescript_spelling_exactly() -> None:
    assert RESOURCE_KINDS == TS_AND_MIGRATION_RESOURCE_KINDS


def test_every_output_and_requirement_entry_names_only_a_composed_capability() -> None:
    # A capability absent from COMPOSED_CAPABILITY_IDS mints nothing and requires nothing here: this
    # module never gets ahead of what the runtime has actually reviewed and composed.
    assert set(CAPABILITY_OUTPUT_RESOURCE) <= COMPOSED_CAPABILITY_IDS
    assert set(CAPABILITY_RESOURCE_REQUIREMENTS) <= COMPOSED_CAPABILITY_IDS


def test_every_output_kind_and_privacy_class_is_real() -> None:
    for spec in CAPABILITY_OUTPUT_RESOURCE.values():
        assert spec.kind in RESOURCE_KINDS
        assert spec.privacy_class in PRIVACY_CLASSES


def test_the_three_m11_capabilities_still_accept_no_resource() -> None:
    # public_research/project_status/project_start were composed before the resource registry existed
    # (Milestone 11) and still take no resource input as of Milestone 12 S2 -- unchanged.
    for capability_id in ("public_research", "project_status", "project_start"):
        assert CAPABILITY_RESOURCE_REQUIREMENTS[capability_id] == ()


def test_document_capabilities_require_exactly_the_kinds_document_service_needs() -> None:
    # Milestone 12 S2's own invariant, now that a real consumer exists: the requirement is closed, ordered
    # and exact -- never inferred from what happens to be available.
    assert CAPABILITY_RESOURCE_REQUIREMENTS["document_read"] == ("document_ref",)
    assert CAPABILITY_RESOURCE_REQUIREMENTS["document_compare"] == ("document_result_ref", "document_result_ref")


def test_m12_s5_form_and_workflow_refs_remain_unavailable_without_trusted_lineage() -> None:
    # M8 form planning consumes saved details or values adopted inside its own M10 workflow.
    # A standalone M12 document_result_ref is neither source. M10 workflow creation also
    # needs a URL/destination bundle from trusted UI, which the M12 planner cannot invent.
    # Keep these kinds visible vocabulary only until those exact bridges are implemented.
    assert {"form_prepare", "workflow_prepare"}.isdisjoint(COMPOSED_CAPABILITY_IDS)
    assert {"form_prepare", "workflow_prepare"}.isdisjoint(CAPABILITY_RESOURCE_REQUIREMENTS)
    assert {"form_prepare", "workflow_prepare"}.isdisjoint(CAPABILITY_OUTPUT_RESOURCE)
    assert {"form_target_ref", "form_result_ref", "workflow_ref"}.isdisjoint(REGISTERABLE_RESOURCE_KINDS)


def test_validate_ref_accepts_only_the_opaque_shape() -> None:
    assert validate_ref("r1") == "r1"
    assert validate_ref("r42") == "r42"
    for bad in ("r0", "R1", "r", "r01", "1", "00000000-0000-4000-8000-000000000001", "r1 ", "", None, 42):
        with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
            validate_ref(bad)


def test_validate_resource_refs_accepts_none_as_no_citation() -> None:
    assert validate_resource_refs(None) == ()


def test_validate_resource_refs_bounds_count_and_shape_and_forbids_duplicates() -> None:
    assert validate_resource_refs(["r1", "r2"]) == ("r1", "r2")
    with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
        validate_resource_refs([f"r{i}" for i in range(1, MAX_RESOURCES_PER_STEP + 2)])
    with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
        validate_resource_refs(["r1", "r1"])
    with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
        validate_resource_refs("r1")  # a bare string is not a list
    with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
        validate_resource_refs(["r1", "00000000-0000-4000-8000-000000000001"])


def test_safe_label_bounds_and_neutralises_control_characters() -> None:
    assert safe_label("public_research result (step 1)") == "public_research result (step 1)"
    assert safe_label("a\x00b") == "a b"
    text = "x" * (MAX_SAFE_LABEL_CHARS + 50)
    bounded = safe_label(text)
    assert len(bounded) == MAX_SAFE_LABEL_CHARS
    assert bounded.endswith("…")


def test_next_ref_is_deterministic_and_never_model_chosen() -> None:
    assert next_ref(0) == "r1"
    assert next_ref(1) == "r2"
    assert next_ref(41) == "r42"


def test_resource_state_codes_are_a_real_subset_of_stable_refusal_codes() -> None:
    assert RESOURCE_STATE_CODES == {
        "resource_not_found", "resource_consumed", "resource_expired", "desktop_target_unavailable",
        "project_run_not_live"
    }
    # resources_not_supported / resources_invalid / resource_kind_mismatch are deliberately NOT state codes
    # (422, a static shape/policy mismatch), matching orchestration.py's own task_kind_mismatch convention.
    assert "resources_not_supported" not in RESOURCE_STATE_CODES
    assert "resource_kind_mismatch" not in RESOURCE_STATE_CODES
    assert "resources_invalid" not in RESOURCE_STATE_CODES
