"""Milestone 10 S4: the cross-app preparation workflow -- lineage, provenance and candidate fields.

A workflow is a **deterministic controller** over capabilities that already exist. It holds no tool of
its own and asks no model what to do next:

```text
download (S2) -> placement (S2)                       role `download`   (a transfer task)
   -> the placed file, proven to be the downloaded bytes, into ONE document task
   -> extraction + local compare (S1)                 role `documents`  (a document task)
   -> optional ONE provider disclosure (S1)
   -> candidate fields: `document_extracted` (a local span) or `provider_derived` (a grounded quote)
   -> trusted adoption (an exact approval) -> a WORKFLOW-SCOPED protected value that keeps its source
   -> authenticated observation + exact form preparation (M8 S4-S6)  role `form` (an authenticated task)
   -> STOP BEFORE SUBMIT
```

**Lineage.** Every child task is created by the controller and recorded in `workflow_steps` with its
role; a task belongs to at most one workflow. An artifact is consumed only by the workflow that produced
it, in the role that produced it: the database itself refuses a candidate whose document is not the
`documents` step's, and a value whose provenance differs from its candidate's.

**Provenance.** A candidate is never trusted automatically and a model never names one:

* `document_extracted` -- a labelled line (`Email: ...`) found by the deterministic extractor below in the
  document's own extracted text, bound to the text's SHA-256 and the span;
* `provider_derived` -- the same extractor applied to a quote the ONE approved provider returned *and the
  runtime grounded in the disclosed projection*. The provider's closed comparison schema is unchanged: it
  can point at text, never name a field kind or a value.

Adopting one is an exact approval. The value it creates lives in `workflow_values`, never in the global
`protected_values` of M8, and its provenance column has no `user_typed` member: a provider-derived value
cannot become a user-typed detail by any path.
"""

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.digest import canonical_json
from app.domain.protected_values import (
    PROTECTED_KINDS,
    ProtectedKind,
    ProtectedValueRefusal,
    canonicalize,
    preview,
    value_digest,
)

WORKFLOW_KIND: Final = "cross_app_preparation"
WORKFLOW_ROLES: Final = ("download", "documents", "form")
WorkflowRole = Literal["download", "documents", "form"]
PROVENANCES: Final = ("document_extracted", "provider_derived")
Provenance = Literal["document_extracted", "provider_derived"]
#: The persisted tool identity of the exact adoption approval. It is not a worker operation: nothing is
#: dispatched, and the generic action routes refuse to propose, approve, start or finish it.
ADOPT_TOOL: Final = "adopt_workflow_value"
ADOPTION_KIND: Final = "workflow_value_adoption"
ADOPTION_POLICY_VERSION: Final = "workflow-adopt-v1"

MAX_OBJECTIVE_CHARS: Final = 300
MAX_CANDIDATES_PER_SOURCE: Final = 16
MAX_CANDIDATES_PER_WORKFLOW: Final = 48
#: Document text is private and short-lived; so is everything a workflow derives from it.
WORKFLOW_TTL_SECONDS: Final = 24 * 60 * 60
#: The redaction placeholder S1 puts in a projection. A value containing it is never a value.
_REDACTION_MARK: Final = "⟦"
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_DIGEST: Final = r"^[0-9a-f]{64}$"

#: The closed label vocabulary. A label outside it is ignored; there is no fuzzy matching and no model.
LABELS: Final[dict[str, str]] = {
    "name": "legal_name",
    "full name": "legal_name",
    "legal name": "legal_name",
    "applicant name": "legal_name",
    "candidate name": "legal_name",
    "preferred name": "preferred_name",
    "email": "email",
    "e-mail": "email",
    "email address": "email",
    "e-mail address": "email",
    "phone": "phone",
    "phone number": "phone",
    "mobile": "phone",
    "mobile number": "phone",
    "telephone": "phone",
    "city": "city",
    "country": "country",
    "linkedin": "linkedin_url",
    "linkedin url": "linkedin_url",
    "linkedin profile": "linkedin_url",
    "portfolio": "portfolio_url",
    "portfolio url": "portfolio_url",
    "website": "portfolio_url",
    "personal website": "portfolio_url",
}
_LINE: Final = re.compile(r"^[ \t]*(?P<label>[A-Za-z][A-Za-z \-]{0,29}?)[ \t]*[:：][ \t]*(?P<value>\S[^\n]*?)[ \t]*$")


