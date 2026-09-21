"""By source: what may change a page, and where (Milestone 8b S6).

S4 and S5 proved the worker contained **no** call that could change a control. S6 has to
contain exactly three, so the proof changes shape: instead of "none anywhere", it is "these
names, in this one file, and nothing else anywhere".

* The worker tree is scanned with a receiver-aware scanner (`find_worker_mutations`). Every
  file but `local_form_draft.py` must contain none of the action names; that file may contain
  the three reviewed write primitives and must still contain no click, key press, upload,
  submit, drag, hover, focus, dispatch or evaluate.
* The scanner is run against planted violations, so a scanner that quietly stopped matching
  would fail its own test.
* The planner vocabulary did not gain a write, the worker's routes are exactly the reviewed
  set, and nothing generic ("set the network mode", "run this script") exists.
"""

import re
from pathlib import Path

import pytest

from app.domain.authenticated import AuthenticatedRefusal, AuthOperation, parse_authenticated_step
from tests.form_source_scan import S6_PRIMITIVES, find_worker_mutations

ROOT = Path(__file__).resolve().parent.parent
BROWSER = ROOT / "app" / "browser"
LOCAL_DRAFT = "app/browser/local_form_draft.py"

#: Files that legitimately drive a page for *other* milestones' reasons, untouched by S6: the
#: booking adapter (PREPARE / CONSEQUENTIAL, its own approval and reconciliation) and the
#: public-research scroll (which presses keys on a public page, never an account page).
OTHER_MILESTONES = {
    "app/browser/adapters/appointment_fixture.py",
    "app/browser/operations/research.py",
}


def worker_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(BROWSER.rglob("*.py"))
    }


@pytest.mark.parametrize("path", sorted(p for p in worker_sources() if p not in OTHER_MILESTONES and p != LOCAL_DRAFT))
def test_no_other_worker_file_can_change_a_page(path: str) -> None:
    assert find_worker_mutations(worker_sources()[path]) == [], path


def test_the_local_draft_module_contains_only_the_reviewed_primitives() -> None:
    source = worker_sources()[LOCAL_DRAFT]
    # With the three primitives allowed, nothing else in the scanner's vocabulary appears.
    assert find_worker_mutations(source, allow=S6_PRIMITIVES) == []
    # And without the allowance the scanner does see them, so the allowance is doing real work.
    seen = {
        match.group(1)
        for hit in find_worker_mutations(source)
        if (match := re.search(r"\.(\w+)\(", hit)) is not None
    }
    assert seen == {"fill", "select_option", "set_checked", "check"}


@pytest.mark.parametrize(
    "path",
    [
        "app/services/form_draft.py",
        "app/services/form_state.py",
        "app/services/form_prepare.py",
        "app/repositories/form_drafts.py",
        "app/domain/local_form_draft.py",
    ],
)
def test_the_runtime_side_never_touches_a_page(path: str) -> None:
    """Durable orchestration sits above the worker and contains no page action at all."""
    assert find_worker_mutations((ROOT / path).read_text(encoding="utf-8")) == [], path


def test_the_local_draft_module_has_no_script_evaluation_of_its_own() -> None:
    source = worker_sources()[LOCAL_DRAFT]
    for forbidden in ("evaluate(", "evaluate_handle(", "eval_on_selector", "request_submit", "dispatch_event", "keyboard", "set_input_files"):
        assert forbidden not in source, forbidden


def test_the_dom_helper_is_still_static_and_read_only() -> None:
    from app.browser import form_observation
    from tests.form_source_scan import find_javascript_mutations

    assert find_javascript_mutations(form_observation._HELPER) == []
    for reviewed in ("resolve_member", "'state'"):
        assert reviewed in form_observation._HELPER


