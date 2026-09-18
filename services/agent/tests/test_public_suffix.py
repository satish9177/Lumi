"""The pinned Public Suffix List: is it the list we think it is, and does the
lookup agree with the algorithm the list is published with?

Both halves matter. A silently replaced or ageing snapshot moves a site
boundary without anybody noticing, and a hand-rolled "last two labels" lookup
gets `example.co.uk` and `foo.github.io` wrong -- each wrong answer being one
profile holding a site's cookies while calling itself another site.
"""

import hashlib
import re
from pathlib import Path

import pytest

from app.domain.public_suffix import (
    PUBLIC_SUFFIX_COMMIT,
    PUBLIC_SUFFIX_DIGEST,
    PUBLIC_SUFFIX_VERSION,
    PublicSuffixError,
    canonical_host,
    public_suffix_list,
    registrable_domain,
)

_SNAPSHOT = Path(__file__).resolve().parents[1] / "app" / "data" / "public_suffix_list.dat"
_VECTORS = Path(__file__).resolve().parent / "public_suffix_vectors.txt"
#: The upstream test file's own shape: `checkPublicSuffix('x', 'y');`
_VECTOR = re.compile(r"^checkPublicSuffix\((null|'(?P<input>[^']*)'), (null|'(?P<expected>[^']*)')\);")


def _a_labels(value: str | None) -> str | None:
    """Lumi stores sites as A-labels, so Unicode vectors are compared as those."""
    if value is None or value.isascii():
        return value
    return ".".join(part.encode("idna").decode("ascii") for part in value.split("."))


def _vectors() -> list[tuple[str, str | None]]:
    cases: list[tuple[str, str | None]] = []
    for line in _VECTORS.read_text(encoding="utf-8").splitlines():
        match = _VECTOR.match(line.strip())
        if match is None or match.group("input") is None:
            continue
        cases.append((match.group("input"), match.group("expected")))
    return cases


def test_the_snapshot_is_the_one_that_was_reviewed() -> None:
    """The digest is pinned in code, so a substituted list fails to load.

    Line endings are normalised before digesting: the digest must depend on the
    file's content, not on how a checkout happened to write it to disk.
    """
    raw = _SNAPSHOT.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == PUBLIC_SUFFIX_DIGEST
    header = raw.decode("utf-8")[:2_000]
    assert f"// VERSION: {PUBLIC_SUFFIX_VERSION}" in header
    assert f"// COMMIT: {PUBLIC_SUFFIX_COMMIT}" in header


def test_the_list_is_bundled_and_never_fetched() -> None:
    """There is no code path that downloads, updates or replaces the snapshot."""
    source = (
        Path(__file__).resolve().parents[1] / "app" / "domain" / "public_suffix.py"
    ).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", "*"))
    )
    for forbidden in ("httpx", "requests", "urlopen", "urlretrieve", "socket"):
        assert forbidden not in body, f"{forbidden} has no business in the PSL loader"


def test_the_snapshot_parses_into_all_three_rule_kinds() -> None:
    psl = public_suffix_list()
    assert len(psl.rules) > 5_000
    assert len(psl.wildcards) > 100
    assert len(psl.exceptions) > 0
    # Both sections are loaded; ICANN is a strict subset used for reporting.
    assert psl.icann < (psl.rules | psl.wildcards | psl.exceptions)
    # Unicode rules were converted to A-labels, or `公司.cn` stops being a suffix.
    assert "xn--55qx5d.cn" in psl.rules
    assert all(rule.isascii() for rule in psl.rules)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        # The plain case, and the one everybody gets right.
        ("github.com", "github.com"),
        ("sub.github.com", "github.com"),
        ("a.b.c.github.com", "github.com"),
        ("GitHub.COM", "github.com"),
        ("github.com.", "github.com"),
        # A multi-label public suffix: "last two labels" would answer `co.uk`.
        ("example.co.uk", "example.co.uk"),
        ("www.example.co.uk", "example.co.uk"),
        # A *private* suffix: two GitHub Pages projects are different sites to a
        # browser, so they must be different Lumi profiles.
        ("foo.github.io", "foo.github.io"),
        ("bar.foo.github.io", "foo.github.io"),
        # A wildcard rule and its exception, from the list's own tricky corner.
        ("www.ck", "www.ck"),
        ("b.c.kobe.jp", "b.c.kobe.jp"),
        ("a.b.c.kobe.jp", "b.c.kobe.jp"),
        ("city.kobe.jp", "city.kobe.jp"),
        ("www.city.kobe.jp", "city.kobe.jp"),
        # An unlisted TLD falls back to the implicit `*` rule.
        ("example.example", "example.example"),
        ("b.example.example", "example.example"),
        # `*.kobe.jp` makes every child of `kobe.jp` a public suffix, but
        # `kobe.jp` itself is an ordinary registration under `jp`. A rule that
        # stopped at "is there a wildcard below me" would get this backwards.
        ("kobe.jp", "kobe.jp"),
        # `s3.amazonaws.com` is a private suffix, so every bucket is its own
        # site -- and `amazonaws.com`, which is *not* a rule, is an ordinary
        # registration under `com`.
        ("my-bucket.s3.amazonaws.com", "my-bucket.s3.amazonaws.com"),
        ("amazonaws.com", "amazonaws.com"),
    ],
)
def test_tricky_registrable_domains(host: str, expected: str) -> None:
    assert registrable_domain(host) == expected


@pytest.mark.parametrize(
    "host",
    [
        # A bare public suffix has no registrable domain, at any depth.
        "com",
        "co.uk",
        "github.io",
        "c.kobe.jp",
        "s3.amazonaws.com",
        "instance.compute.amazonaws.com",
        # Addresses are not sites, in any spelling a browser would accept.
        "127.0.0.1",
        "0x7f000001",
        "2130706433",
        "192.168.1.1",
        # Local and reserved names.
        "localhost",
        # Not a host at all.
        "",
        "github.com:443",
        "github.com/path",
        "user@github.com",
        "git hub.com",
        "exa_mple.com",
        "-github.com",
        "github-.com",
        "xn--" + "a" * 70 + ".com",
        "http://github.com",
        "гитхаб.com",
    ],
)
def test_names_with_no_registrable_domain_are_refused(host: str) -> None:
    with pytest.raises(PublicSuffixError):
        registrable_domain(host)


def test_the_published_test_vectors_all_pass() -> None:
    """The upstream `tests/test_psl.txt`, run in full against the snapshot.

    This is the strongest available statement that the lookup implements the
    published algorithm rather than something that merely agrees with it on the
    cases somebody thought to write down.
    """
    cases = _vectors()
    assert len(cases) >= 70, "the vector file did not parse"
    failures: list[str] = []
    for raw, expected in cases:
        host, want = _a_labels(raw), _a_labels(expected)
        assert host is not None
        try:
            got: str | None = registrable_domain(host)
        except PublicSuffixError:
            got = None
        if got != want:
            failures.append(f"{raw!r}: expected {want!r}, got {got!r}")
    assert failures == []


def test_canonical_host_has_exactly_one_spelling_per_name() -> None:
    assert canonical_host(" GitHub.COM. ") == "github.com"
    assert canonical_host("WWW.Example.Co.UK") == "www.example.co.uk"
