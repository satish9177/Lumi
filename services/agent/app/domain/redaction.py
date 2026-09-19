"""Deterministic identifier redaction for account-private text (Milestone 8a S3).

Authenticated page text is redacted **before** it is projected: inside the
isolated worker, before it is returned, hashed, stored or sent to any model.
The provider therefore never sees a raw identifier, the database never holds
one, and grounding runs against exactly the text the provider received.

What is reduced, in this order (each pass sees the previous pass's output, and
no placeholder can be matched again by a later pass):

```text
email address                    ⟦email:1⟧       stable per distinct address
Luhn-valid card-shaped number    ⟦card:4242⟧     last four kept
phone-shaped identifier          ⟦phone:1⟧       stable per distinct number
nine or more contiguous digits   ⟦digits:4821⟧   last four kept
```

Ordinary quantities, dates and counts ("17 private repositories",
"2026-09-20", "1,234") survive, because an answer that cannot cite a figure is
not an answer.

**This is a reduction in exposure, not anonymisation.** Pattern matching
over-matches (a long order number is hidden although it identifies nobody) and
under-matches (a name, an address, a username, a short account number, a
spelled-out number and any identifier that merely looks like prose are all
untouched). Nothing here may be described as "anonymous", "anonymised" or
"de-identified", and no caller may rely on it as a privacy guarantee.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

OPEN_MARK = "⟦"
CLOSE_MARK = "⟧"

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w-])")
#: 13-19 digits with optional single space or dash separators.
_CARD = re.compile(r"(?<![\d⟦])(?:\d[ -]?){12,18}\d(?!\d)")
#: A phone-shaped run must carry structure: a leading `+country`, or grouped
#: digits with separators. Bare digit runs are the long-digit rule's business,
#: and ISO dates (4-2-2) and plain thousands separators never match.
_PHONE = re.compile(
    r"(?<![\w⟦])(?:"
    r"\+\d{1,3}[ .-]?(?:\(\d{1,4}\)|\d{1,4})(?:[ .-]?\d{2,5}){2,4}"
    r"|\(\d{3,5}\)[ .-]?\d{3,4}[ .-]?\d{3,4}"
    r"|\d{3,5}[ .-]\d{3,4}[ .-]\d{3,4}"
    r")(?![\w⟧])"
)
_LONG_DIGITS = re.compile(r"(?<!\d)\d{9,}(?!\d)")

_DIGITS_ONLY = re.compile(r"\D")


def luhn_valid(digits: str) -> bool:
    """The Luhn checksum over a digit string."""
    if not digits.isdigit():
        return False
    total = 0
    for index, character in enumerate(reversed(digits)):
        value = int(character)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


@dataclass(slots=True)
class Redactor:
    """One observation's redactor. Placeholders are stable *within* it."""

    _emails: dict[str, int] = field(default_factory=dict)
    _phones: dict[str, int] = field(default_factory=dict)
    counts: Counter[str] = field(default_factory=Counter)

    def redact(self, text: str) -> str:
        text = _EMAIL.sub(self._email, text)
        text = _CARD.sub(self._card, text)
        text = _PHONE.sub(self._phone, text)
        return _LONG_DIGITS.sub(self._digits, text)

    def _email(self, match: re.Match[str]) -> str:
        key = match.group(0).lower()
        number = self._emails.setdefault(key, len(self._emails) + 1)
        self.counts["email"] += 1
        return f"{OPEN_MARK}email:{number}{CLOSE_MARK}"

    def _card(self, match: re.Match[str]) -> str:
        digits = _DIGITS_ONLY.sub("", match.group(0))
        if not 13 <= len(digits) <= 19 or not luhn_valid(digits):
            return match.group(0)
        self.counts["card"] += 1
        return f"{OPEN_MARK}card:{digits[-4:]}{CLOSE_MARK}"

    def _phone(self, match: re.Match[str]) -> str:
        digits = _DIGITS_ONLY.sub("", match.group(0))
        if not 8 <= len(digits) <= 15:
            return match.group(0)
        number = self._phones.setdefault(digits, len(self._phones) + 1)
        self.counts["phone"] += 1
        return f"{OPEN_MARK}phone:{number}{CLOSE_MARK}"

    def _digits(self, match: re.Match[str]) -> str:
        self.counts["digits"] += 1
        return f"{OPEN_MARK}digits:{match.group(0)[-4:]}{CLOSE_MARK}"


def is_redacted(text: str) -> bool:
    """True when a fresh redactor would change nothing.

    The runtime uses this on everything the worker returns: text that still
    contains an identifier-shaped run was not redacted where it should have
    been, and is refused rather than stored.
    """
    return Redactor().redact(text) == text


__all__ = ["CLOSE_MARK", "OPEN_MARK", "Redactor", "is_redacted", "luhn_valid"]
