"""Milestone 10 S1: source-level checks for the document and file-broker code.

A tripwire, not a proof: it pins what these modules may import and call, so a later edit that gives the
extraction helper network access, lets any module other than the document service read extracted text, or
gives the file broker a write/delete verb shows up as a failing test instead of as a quiet capability.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

#: The only modules that may import the extracted-text repository.
_TEXT_READERS = {"services/documents.py"}
#: What nothing in `app/documents` or `app/files` may import.
_NETWORK_OR_EXEC = {
    "socket", "ssl", "http", "urllib", "httpx", "requests", "ftplib", "smtplib", "webbrowser", "ctypes.util",
    "multiprocessing", "asyncio.subprocess", "playwright", "win32com", "comtypes", "pywinauto",
}
#: Verbs that would turn the read-only broker into a writer. S1 has no file mutation at all.
_OS_WRITE_CALLS = {"remove", "unlink", "rmdir", "rename", "replace", "makedirs", "mkdir", "startfile", "chmod", "truncate", "link", "symlink", "removedirs", "renames", "open"}
_SHUTIL_CALLS = {"rmtree", "move", "copy", "copy2", "copyfile", "copytree", "copyfileobj"}
_PATH_WRITE_CALLS = {"write_bytes", "write_text", "unlink", "rmdir", "touch", "symlink_to", "hardlink_to", "mkdir"}


def _modules(folder: str) -> list[Path]:
    return sorted((APP / folder).rglob("*.py"))


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_extraction_and_the_file_broker_have_no_network_or_automation_imports() -> None:
    for path in [*_modules("documents"), *_modules("files")]:
        imported = _imports(ast.parse(path.read_text(encoding="utf-8")))
        bad = {name for name in imported if name.split(".")[0] in _NETWORK_OR_EXEC or name in _NETWORK_OR_EXEC}
        assert not bad, f"{path.name} imports {sorted(bad)}"


def test_the_extraction_helper_imports_only_the_standard_library_and_its_own_package() -> None:
    allowed_roots = {"argparse", "json", "sys", "io", "re", "zlib", "zipfile", "base64", "codecs", "unicodedata",
                     "collections", "dataclasses", "typing", "xml", "app"}
    for name in ("helper.py", "pdf.py", "docx.py", "text.py", "sniff.py", "limits.py", "errors.py"):
        imported = _imports(ast.parse((APP / "documents" / name).read_text(encoding="utf-8")))
        for module in imported:
            assert module.split(".")[0] in allowed_roots, f"{name} imports {module}"
            if module.startswith("app."):
                assert module.startswith("app.documents."), f"{name} imports {module}"


#: The only two S2 writers, each pinned by its own test below.
_S2_WRITERS = {"quarantine.py", "place.py"}


def test_the_file_broker_has_no_write_rename_or_delete_verb() -> None:
    for path in [*_modules("files"), *_modules("documents")]:
        if path.parent.name == "files" and path.name in _S2_WRITERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            where = f"{path.name}:{node.lineno}"
            if isinstance(node.func, ast.Attribute):
                base = node.func.value.id if isinstance(node.func.value, ast.Name) else None
                # `os.open` is allowed only as the read-only `open_read` in handles.py (checked below).
                if base == "os" and node.func.attr in _OS_WRITE_CALLS and not (path.name == "handles.py" and node.func.attr == "open"):
                    raise AssertionError(f"{where} calls os.{node.func.attr}")
                assert not (base == "shutil" and node.func.attr in _SHUTIL_CALLS), f"{where} calls shutil.{node.func.attr}"
                assert node.func.attr not in _PATH_WRITE_CALLS, f"{where} calls {node.func.attr}"
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                raise AssertionError(f"{where} calls the builtin open")
    handles = (APP / "files" / "handles.py").read_text(encoding="utf-8")
    assert "os.open(path, os.O_RDONLY | _O_BINARY | _O_NOINHERIT)" in handles
    assert handles.count("os.open(") == 1


def test_the_quarantine_only_creates_new_files_inside_its_own_transfer_directory() -> None:
    source = (APP / "files" / "quarantine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = sorted(
        f"{node.func.value.id}.{node.func.attr}" if isinstance(node.func.value, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in _OS_WRITE_CALLS | _PATH_WRITE_CALLS | {"write", "rmtree"}
    )
    # Every write is an O_EXCL create (never truncating an existing file); one rename .part -> .bin.
    assert source.count("os.O_CREAT | os.O_EXCL") == source.count("os.O_WRONLY") == 3
    assert "O_TRUNC" not in source and "shutil" not in source
    assert calls.count("os.rename") == 1 and "os.remove" not in calls and "os.unlink" not in calls
    assert "directory.mkdir(exist_ok=False)" in source


def test_placement_is_the_one_no_overwrite_rename() -> None:
    source = (APP / "files" / "place.py").read_text(encoding="utf-8")
    assert "NtSetInformationFile" in source and "ReplaceIfExists" in source
    assert "MoveFileEx" not in source and "os.replace" not in source and "os.rename" not in source
    assert "shutil" not in source and "CopyFile" not in source and "DeleteFile" not in source
    assert "MOVEFILE_REPLACE_EXISTING" not in source


def test_only_the_document_service_reads_extracted_text() -> None:
    for path in APP.rglob("*.py"):
        relative = path.relative_to(APP).as_posix()
        if relative.startswith("repositories/documents.py"):
            continue
        source = path.read_text(encoding="utf-8")
        if "text_for(" in source:
            assert relative in _TEXT_READERS, f"{relative} reads extracted document text"


def test_no_other_runtime_module_imports_the_document_modules() -> None:
    allowed = {"api/document_routes.py", "api/document_schemas.py", "api/errors.py", "main.py", "services/documents.py", "repositories/documents.py"}
    for path in APP.rglob("*.py"):
        relative = path.relative_to(APP).as_posix()
        if relative.startswith(("documents/", "files/", "domain/documents.py")):
            continue
        imported = _imports(ast.parse(path.read_text(encoding="utf-8")))
        uses = {name for name in imported if name.startswith(("app.services.documents", "app.repositories.documents", "app.domain.documents", "app.documents"))}
        # S2: the transfer service and the download operation classify bytes by signature only; they never
        # extract or read document text.
        if uses == {"app.documents.sniff"} and relative in {"services/transfers.py", "browser/operations/download.py"}:
            continue
        if uses:
            assert relative in allowed, f"{relative} imports {sorted(uses)}"


def test_the_helper_is_started_without_a_shell_and_with_a_fixed_module() -> None:
    source = (APP / "documents" / "runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Popen"]
    assert len(calls) == 1
    keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords}
    assert isinstance(keywords["shell"], ast.Constant) and keywords["shell"].value is False
    assert '"app.documents.helper"' in source
