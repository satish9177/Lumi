"""The prohibition that makes a persistent profile safe to have at all.

> Lumi may operate a persistent Chromium profile, but Lumi must never extract
> or serialise the authentication material inside that profile.

That is not a convention, a review note or a docstring: it is this test. A
persistent profile is only defensible because nothing in Lumi can turn it into
a portable, replayable impersonation token. `context.storage_state()` would put
session cookies into the worker's memory and then into a file Lumi wrote;
`context.cookies()` would hand them to application code; reading `Cookies` or
`Login Data` out of the profile directory would do the same with extra steps.
None of those exist, and this test is what stops one being added casually.

**Scope: Lumi's own application code** (`app/`, `evals/` and `tests/`), never
`site-packages`. Playwright itself of course defines `storage_state` and
`cookies` -- it is a browser automation library, and forbidding the strings
there would be meaningless. The boundary is that *Lumi* never calls them. Test
files are scanned too, so a "just for this test" export is caught as well.

Three scans, with deliberately different strictness, because the three risks
look different in source:

1. **Call-shaped patterns** (`storage_state(`, `.cookies(`) are matched against
   source with docstrings, comments *and string literals* removed. Several
   modules explain this rule at length and must be able to name the thing they
   forbid; a call is the thing that matters.
2. **Page script that reaches storage** (`document.cookie`, `localStorage`)
   can only ever appear inside a Python string literal -- a script handed to
   `page.evaluate` or `add_init_script` -- so literals are kept for this one.
3. **Chromium's own profile filenames**, again with literals kept, so
   `open(profile / "Login Data")` is caught: the filename lives in a literal,
   which is exactly where a real read would put it.

And the scanner is itself tested against planted violations, because a guard
nobody has watched fail is a guard nobody knows works.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1]
SCANNED_TREES = (AGENT / "app", AGENT / "evals", AGENT / "tests")

#: Call-shaped patterns for every way a browser profile's contents could leave
#: the browser. The second element is what a failure reports.
FORBIDDEN: tuple[tuple[str, str], ...] = (
    (r"\bstorage_state\s*\(", "storage_state() serialises cookies and web storage to a file"),
    (r"\bstorageState\b", "storageState is the same export by its JavaScript name"),
    (r"\.cookies\s*\(", "context.cookies() hands session cookies to application code"),
    (r"\badd_cookies\s*\(", "add_cookies() injects a credential Lumi would have had to hold"),
    (r"\bclear_cookies\s*\(", "clearing cookies is a partial, silent logout Lumi does not do"),
)

#: JavaScript that reaches a profile's stored credentials from inside a page.
FORBIDDEN_PAGE_SCRIPT: tuple[tuple[str, str], ...] = (
    (r"\bdocument\.cookie\b", "reading or writing cookies from inside a page"),
    (r"\blocalStorage\b", "reading localStorage out of a page"),
    (r"\bsessionStorage\b", "reading sessionStorage out of a page"),
    (r"\bindexedDB\b", "reading IndexedDB out of a page"),
)

#: Files Chromium keeps inside a profile directory. Lumi must never open one:
#: they are the credential database, and reading one is an export whatever the
#: intent. Lumi creates the directory, holds one lock file of its own inside
#: it, and removes the tree on delete. Nothing else.
PROFILE_FILES: tuple[str, ...] = (
    "Login Data",
    "Web Data",
    "Local State",
    "Network/Cookies",
    "SingletonLock",
    "SingletonCookie",
    "Local Storage/leveldb",
)

#: The fixture *sites* under `evals/sites/` play the role of the remote website
#: in a test, not the role of Lumi. A fixture page setting its own
#: `localStorage` is a website doing what websites do, and is exactly what the
#: S1 persistence proof needs. Lumi's own code is held to the stricter rule.
_PAGE_SCRIPT_EXEMPT_TREE = AGENT / "evals" / "sites"

#: This file names every pattern and filename it forbids, in code, on purpose.
_EXEMPT = {Path(__file__).resolve()}

_DOCSTRING = re.compile(r"(\"\"\"|''')(?:.|\n)*?\1")
_COMMENT = re.compile(r"#[^\n]*")
_LITERAL = re.compile(r"(?<![A-Za-z])[rbfu]{0,2}(['\"])(?:\\.|(?!\1).)*?\1")


def _python_files() -> Iterator[Path]:
    for tree in SCANNED_TREES:
        for path in sorted(tree.rglob("*.py")):
            if "__pycache__" in path.parts or path.resolve() in _EXEMPT:
                continue
            yield path


def _without_prose(source: str) -> str:
    """Docstrings and comments removed; string literals kept."""
    return _COMMENT.sub("", _DOCSTRING.sub('""', source))


def _code_only(source: str) -> str:
    """Docstrings, comments and string literals all removed."""
    return _LITERAL.sub('""', _without_prose(source))


def _scan(
    code: str, path: Path, patterns: tuple[tuple[str, str], ...]
) -> list[str]:
    found: list[str] = []
    for pattern, reason in patterns:
        for match in re.finditer(pattern, code):
            line = code[: match.start()].count("\n") + 1
            found.append(f"{path.relative_to(AGENT)}:{line} {match.group(0)!r} -- {reason}")
    return found


def test_no_lumi_code_exports_a_profiles_credential_material() -> None:
    """No call that could serialise cookies, tokens or web storage exists."""
    violations: list[str] = []
    for path in _python_files():
        violations += _scan(_code_only(path.read_text(encoding="utf-8")), path, FORBIDDEN)
    assert violations == [], "\n".join(violations)


def test_no_lumi_code_reads_storage_out_of_a_page() -> None:
    """No script Lumi hands to a page reaches cookies or web storage."""
    violations: list[str] = []
    for path in _python_files():
        if path.is_relative_to(_PAGE_SCRIPT_EXEMPT_TREE):
            continue
        violations += _scan(
            _without_prose(path.read_text(encoding="utf-8")), path, FORBIDDEN_PAGE_SCRIPT
        )
    assert violations == [], "\n".join(violations)


def test_no_lumi_code_reads_a_file_inside_a_profile_directory() -> None:
    """The browser owns the directory's contents; Lumi owns its identity."""
    violations: list[str] = []
    for path in _python_files():
        code = _without_prose(path.read_text(encoding="utf-8"))
        for name in PROFILE_FILES:
            if name in code:
                violations.append(f"{path.relative_to(AGENT)} names the profile file {name!r}")
    assert violations == [], "\n".join(violations)


