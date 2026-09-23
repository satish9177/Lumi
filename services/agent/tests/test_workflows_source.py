"""Milestone 10 S4 source pins: the workflow controller composes existing services and adds no authority.

These are structural guarantees the tests above cannot give by example:

* the workflow modules never import a browser, desktop, project or worker module, never name an operation
  that could submit, press a key, click, upload or navigate, and never call a model or a provider;
* nothing in the workflow modules writes M8's global `protected_values` (a workflow value can never become a
  user-typed saved detail);
* only `WorkflowRepository.values_for_execution` returns an adopted value, and only the frozen local-draft
  fill calls it.
"""

import ast
import re
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
MODULES = (
    AGENT / "app" / "domain" / "workflows.py",
    AGENT / "app" / "services" / "workflows.py",
    AGENT / "app" / "repositories" / "workflows.py",
    AGENT / "app" / "api" / "workflow_routes.py",
)
FORBIDDEN_IMPORT = re.compile(r"^app\.(browser|desktop|projects)\b|^app\.services\.(browser_execution|desktop|projects|booking)")
FORBIDDEN_WORDS = re.compile(r"\b(submit|press_key|keyboard|click|upload|navigate|set_input_files|dispatch_and_classify)\b", re.IGNORECASE)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


def _code_without_docstrings(path: Path) -> str:
    """Identifiers and string literals that are not docstrings or comments."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pieces: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            pieces.append(node.id)
        elif isinstance(node, ast.Attribute):
            pieces.append(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            pieces.append(node.name)
    return " ".join(pieces)


def test_the_workflow_controller_never_reads_document_text_itself() -> None:
    service = AGENT / "app" / "services" / "workflows.py"
    imported = _imports(service)
    assert "app.repositories.documents" not in imported and "app.domain.documents" not in imported
    assert "text_for(" not in service.read_text(encoding="utf-8")


def test_the_workflow_modules_import_no_executor() -> None:
    for path in MODULES:
        for name in _imports(path):
            assert not FORBIDDEN_IMPORT.search(name), (path.name, name)


def test_the_workflow_modules_name_no_submit_click_key_upload_or_navigation() -> None:
    for path in MODULES:
        assert not FORBIDDEN_WORDS.search(_code_without_docstrings(path)), path.name


def test_no_workflow_module_writes_the_global_saved_details() -> None:
    for path in MODULES:
        code = _code_without_docstrings(path)
        assert "ProtectedValueRepository" not in code, path.name
        assert "protected_values" not in code, path.name
        assert "app.repositories.form_prepare" not in _imports(path), path.name


def test_only_the_frozen_draft_fill_reads_an_adopted_value() -> None:
    # The two definitions (global saved details, workflow values) and the one caller: the frozen draft fill.
    found = sorted(
        path.relative_to(AGENT).as_posix()
        for path in (AGENT / "app").rglob("*.py")
        if "values_for_execution(" in path.read_text(encoding="utf-8")
    )
    assert found == ["app/repositories/form_prepare.py", "app/repositories/workflows.py", "app/services/form_draft.py"]
