"""Milestone 11 S2: the closed orchestration vocabulary (`app/domain/orchestration.py`).

No database here -- these are the same pure-function properties `test_workflows_domain.py` and
`test_research_authorization.py`'s own domain modules pin: bounded input validation, a closed capability
catalog that matches the TypeScript catalog's own spelling, and a strict subset of it actually composed.
"""

from app.domain.orchestration import (
    CATALOG_CAPABILITY_IDS,
    COMPOSED_CAPABILITY_IDS,
    EXPECTED_TASK_TYPE,
    MAX_RESULT_SUMMARY_CHARS,
    PAUSE_REASONS,
    STATE_CODES,
    SYNCHRONOUS_CAPABILITY_IDS,
    TASK_BACKED_CAPABILITY_IDS,
    OrchestrationRefusal,
    bounded_summary,
    result_handle,
    validate_capability_id,
    validate_objective,
)

# Pinned identically to src/shared/agent-capabilities.ts's AGENT_CAPABILITY_IDS (Milestone 11 S1) and to
# migration 0021's own _CAPABILITY_IDS / db/tables.py's _ORCHESTRATION_CAPABILITY_IDS. The four spellings
# cannot literally share a source file (two languages, a migration, a live schema); this test is the pin.
TS_CATALOG_IDS = frozenset(
    {
        "public_research", "inspect_public_page", "account_read", "document_read", "document_compare",
        "download_document", "place_downloaded_file", "desktop_observe", "desktop_reason",
        "desktop_safe_action", "launch_registered_app", "project_status", "project_start", "project_stop",
        "form_prepare", "workflow_prepare",
    }
)

FORBIDDEN_ID_FRAGMENTS = (
    "shell", "cmd", "powershell", "terminal", "exec", "command", "subprocess",
    "arbitrary_file", "generic_file", "file_write", "recursive_delete", "delete_file",
    "upload", "submit", "send_message", "purchase", "payment", "pay",
    "git", "install", "dependency",
    "mouse", "keyboard", "coordinate", "click", "hotkey", "drag",
    "set_value", "select_control", "invoke_control",
)


def test_the_catalog_matches_the_typescript_catalog_exactly() -> None:
    assert CATALOG_CAPABILITY_IDS == TS_CATALOG_IDS


def test_no_catalog_id_is_a_no_go_capability() -> None:
    for capability_id in CATALOG_CAPABILITY_IDS:
        for forbidden in FORBIDDEN_ID_FRAGMENTS:
            assert forbidden not in capability_id, capability_id


def test_composed_capabilities_are_a_strict_subset_of_the_full_catalog() -> None:
    assert COMPOSED_CAPABILITY_IDS <= CATALOG_CAPABILITY_IDS
    assert COMPOSED_CAPABILITY_IDS < CATALOG_CAPABILITY_IDS  # honestly narrower, not the whole catalog


def test_task_backed_and_synchronous_capabilities_are_disjoint_and_cover_composed() -> None:
    assert TASK_BACKED_CAPABILITY_IDS & SYNCHRONOUS_CAPABILITY_IDS == frozenset()
    assert TASK_BACKED_CAPABILITY_IDS | SYNCHRONOUS_CAPABILITY_IDS == COMPOSED_CAPABILITY_IDS


def test_every_task_backed_capability_has_an_expected_task_type() -> None:
    for capability_id in TASK_BACKED_CAPABILITY_IDS:
        assert capability_id in EXPECTED_TASK_TYPE


def test_validate_objective_accepts_a_bounded_plain_string() -> None:
    assert validate_objective("  Research   the Lumi repository  ") == "Research the Lumi repository"


def test_validate_objective_refuses_empty_overlong_or_control_characters() -> None:
    for bad in ("", "   ", "x" * 501, "hello\x00world", 42, None):
        try:
            validate_objective(bad)
        except OrchestrationRefusal as refusal:
            assert refusal.code == "objective_invalid"
        else:
            raise AssertionError(f"{bad!r} should have been refused")


def test_validate_capability_id_accepts_only_exact_catalog_membership() -> None:
    assert validate_capability_id("public_research") == "public_research"
    for bad in ("Public_Research", "public_research ", "run_shell", "", 42, None):
        try:
            validate_capability_id(bad)
        except OrchestrationRefusal as refusal:
            assert refusal.code == "capability_unknown"
        else:
            raise AssertionError(f"{bad!r} should have been refused")


def test_bounded_summary_normalises_whitespace_and_neutralises_control_characters() -> None:
    assert bounded_summary("  answered:   Lumi is  \n a desktop companion  ") == "answered: Lumi is a desktop companion"
    # Substituted with a space, not silently removed -- the same convention context-builder.ts's own
    # `clip()` uses, so a control character can never splice two words together unexpectedly.
    assert bounded_summary("a\x00b\x1fc") == "a b c"


def test_bounded_summary_truncates_at_the_bound() -> None:
    text = "x" * (MAX_RESULT_SUMMARY_CHARS + 50)
    summary = bounded_summary(text)
    assert len(summary) == MAX_RESULT_SUMMARY_CHARS
    assert summary.endswith("…")


def test_result_handle_is_deterministic_and_never_model_chosen() -> None:
    assert result_handle("research_result", 3) == "research_result:3"
    assert result_handle("project_status", 1) == "project_status:1"


def test_state_codes_and_pause_reasons_are_disjoint_closed_vocabularies() -> None:
    assert STATE_CODES.isdisjoint(PAUSE_REASONS)
    assert all(isinstance(code, str) and code for code in STATE_CODES)
    assert all(isinstance(reason, str) and reason for reason in PAUSE_REASONS)


def test_pause_reasons_matches_the_migration_0022_widened_set() -> None:
    # Pinned against the exact set migration 0022 widens `ck_orchestrations_pause_reason_closed` to.
    assert PAUSE_REASONS == {
        "approval_required", "budget_exhausted", "loop_detected", "capability_unavailable",
        "manual_handoff_required", "outcome_unknown",
    }