def test_the_profile_lock_is_lumis_own_file_and_chromiums_is_untouched() -> None:
    """Chromium's `SingletonLock` is neither relied on nor deleted.

    Deleting a lock file behind the browser's back is the hand-editing of
    browser internals this design refuses; Lumi keeps its own handle instead.
    """
    code = _without_prose(
        (AGENT / "app" / "browser" / "profile_lock.py").read_text(encoding="utf-8")
    )
    assert "Singleton" not in code
    for removal in ("unlink", "rmtree", "os.remove", "shutil"):
        assert removal not in code


def test_the_scanner_catches_a_planted_violation() -> None:
    """The shapes a well-meaning future change would actually take."""
    for line in (
        "state = await context.storage_state(path='out.json')",
        "jar = await context.cookies()",
        "await context.add_cookies(jar)",
        "await context.clear_cookies()",
    ):
        code = _code_only("async def leak(context, page):\n    " + line + "\n")
        assert any(re.search(pattern, code) for pattern, _ in FORBIDDEN), line

    for script in (
        "await page.evaluate('document.cookie')",
        "await page.evaluate('JSON.stringify(localStorage)')",
        "await context.add_init_script('window.__s = sessionStorage')",
    ):
        code = _without_prose("async def leak(context, page):\n    " + script + "\n")
        assert any(re.search(p, code) for p, _ in FORBIDDEN_PAGE_SCRIPT), script

    # A read of Chromium's own credential database, written the way one would
    # be: the filename is in a literal, which the file scan keeps.
    database_read = _without_prose("data = (profile / 'Login Data').read_bytes()\n")
    assert any(name in database_read for name in PROFILE_FILES)


def test_prose_explaining_the_rule_is_not_a_violation() -> None:
    """Modules must be able to name what they forbid, or the rule is unwritable."""
    explained = (
        '"""Lumi never calls storage_state() or context.cookies(), never reads\n'
        'document.cookie or localStorage, and never opens Login Data."""\n'
    )
    assert not any(re.search(p, _code_only(explained)) for p, _ in FORBIDDEN)
    assert not any(re.search(p, _without_prose(explained)) for p, _ in FORBIDDEN_PAGE_SCRIPT)
    assert not any(name in _without_prose(explained) for name in PROFILE_FILES)


@pytest.mark.parametrize(("pattern", "reason"), FORBIDDEN + FORBIDDEN_PAGE_SCRIPT)
def test_every_forbidden_pattern_is_a_valid_expression(pattern: str, reason: str) -> None:
    """A pattern that does not compile silently guards nothing."""
    assert re.compile(pattern)
    assert reason
