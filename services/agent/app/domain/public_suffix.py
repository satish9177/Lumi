"""The registrable domain (eTLD+1), from a pinned Public Suffix List snapshot.

Milestone 8a binds one persistent browser profile to exactly one *site*, and
"site" has to mean the same thing a browser means when it decides whether a
cookie may be set. That boundary is the Public Suffix List, and it cannot be
computed by splitting on dots:

    github.com          -> github.com      two labels, and both are needed
    sub.github.com      -> github.com      the sub-domain is not a site
    example.co.uk       -> example.co.uk   `co.uk` is a suffix, so three labels
    foo.github.io       -> foo.github.io   `github.io` is a *private* suffix,
                                           so two GitHub Pages projects are
                                           different sites to a browser
    www.ck              -> www.ck          `*.ck` with a `!www.ck` exception

A naive "last two labels" rule gets three of those five wrong, and each wrong
answer is a profile holding one site's cookies while claiming to be another's.

**The snapshot is pinned, not fetched.** `app/data/public_suffix_list.dat` is a
committed copy; its digest is asserted on load against `PUBLIC_SUFFIX_DIGEST`
below, and the packaged runtime manifest records the same digest. There is no
code path that downloads, updates or replaces it -- a list that changed under
Lumi would silently move a site boundary, which is exactly the thing this
module exists to make stable. The cost is that the snapshot ages: a suffix
delegated after `PUBLIC_SUFFIX_VERSION` is classified by the old rules until
the next Lumi release. That is a known, accepted residual (plan section 24.13).

**Both sections are used, ICANN and private.** Browsers apply the whole list to
cookie scope, so `foo.github.io` and `bar.github.io` cannot set cookies for one
another. Using ICANN-only rules here would let one Lumi profile span every
GitHub Pages project, which is the multi-site profile the architecture refuses.

The lookup follows the algorithm published with the list, in full:

1. every rule whose labels match the trailing labels of the domain matches;
2. a wildcard label `*` matches any single label;
3. an exception rule (`!`) wins outright, and its public suffix is the rule
   with its leftmost label removed;
4. otherwise the matching rule with the most labels wins;
5. with no matching rule the prevailing rule is `*`, so a bare unknown TLD has
   a one-label public suffix;
6. the registrable domain is the public suffix plus one more label, and a
   domain that *is* a public suffix has none.
"""

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: The snapshot's own `// VERSION:` header. Recorded in the runtime manifest,
#: reported in the S1 review, and the thing to bump when the list is refreshed.
PUBLIC_SUFFIX_VERSION = "2026-09-17_19-09-53_UTC"
#: The upstream commit the snapshot was taken at.
PUBLIC_SUFFIX_COMMIT = "329834e40a8543f3ef6265db543b7af6c4185571"
#: SHA-256 over the snapshot with line endings normalised to LF. Normalising
#: first is deliberate: the digest must not depend on how a checkout wrote the
#: file to disk, only on its content.
PUBLIC_SUFFIX_DIGEST = "82c08c93231a51649b061ea3541884b01716344c3f2edec07272a0a8b9849b1c"
PUBLIC_SUFFIX_SOURCE = "https://publicsuffix.org/list/public_suffix_list.dat"

_DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "public_suffix_list.dat"
_ICANN_BEGIN = "===BEGIN ICANN DOMAINS==="
_ICANN_END = "===END ICANN DOMAINS==="
#: A label as it may appear in a domain Lumi will bind a profile to. ASCII only:
#: an internationalised name must already be in its A-label (`xn--`) spelling,
#: so there is exactly one way to write any site Lumi stores.
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
#: The WHATWG URL parser reads a final numeric label as an IPv4 address.
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")
MAX_DOMAIN_LENGTH = 253


