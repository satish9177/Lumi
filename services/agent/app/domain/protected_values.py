"""Milestone 8b S5: the closed set of saved details, and how each is previewed.

A protected value is something the user typed **directly** into a trusted Lumi
surface so that Lumi could later put it in a form field they approve. There are
exactly eight kinds, chosen in advance:

```text
legal_name  preferred_name  email  phone  city  country  linkedin_url  portfolio_url
```

There is no free-form kind: a model, a website and a voice turn cannot name a
ninth. There is no password, one-time code, payment detail, file, resume, address
blob or JSON.

**Where the raw value lives.** In the `protected_values` table of Lumi's local
runtime database, as plaintext task data. That is *not* encryption at rest and it
does not protect against a live compromise of the same Windows user; the
protection is the operating-system account and database access controls, and
that is all this document claims. It is not a credential: browser sessions stay
under the browser-profile boundary. After it is saved the value does not leave
its row in S5 -- what everything else sees is `kind`, `preview` and (to the
approval machinery only) `value_digest`.

**The canonical value and its digest.** `canonicalize` returns the exact string
Lumi would later type; `value_digest` is `SHA-256(UTF-8(canonical))`. An approval
binds the digest, so an approved mapping can never silently start using a
different value.

**The masking policy.** A preview is deterministic, fixed for a kind, and never
depends on the value's length. It gives a planner enough to decide *which field*
a saved detail belongs in, and nothing more:

| kind             | preview                                          |
| ---------------- | ------------------------------------------------ |
| `legal_name`     | the fixed text `saved legal name`                |
| `preferred_name` | the fixed text `saved preferred name`            |
| `email`          | first local character, fixed `***`, first domain character, fixed `***`, last label, e.g. `s***@e***.com` |
| `phone`          | `ending ` and the last four digits               |
| `city`           | the fixed text `saved city`                      |
| `country`        | **the country itself**                           |
| `linkedin_url`   | `linkedin.com/in/***`                            |
| `portfolio_url`  | the fixed text `saved portfolio link`            |

`country` is the one deliberate exception: a country is coarse, and choosing the
right option in a country list is impossible from a masked string. The trusted
disclosure card says so whenever `country` is among the details it offers, so the
card never claims that no saved value is sent. Every other preview is provably
not the value (`assert_masked`).
"""

import hashlib
import re
import unicodedata
from typing import Final, Literal, get_args
from urllib.parse import urlsplit

ProtectedKind = Literal[
    "legal_name",
    "preferred_name",
    "email",
    "phone",
    "city",
    "country",
    "linkedin_url",
    "portfolio_url",
]
PROTECTED_KINDS: Final[tuple[str, ...]] = get_args(ProtectedKind)

#: Kinds whose preview *is* the value, by design. Documented above and on the card.
PREVIEW_REVEALS_VALUE: Final[frozenset[str]] = frozenset({"country"})

MAX_VALUE_CHARS: Final = 300
_MAX_BY_KIND: Final[dict[str, int]] = {
    "legal_name": 100,
    "preferred_name": 100,
    "email": 254,
    "phone": 32,
    "city": 100,
    "country": 100,
    "linkedin_url": MAX_VALUE_CHARS,
    "portfolio_url": MAX_VALUE_CHARS,
}
_EMAIL = re.compile(r"^[^\s@]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,24}$")
_ENDS = " \t\r\n"
_PHONE_CHARS = re.compile(r"^\+?[0-9 ().\-]+$")