# ---- the scanner catches what it claims to catch --------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        "locator.click()", "page.keyboard.press('Enter')", "locator.press('Enter')",
        "locator.press_sequentially('x')", "locator.type('x')", "locator.dblclick()", "locator.tap()",
        "locator.set_input_files('f')", "a.drag_to(b)", "handle.dispatch_event('input')",
        "form.request_submit()", "locator.focus()", "locator.hover()", "locator.clear()",
        "page.mouse.click(1, 2)", "page.eval_on_selector('a', 'e => e.click()')",
        "member.check()", "handle.check()", "handle.set_checked(True)", "handle.fill('x')",
        "handle.select_option(index=1)",
    ],
)
def test_the_scanner_catches_planted_violations(call: str) -> None:
    source = f"async def f(page, locator, handle, form, a, b, member):\n    await {call}\n"
    assert find_worker_mutations(source)
    # A primitive is allowed in the one file that may hold it -- and nothing else is.
    is_primitive = any(f".{name}(" in call for name in S6_PRIMITIVES)
    assert bool(find_worker_mutations(source, allow=S6_PRIMITIVES)) == (not is_primitive)


@pytest.mark.parametrize(
    "harmless",
    ["self._policy.check(url)", "policy.check(url)", "self.tabs.clear()", "writer.write(b'x')", "counter.clear()"],
)
def test_the_scanner_ignores_ordinary_methods(harmless: str) -> None:
    assert find_worker_mutations(f"def f(self, policy, writer, counter, url):\n    {harmless}\n") == []


# ---- the vocabulary did not grow ----------------------------------------------------------------------


def test_the_planner_vocabulary_did_not_gain_a_write() -> None:
    assert {operation.value for operation in AuthOperation} == {"navigate", "observe", "reveal", "tab", "history"}
    for verb in ("fill", "type", "click", "focus", "press", "invoke", "submit", "upload", "clear", "javascript", "set_value", "select_option", "set_checked", "prepare_form", "handover_form"):
        with pytest.raises(AuthenticatedRefusal):
            parse_authenticated_step({"operation": verb, "tab": "t1"})


def test_the_worker_serves_exactly_the_reviewed_routes() -> None:
    source = worker_sources()["app/browser/worker.py"]
    routes = set(re.findall(r'@app\.(?:get|post)\("([^"]+)"', source))
    assert routes == {
        "/health",
        "/v1/sessions/open", "/v1/sessions/close",
        "/v1/profiles/open", "/v1/profiles/close",
        "/v1/profiles/takeover/start", "/v1/profiles/takeover/confirm",
        # Milestone 8b S6, and nothing generic beside them.
        "/v1/profiles/prepare-capture", "/v1/profiles/prepare-restore",
        "/v1/profiles/form-freeze", "/v1/profiles/form-discard", "/v1/profiles/form-handover",
        "/v1/dispatch",
    }


def test_no_generic_network_or_script_control_exists_anywhere() -> None:
    for path, source in worker_sources().items():
        for generic in ("setNetworkMode", "set_network_mode", "network_mode", "unfreeze", "run_script", "execute_script"):
            assert generic not in source, (path, generic)


def test_the_effect_and_tables_are_what_s6_says_they_are() -> None:
    from app.db.tables import metadata
    from app.domain.browser_dispatch import BrowserEffect

    assert BrowserEffect.LOCAL_DRAFT.value == "LOCAL_DRAFT"
    assert "form_drafts" in metadata.tables and "frozen_at" in metadata.tables["browser_dispatches"].c
    # No column anywhere is a place a raw value could be stored.
    forbidden = {"value", "text_value", "raw_value", "selector", "xpath", "locator", "html"}
    assert not (forbidden & {column.name for column in metadata.tables["form_drafts"].c})


def test_runtime_routes_added_by_s6_carry_ids_and_revisions_only() -> None:
    routes = (ROOT / "app/api/routes.py").read_text(encoding="utf-8")
    added = {
        "/tasks/{task_id}/authenticated/form/preparation-mode",
        "/tasks/{task_id}/authenticated/form/stop",
        "/form-drafts/{draft_id}/discard",
        "/form-drafts/{draft_id}/handover-request",
        "/actions/{action_id}/form-handover/approve",
        "/actions/{action_id}/form-handover/reject",
    }
    declared = set(re.findall(r'@router\.\w+\(\s*"([^"]+)"', routes))
    assert added <= declared
    # Their bodies are `DraftDecisionBody` / `DisclosureDecisionBody`: an expected revision.
    schemas = (ROOT / "app/api/form_prepare_schemas.py").read_text(encoding="utf-8")
    body = schemas.split("class DraftDecisionBody", 1)[1].split("class ", 1)[0]
    assert re.findall(r"^\s+(\w+):", body, re.M) == ["expected_revision"]