class PublicSuffixError(Exception):
    """A name that has no registrable domain. `code` is stable and loggable."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The host has no registrable domain ({code}).")
        self.code = code


@dataclass(frozen=True, slots=True)
class PublicSuffixList:
    """The parsed snapshot. Built once and cached; never mutated."""

    #: Ordinary rules, such as `co.uk`.
    rules: frozenset[str]
    #: Wildcard rules with their `*` intact, such as `*.ck`.
    wildcards: frozenset[str]
    #: Exception rules with the leading `!` stripped, such as `www.ck`.
    exceptions: frozenset[str]
    #: Rules that came from the ICANN section. Reporting only -- the lookup
    #: uses every rule, because browsers do.
    icann: frozenset[str]
    version: str
    digest: str

    def public_suffix(self, domain: str) -> str:
        """The public suffix of an already-canonical domain."""
        labels = domain.split(".")
        # An exception rule wins over everything, including a longer wildcard.
        for index in range(len(labels)):
            if ".".join(labels[index:]) in self.exceptions:
                return ".".join(labels[index + 1 :])
        best = ""
        for index in range(len(labels)):
            candidate = ".".join(labels[index:])
            matched = candidate
            if candidate not in self.rules:
                # A wildcard consumes exactly one more label to the left.
                if index == 0 or "*." + candidate not in self.wildcards:
                    continue
                matched = ".".join(labels[index - 1 :])
            if matched.count(".") >= best.count("."):
                best = matched
        if best:
            return best
        # Rule 5: no rule matched, so the prevailing rule is `*`.
        return labels[-1]

    def registrable_domain(self, domain: str) -> str:
        """`sub.example.co.uk` -> `example.co.uk`. Raises, never guesses."""
        suffix = self.public_suffix(domain)
        if suffix == domain:
            raise PublicSuffixError("host_is_a_public_suffix")
        remainder = domain[: -(len(suffix) + 1)]
        return f"{remainder.rsplit('.', 1)[-1]}.{suffix}"


def _to_a_labels(rule: str) -> str:
    """`公司.cn` -> `xn--55qx5d.cn`.

    459 of the snapshot's rules are written in Unicode. Lumi stores and compares
    sites as A-labels, so the rules have to be in the same alphabet or a
    Unicode-named suffix silently stops being a suffix -- which is how
    `xn--55qx5d.cn` would become a registrable domain someone could bind a
    profile to. Conversion happens once at load; an unconvertible rule raises
    rather than being dropped, because dropping one would weaken the boundary
    quietly.
    """
    if rule.isascii():
        return rule
    try:
        return ".".join(
            label if label == "*" else label.encode("idna").decode("ascii")
            for label in rule.split(".")
        )
    except UnicodeError:
        raise PublicSuffixError("public_suffix_rule_not_encodable") from None


def _parse(text: str) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    rules: set[str] = set()
    wildcards: set[str] = set()
    exceptions: set[str] = set()
    icann: set[str] = set()
    in_icann = False
    for raw in text.split("\n"):
        line = raw.strip()
        if line.startswith("//"):
            if _ICANN_BEGIN in line:
                in_icann = True
            elif _ICANN_END in line:
                in_icann = False
            continue
        if not line:
            continue
        # Case-fold and convert to A-labels, so every rule is in the same
        # alphabet as the hosts they are matched against.
        rule = _to_a_labels(line.lower())
        if rule.startswith("!"):
            body = rule[1:]
            exceptions.add(body)
        elif "*" in rule:
            body = rule
            wildcards.add(rule)
        else:
            body = rule
            rules.add(rule)
        if in_icann:
            icann.add(body)
    return frozenset(rules), frozenset(wildcards), frozenset(exceptions), frozenset(icann)


@lru_cache(maxsize=1)
def public_suffix_list() -> PublicSuffixList:
    """Load, verify and parse the pinned snapshot. Cached for the process."""
    raw = _DATA_FILE.read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != PUBLIC_SUFFIX_DIGEST:
        # Fail closed: an unverified list is a silently different site boundary.
        raise PublicSuffixError("public_suffix_list_digest_mismatch")
    rules, wildcards, exceptions, icann = _parse(raw.decode("utf-8"))
    return PublicSuffixList(
        rules=rules,
        wildcards=wildcards,
        exceptions=exceptions,
        icann=icann,
        version=PUBLIC_SUFFIX_VERSION,
        digest=digest,
    )


def canonical_host(host: str) -> str:
    """Lower-case, trailing dot removed, and checked label by label.

    Refuses anything that is not a plain ASCII DNS name: an IP literal in any
    spelling, a name with an empty or over-long label, a percent-encoded or
    non-ASCII authority, or a name carrying a port, scheme, path or credential.
    """
    value = host.strip().lower().rstrip(".")
    if not value or len(value) > MAX_DOMAIN_LENGTH:
        raise PublicSuffixError("host_length")
    if any(character in value for character in ':/@?#\\ %_"'):
        raise PublicSuffixError("host_shape")
    labels = value.split(".")
    if len(labels) < 2:
        raise PublicSuffixError("host_not_a_domain")
    if not all(_LABEL.fullmatch(label) for label in labels):
        raise PublicSuffixError("host_label")
    # A final numeric label is how a browser spells an IPv4 address; refuse it
    # rather than letting `127.0.0.1` become a "site".
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        raise PublicSuffixError("host_is_an_address")
    return value


def registrable_domain(host: str) -> str:
    """The one function callers need: a host name in, an eTLD+1 out."""
    return public_suffix_list().registrable_domain(canonical_host(host))


__all__ = [
    "MAX_DOMAIN_LENGTH",
    "PUBLIC_SUFFIX_COMMIT",
    "PUBLIC_SUFFIX_DIGEST",
    "PUBLIC_SUFFIX_SOURCE",
    "PUBLIC_SUFFIX_VERSION",
    "PublicSuffixError",
    "PublicSuffixList",
    "canonical_host",
    "public_suffix_list",
    "registrable_domain",
]