class ProtectedValueRefusal(ValueError):
    """A refused saved detail. `code` is stable and never contains the value."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The saved detail was refused ({code}).")
        self.code = code


def is_protected_kind(value: object) -> bool:
    return isinstance(value, str) and value in PROTECTED_KINDS


def _has_unsafe_character(value: str) -> bool:
    # NUL and every other control or format character (newline, tab, bidi
    # overrides, zero-width characters) -- a saved detail is one plain line.
    return any(unicodedata.category(character) in ("Cc", "Cf", "Cs", "Co", "Cn") for character in value)


def _url_parts(value: str, *, kind: str) -> tuple[str, str]:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ProtectedValueRefusal("invalid_url")
    if parts.username is not None or parts.password is not None:
        raise ProtectedValueRefusal("invalid_url")
    try:
        parts.port  # noqa: B018 - raises on a malformed port.
    except ValueError:
        raise ProtectedValueRefusal("invalid_url") from None
    host = parts.hostname.lower()
    if kind == "linkedin_url" and not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        raise ProtectedValueRefusal("invalid_linkedin_url")
    return parts.scheme, host


def canonicalize(kind: str, raw: object) -> str:
    """The exact string Lumi would later place, or a `ProtectedValueRefusal`.

    Deliberately modest. Names and places are never "corrected": internal runs of
    whitespace collapse and the ends are trimmed, nothing else changes. An email's
    domain is lower-cased (its local part is kept as typed); a phone number keeps
    the user's own formatting; a URL is kept as typed.
    """
    if not is_protected_kind(kind):
        raise ProtectedValueRefusal("unknown_kind")
    if not isinstance(raw, str):
        raise ProtectedValueRefusal("not_text")
    value = unicodedata.normalize("NFC", raw.strip(_ENDS))
    # Nothing but plain printable text is ever saved: NUL, newlines inside the
    # value, bidi and zero-width characters are refused, never silently dropped.
    if _has_unsafe_character(value):
        raise ProtectedValueRefusal("unsafe_characters")
    if kind in ("legal_name", "preferred_name", "city", "country"):
        value = re.sub(" +", " ", value)
    if not value:
        raise ProtectedValueRefusal("empty")
    if len(value) > _MAX_BY_KIND[kind]:
        raise ProtectedValueRefusal("too_long")
    if kind == "email":
        if not _EMAIL.match(value):
            raise ProtectedValueRefusal("invalid_email")
        local, _, domain = value.rpartition("@")
        value = f"{local}@{domain.lower()}"
    elif kind == "phone":
        digits = sum(character.isdigit() for character in value)
        if not _PHONE_CHARS.match(value) or not 7 <= digits <= 15:
            raise ProtectedValueRefusal("invalid_phone")
    elif kind in ("linkedin_url", "portfolio_url"):
        _url_parts(value, kind=kind)
    return value


def value_digest(canonical: str) -> str:
    """`SHA-256(UTF-8(canonical))`, lower-case hex. The database re-derives it."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def preview(kind: str, canonical: str) -> str:
    """The masked text a provider and the trusted card may see. See the module table."""
    if kind == "legal_name":
        result = "saved legal name"
    elif kind == "preferred_name":
        result = "saved preferred name"
    elif kind == "city":
        result = "saved city"
    elif kind == "country":
        result = canonical
    elif kind == "email":
        local, _, domain = canonical.rpartition("@")
        labels = domain.split(".")
        result = f"{local[:1]}***@{labels[0][:1]}***.{labels[-1]}"
    elif kind == "phone":
        digits = [character for character in canonical if character.isdigit()]
        result = f"ending {''.join(digits[-4:])}"
    elif kind == "linkedin_url":
        result = "linkedin.com/in/***"
    elif kind == "portfolio_url":
        result = "saved portfolio link"
    else:  # pragma: no cover - `canonicalize` already refused an unknown kind.
        raise ProtectedValueRefusal("unknown_kind")
    assert_masked(kind, canonical, result)
    return result


def assert_masked(kind: str, canonical: str, shown: str) -> None:
    """A preview that would reveal the value is a bug, so it is a hard error."""
    if kind not in PREVIEW_REVEALS_VALUE and canonical in shown:
        raise ProtectedValueRefusal("preview_reveals_value")  # pragma: no cover


__all__ = [
    "MAX_VALUE_CHARS",
    "PREVIEW_REVEALS_VALUE",
    "PROTECTED_KINDS",
    "ProtectedKind",
    "ProtectedValueRefusal",
    "assert_masked",
    "canonicalize",
    "is_protected_kind",
    "preview",
    "value_digest",
]
