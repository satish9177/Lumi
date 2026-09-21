"""No authenticated operation can change a field (Milestone 8b S4), by source.

**S4 observes form structure only. It cannot type, choose, check, click, upload
or submit anything.** The behavioural proof is `test_authenticated_forms_browser.py`
(zero events, zero submissions). This is the structural one: the production code
that observes forms and resolves element refs contains no call that could act on
a control, and the page-side helper assigns nothing but two local records it
builds itself.
"""

from pathlib import Path

import pytest

from app.browser import form_observation
from app.domain.authenticated import AuthOperation
from tests.form_source_scan import find_javascript_mutations, find_python_mutations

ROOT = Path(__file__).resolve().parent.parent
#: Everything that observes a form or resolves an element ref.
GUARDED = (
    "app/browser/form_observation.py",
    "app/browser/authenticated_session.py",
    "app/browser/operations/authenticated.py",
    "app/domain/authenticated_forms.py",
    "app/domain/authenticated.py",
    # Milestone 8b S5: form planning and the exact disclosure approval. They sit
    # above the worker and never reach it, so they contain no mutating call either.
    "app/domain/form_prepare.py",
    "app/domain/protected_values.py",
    "app/repositories/form_prepare.py",
    "app/services/form_prepare.py",
    "app/api/form_prepare_schemas.py",
)


@pytest.mark.parametrize("path", GUARDED)
def test_production_form_code_contains_no_mutating_call(path: str) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    assert find_python_mutations(source) == [], path


def test_the_page_helper_assigns_nothing_and_acts_on_nothing() -> None:
    assert find_javascript_mutations(form_observation._HELPER) == []


def test_the_page_helper_is_static_source() -> None:
    # It is a constant: nothing is interpolated at run time, and the request it
    # receives is data it reads, never code it runs.
    source = (ROOT / "app/browser/form_observation.py").read_text(encoding="utf-8")
    assert source.count("_HELPER") >= 3
    assert "f\"\"\"" not in source.split("_HELPER = r", 1)[1].split('"""', 2)[1]
    assert "evaluate(" not in (ROOT / "app/browser/operations/authenticated.py").read_text(encoding="utf-8")


def test_the_planner_vocabulary_is_unchanged() -> None:
    assert {operation.value for operation in AuthOperation} == {
        "navigate", "observe", "reveal", "tab", "history",
    }
    from app.domain.authenticated import parse_authenticated_step, AuthenticatedRefusal

    for proposal in (
        {"operation": "prepare_form"},
        {"operation": "set_value", "tab": "t1"},
        {"operation": "click", "tab": "t1"},
        {"operation": "focus", "tab": "t1"},
        {"operation": "type", "tab": "t1"},
        {"operation": "reveal", "tab": "t1", "target": {"kind": "element", "observation": "o1", "ref": "e1"}},
    ):
        with pytest.raises(AuthenticatedRefusal):
            parse_authenticated_step(proposal)


# ---- the scanner itself: it must catch what it claims to catch ---------------------------


@pytest.mark.parametrize(
    "call",
    [
        "locator.fill('x')", "locator.type('x')", "page.keyboard.press('Enter')",
        "locator.press('Enter')", "locator.press_sequentially('x')", "locator.click()",
        "locator.dblclick()", "locator.tap()", "locator.check()", "locator.uncheck()",
        "locator.select_option('a')", "locator.set_input_files('f')", "a.drag_to(b)",
        "handle.dispatch_event('input')", "form.request_submit()", "locator.focus()",
        "locator.hover()", "locator.clear()", "locator.set_checked(True)", "page.mouse.click(1, 2)",
        "page.eval_on_selector('a', 'e => e.click()')",
    ],
)
def test_the_python_scanner_catches_planted_mutations(call: str) -> None:
    assert find_python_mutations(f"async def f(page, locator, handle, form, a, b):\n    await {call}\n")


def test_the_python_scanner_ignores_words_in_docstrings_and_comments() -> None:
    source = '"""It cannot .click( or .fill( anything."""\n# locator.type("x")\nx = "press"\n'
    assert find_python_mutations(source) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "el.value = 'x';", "el.checked = true;", "el.innerHTML = '';", "el.click();", "el.focus();",
        "el.dispatchEvent(new Event('input'));", "form.submit();", "form.requestSubmit();",
        "el.setAttribute('a', 'b');", "el.blur();", "fetch('/x');", "el.value += 'a';",
        "el.selectedIndex = 1;", "el.remove();", "el.scrollIntoView();",
        "const a = el.value; const b = el.value;",
    ],
)
def test_the_javascript_scanner_catches_planted_mutations(snippet: str) -> None:
    assert find_javascript_mutations(f"(r) => {{ const hasValue = (el) => el.value.length; {snippet} }}")


def test_the_javascript_scanner_accepts_the_reviewed_shape() -> None:
    reviewed = (
        "(r) => { // el.click() in a comment is not code\n"
        "  const hasValue = (el) => el.value.length > 0;\n"
        "  const out = {}; out.required = true; target.x = 1; return out; }"
    )
    assert find_javascript_mutations(reviewed) == []


# ---- Milestone 8b S5 asserted that no S6 shape existed. S6 exists now, and what replaces that ----
# ---- assertion is `tests/test_local_form_draft_source.py`: exactly which names may exist, where. ----