class WorkflowRefusal(ValueError):
    """A refused workflow request or state. `code` is stable and never carries a value, text or a path."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The workflow request was refused ({code}).")
        self.code = code


#: Refusals that are *state* (the world moved on), not a malformed request.
STATE_CODES: Final = frozenset(
    {
        "workflow_not_found",
        "workflow_not_active",
        "workflow_expired",
        "step_exists",
        "step_missing",
        "not_placed",
        "placed_file_changed",
        "document_not_in_workflow",
        "document_expired",
        "document_changed",
        "disclosure_not_in_workflow",
        "disclosure_not_succeeded",
        "candidate_not_found",
        "candidate_not_proposed",
        "candidate_changed",
        "value_already_adopted",
        "adoption_not_found",
        "not_a_workflow_step",
        "projection_changed",
    }
)


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def validate_objective(value: object) -> str:
    if not isinstance(value, str):
        raise WorkflowRefusal("objective_invalid")
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if not text or len(text) > MAX_OBJECTIVE_CHARS or _CONTROL.search(text):
        raise WorkflowRefusal("objective_invalid")
    return text


# ---- the deterministic candidate extractor --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FoundField:
    """One labelled value found in a text. `start`/`end` index the raw value in that text."""

    kind: str
    raw: str
    canonical: str
    digest: str
    preview: str
    start: int
    end: int
    #: The whole labelled line (`[line_start, line_end)` in the same text): what a provider quote must cover.
    line_start: int = 0
    line_end: int = 0


def _label_kind(label: str) -> str | None:
    key = " ".join(label.casefold().replace("–", "-").split())
    return LABELS.get(key)


def find_fields(text: str, *, limit: int = MAX_CANDIDATES_PER_SOURCE) -> list[FoundField]:
    """Every `Label: value` line whose label is in the closed vocabulary and whose value is a valid
    protected value of that kind. Deterministic, local, bounded; a refused value is skipped, never fixed.

    One result per (kind, canonical value); the first occurrence wins.
    """
    found: list[FoundField] = []
    seen: set[tuple[str, str]] = set()
    offset = 0
    for line in text.split("\n"):
        start_of_line = offset
        offset += len(line) + 1
        match = _LINE.match(line)
        if match is None:
            continue
        kind = _label_kind(match.group("label"))
        if kind is None:
            continue
        raw = match.group("value")
        if _REDACTION_MARK in raw:
            continue
        try:
            canonical = canonicalize(kind, raw)
        except ProtectedValueRefusal:
            continue
        digest = value_digest(canonical)
        if (kind, digest) in seen:
            continue
        seen.add((kind, digest))
        found.append(
            FoundField(
                kind=kind,
                raw=raw,
                canonical=canonical,
                digest=digest,
                preview=preview(kind, canonical),
                start=start_of_line + match.start("value"),
                end=start_of_line + match.end("value"),
                line_start=start_of_line,
                line_end=start_of_line + len(line),
            )
        )
        if len(found) >= limit:
            break
    return found


def line_around(text: str, start: int) -> str:
    """The whole line of `text` containing offset `start`."""
    begin = text.rfind("\n", 0, start) + 1
    end = text.find("\n", start)
    return text[begin : len(text) if end < 0 else end]


def span_still_holds(text: str, *, start: int, end: int, kind: str, digest: str) -> bool:
    """Re-derive a candidate from its recorded span: the same text, canonicalised, has the same digest."""
    if not 0 <= start < end <= len(text):
        return False
    try:
        return value_digest(canonicalize(kind, text[start:end])) == digest
    except ProtectedValueRefusal:
        return False


# ---- the exact adoption approval ------------------------------------------------------------------


class AdoptionProposal(BaseModel):
    """What one adoption approval binds. Digest-only: the raw value is never in the action ledger.

    Changing the candidate, its value, its source document (by text digest), the disclosure or the
    projection changes the proposal digest, so an approval of the old statement authorises nothing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["workflow_value_adoption"] = ADOPTION_KIND
    policy_version: Literal["workflow-adopt-v1"] = ADOPTION_POLICY_VERSION
    workflow_id: uuid.UUID
    candidate_id: uuid.UUID
    data_kind: ProtectedKind
    provenance: Provenance
    value_digest: str = Field(pattern=_DIGEST)
    preview: str = Field(min_length=1, max_length=120)
    document_id: uuid.UUID
    document_text_sha256: str = Field(pattern=_DIGEST)
    disclosure_id: uuid.UUID | None = None
    projection_digest: str | None = Field(default=None, pattern=_DIGEST)
    #: Never a user-typed detail, and never the global saved details: a workflow-scoped value only.
    scope: Literal["workflow"] = "workflow"

    def proposal(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def digest(self) -> str:
        return _digest(self.proposal())


def parse_adoption(proposal: dict[str, Any]) -> AdoptionProposal:
    try:
        parsed = AdoptionProposal.model_validate(dict(proposal))
    except ValidationError:
        raise WorkflowRefusal("adoption_invalid") from None
    if (parsed.provenance == "provider_derived") != (parsed.disclosure_id is not None and parsed.projection_digest is not None):
        raise WorkflowRefusal("adoption_invalid")
    return parsed


def is_protected_kind(value: object) -> bool:
    return isinstance(value, str) and value in PROTECTED_KINDS


__all__ = [
    "ADOPTION_KIND",
    "ADOPTION_POLICY_VERSION",
    "ADOPT_TOOL",
    "LABELS",
    "MAX_CANDIDATES_PER_SOURCE",
    "MAX_CANDIDATES_PER_WORKFLOW",
    "MAX_OBJECTIVE_CHARS",
    "PROVENANCES",
    "STATE_CODES",
    "WORKFLOW_KIND",
    "WORKFLOW_ROLES",
    "WORKFLOW_TTL_SECONDS",
    "AdoptionProposal",
    "FoundField",
    "Provenance",
    "WorkflowRefusal",
    "WorkflowRole",
    "find_fields",
    "is_protected_kind",
    "parse_adoption",
    "span_still_holds",
    "validate_objective",
]
