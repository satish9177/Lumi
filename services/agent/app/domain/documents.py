"""Milestone 10 S1: approved documents, local comparison, and exact provider disclosure.

```text
file root (READ) or one dropped file
   -> task-owned file ref (root + relative name, or the dropped file) bound to (volume, index, size, mtime, sha256)
   -> extraction in a contained helper (bytes in, bounded text out) -> a task-owned document
   -> LOCAL structural comparison (deterministic; nothing leaves the machine)
   -> optionally: a trusted card naming the documents, the exact redacted excerpt of each, ONE provider,
      ONE model and the person's purpose -> the trusted click -> claim (grant ACTIVE -> COMPLETED + UNIQUE
      disclosure row, committed) -> ONE provider attempt, no failover -> a closed, grounded comparison
```

**Classification.** Extracted text is `document_private` and `untrusted_environment`. It is never put in
conversation memory, desktop planning, research, voice, a task summary or another task. A provider sees
only the projection named on the card: at most `MAX_EXCERPT_BYTES` of each document, identifier-redacted,
under an opaque `d1` / `d2` reference -- never a file name, a folder, a path or a root.

**What a provider may say.** A closed comparison whose every finding quotes the excerpt it was shown, or
`cannot_compare`. There is no field for an action, a value to fill, a destination, a tool or a provider.
"""

import hashlib
import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.domain.authenticated import Recipient
from app.domain.digest import canonical_json
from app.domain.redaction import Redactor, is_redacted

DOCUMENT_TASK_TYPE: Final = "document_task"
DOCUMENT_DISCLOSE_KIND: Final = "document_disclose"
DOCUMENT_DISCLOSE_POLICY_VERSION: Final = "document-disclose-v1"
DOCUMENT_PRIVATE: Final = "document_private"
REDACTION_POLICY: Final = "identifier-redaction-v1"

#: A task holds a few documents, not a library.
MAX_FILES_PER_TASK: Final = 4
MAX_DISCLOSED_DOCUMENTS: Final = 2
MAX_EXCERPT_BYTES: Final = 6 * 1024
MAX_PURPOSE_CHARS: Final = 400
MAX_OBJECTIVE_CHARS: Final = 300
MAX_SUMMARY_CHARS: Final = 800
MAX_FINDINGS: Final = 8
MAX_FINDING_CHARS: Final = 300
MAX_QUOTE_CHARS: Final = 200
MIN_QUOTE_CHARS: Final = 6
MAX_PREVIEW_CHARS: Final = 2_000
#: Extracted text is kept for a day at most, like desktop observations.
RETENTION_SECONDS: Final = 24 * 60 * 60
DEFAULT_GRANT_TTL_SECONDS: Final = 10 * 60
STALE_CLAIM_SECONDS: Final = 5 * 60

ERROR_MODEL_UNAVAILABLE: Final = "model_unavailable"
ERROR_INVALID_OUTPUT: Final = "invalid_output"
ERROR_NOT_GROUNDED: Final = "answer_not_grounded"
ERROR_RUNTIME_RESTART: Final = "runtime_restart"
ERROR_DOCUMENT_GONE: Final = "document_unavailable"
PROVIDER_FAILURE_CODES: Final = frozenset({ERROR_MODEL_UNAVAILABLE, ERROR_INVALID_OUTPUT})

_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_MODEL: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DIGEST: Final = r"^[0-9a-f]{64}$"
_WORD: Final = re.compile(r"[^\W_][\w+#.-]*[\w+#]|[^\W_]", re.UNICODE)
_NUMBER: Final = re.compile(r"\d[\d,]*(?:\.\d+)?")
_STOPWORDS: Final = frozenset(
    "a an and are as at be by for from has have in into is it its of on or that the this to with we you your our "
    "will can not but if all any their they them than then there these those was were been being also such via "
    "per who what when where which while about more most other some each".split()
)


class DocumentRefusal(ValueError):
    """A refused document request or state. `code` is stable and never carries document text or a path."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The document request was refused ({code}).")
        self.code = code


#: Refusals that are *state* (the world moved on since the card), not a malformed request.
STATE_CODES: Final = frozenset(
    {
        "root_not_found",
        "root_revoked",
        "root_changed",
        "root_unavailable",
        "permission_missing",
        "file_not_found",
        "file_missing",
        "file_changed",
        "file_unavailable",
        "document_not_found",
        "document_expired",
        "grant_not_found",
        "grant_not_pending",
        "grant_not_active",
        "grant_expired",
        "grant_changed",
        "document_changed",
        "disclosure_not_started",
        "disclosure_already_recorded",
        "dropped_file_unavailable",
    }
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _plain_line(value: str, *, limit: int) -> str:
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if not text or len(text) > limit or _CONTROL.search(text):
        raise ValueError("not a plain, bounded line")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("not encodable") from None
    return text


def validate_purpose(value: object) -> str:
    try:
        return _plain_line(value if isinstance(value, str) else "", limit=MAX_PURPOSE_CHARS)
    except ValueError:
        raise DocumentRefusal("purpose_invalid") from None


def validate_objective(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        return _plain_line(value if isinstance(value, str) else "", limit=MAX_OBJECTIVE_CHARS)
    except ValueError:
        raise DocumentRefusal("objective_invalid") from None


def validate_model(value: object) -> str:
    if not isinstance(value, str) or not _MODEL.fullmatch(value):
        raise DocumentRefusal("model_invalid")
    return value


def validate_label(value: object) -> str:
    """A display label for a root. Inert text; it grants nothing."""
    try:
        return _plain_line(value if isinstance(value, str) else "", limit=64)
    except ValueError:
        raise DocumentRefusal("label_invalid") from None


def preview(text: str) -> str:
    return text[:MAX_PREVIEW_CHARS]


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- local structural comparison ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentShape:
    characters: int
    words: int
    lines: int
    headings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LocalComparison:
    """Deterministic, computed on this machine from both documents. Nothing is sent anywhere."""

    first: DocumentShape
    second: DocumentShape
    shared_terms: tuple[str, ...]
    only_first: tuple[str, ...]
    only_second: tuple[str, ...]
    overlap: float


def _terms(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for match in _WORD.finditer(unicodedata.normalize("NFKC", text).casefold()):
        word = match.group(0).strip(".-")
        if len(word) < 3 or word in _STOPWORDS or word.isdigit() or "@" in word:
            continue
        counts[word] += 1
    return counts


def _headings(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or len(stripped) > 48 or len(stripped.split()) > 5 or stripped.endswith((".", ",", ";")):
            continue
        if stripped.isupper() or stripped.istitle() or stripped.rstrip(":") != stripped:
            if stripped not in found:
                found.append(stripped)
        if len(found) >= 20:
            break
    return tuple(found)


def _shape(text: str) -> DocumentShape:
    return DocumentShape(
        characters=len(text),
        words=len(text.split()),
        lines=text.count("\n") + 1 if text else 0,
        headings=_headings(text),
    )


def compare_locally(first: str, second: str) -> LocalComparison:
    first_terms = _terms(first)
    second_terms = _terms(second)
    shared = sorted(
        set(first_terms) & set(second_terms), key=lambda term: (-(first_terms[term] + second_terms[term]), term)
    )
    only_first = sorted(set(first_terms) - set(second_terms), key=lambda term: (-first_terms[term], term))
    only_second = sorted(set(second_terms) - set(first_terms), key=lambda term: (-second_terms[term], term))
    union = len(set(first_terms) | set(second_terms))
    return LocalComparison(
        first=_shape(first),
        second=_shape(second),
        shared_terms=tuple(shared[:40]),
        only_first=tuple(only_first[:25]),
        only_second=tuple(only_second[:25]),
        overlap=round(len(shared) / union, 4) if union else 0.0,
    )


# ---- the disclosure scope and projection ---------------------------------------------------------


class DisclosedDocument(_Frozen):
    """One document on the card. `label` is display only (the file name the person recognises)."""

    doc_ref: Literal["d1", "d2"]
    document_id: uuid.UUID
    text_sha256: str = Field(pattern=_DIGEST)
    label: str = Field(min_length=1, max_length=255)


class DocumentDiscloseScope(_Frozen):
    """Exactly what one document-disclosure grant authorises. Immutable once inserted.

    One named provider and model may see the redacted, bounded excerpt of each listed document, once,
    for the person's stated purpose. Nothing else: not another provider, not a second call, not the full
    documents, not a file name or a path, and no action of any kind.
    """

    schema_version: Literal[1] = 1
    kind: Literal["document_disclose"] = DOCUMENT_DISCLOSE_KIND
    policy_version: Literal["document-disclose-v1"] = DOCUMENT_DISCLOSE_POLICY_VERSION
    task_id: uuid.UUID
    documents: tuple[DisclosedDocument, ...] = Field(min_length=1, max_length=MAX_DISCLOSED_DOCUMENTS)
    fields: tuple[Literal["excerpt"], ...] = ("excerpt",)
    max_excerpt_bytes: int = Field(default=MAX_EXCERPT_BYTES, ge=256, le=MAX_EXCERPT_BYTES)
    recipient: Recipient
    model: str = Field(pattern=_MODEL.pattern)
    purpose: str = Field(min_length=1, max_length=MAX_PURPOSE_CHARS)
    redaction_policy: Literal["identifier-redaction-v1"] = REDACTION_POLICY
    max_provider_calls: Literal[1] = 1
    failover: Literal["none"] = "none"
    classification: Literal["document_private"] = DOCUMENT_PRIVATE

    @field_validator("documents")
    @classmethod
    def _distinct(cls, value: tuple[DisclosedDocument, ...]) -> tuple[DisclosedDocument, ...]:
        refs = [item.doc_ref for item in value]
        ids = [item.document_id for item in value]
        if len(set(refs)) != len(refs) or len(set(ids)) != len(ids) or refs != ["d1", "d2"][: len(refs)]:
            raise ValueError("documents must be distinct and referenced in order")
        return value

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class Projection:
    payload: dict[str, Any]
    digest: str
    text_bytes: int
    redaction_count: int
    truncated: bool

    def excerpt(self, doc_ref: str) -> str | None:
        for item in self.payload["documents"]:
            if item["doc_ref"] == doc_ref:
                return str(item["excerpt"])
        return None


def _clip_bytes(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    clipped = encoded[:limit].decode("utf-8", errors="ignore")
    # Do not leave half an identifier at the edge for the redactor to miss.
    cut = max(clipped.rfind(" "), clipped.rfind("\n"))
    return (clipped[:cut] if cut > limit // 2 else clipped), True


def build_projection(scope: DocumentDiscloseScope, texts: dict[str, str]) -> Projection:
    """The deterministic provider view: excerpt of each document, clipped, then identifier-redacted.

    `texts` maps `doc_ref` to the document's full extracted text; the caller has verified each against
    the scope's `text_sha256` first. The same function recomputes the projection when the result comes
    back, so grounding checks quotes against exactly what the provider was allowed to see.
    """
    redactor = Redactor()
    documents: list[dict[str, Any]] = []
    total = 0
    truncated = False
    for item in scope.documents:
        text = texts.get(item.doc_ref)
        if text is None or text_sha256(text) != item.text_sha256:
            raise DocumentRefusal("document_changed")
        clipped, cut = _clip_bytes(text, scope.max_excerpt_bytes)
        excerpt = redactor.redact(clipped)
        if not is_redacted(excerpt):  # pragma: no cover - defence in depth.
            raise DocumentRefusal("projection_not_redacted")
        truncated = truncated or cut
        total += len(excerpt.encode("utf-8"))
        documents.append({"doc_ref": item.doc_ref, "excerpt": excerpt, "truncated": cut})
    payload: dict[str, Any] = {
        "schema_version": 1,
        "classification": DOCUMENT_PRIVATE,
        "trust": "untrusted_environment",
        "purpose": scope.purpose,
        "documents": documents,
    }
    return Projection(
        payload=payload,
        digest=_digest(payload),
        text_bytes=total,
        redaction_count=sum(redactor.counts.values()),
        truncated=truncated,
    )


# ---- the provider's closed result ------------------------------------------------------------------


def _one_line(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("control characters are not allowed")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("not encodable") from None
    return value


class Evidence(_Frozen):
    doc_ref: Literal["d1", "d2"]
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)

    @field_validator("quote")
    @classmethod
    def _plain(cls, value: str) -> str:
        return _one_line(value)


class Finding(_Frozen):
    kind: Literal["match", "gap", "difference"]
    text: str = Field(min_length=1, max_length=MAX_FINDING_CHARS)
    evidence: tuple[Evidence, ...] = Field(min_length=1, max_length=3)

    @field_validator("text")
    @classmethod
    def _plain(cls, value: str) -> str:
        return _one_line(value).strip()


class Comparison(_Frozen):
    schema_version: Literal[1] = 1
    kind: Literal["comparison"] = "comparison"
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    findings: tuple[Finding, ...] = Field(min_length=1, max_length=MAX_FINDINGS)

    @field_validator("summary")
    @classmethod
    def _plain(cls, value: str) -> str:
        return _one_line(value).strip()


class CannotCompare(_Frozen):
    schema_version: Literal[1] = 1
    kind: Literal["cannot_compare"] = "cannot_compare"
    reason: Literal["not_in_documents", "documents_incomplete", "unclear_purpose", "not_supported"]


CompareResult = Comparison | CannotCompare


def parse_compare_result(payload: Any) -> CompareResult:
    if not isinstance(payload, dict):
        raise DocumentRefusal("result_malformed")
    try:
        if payload.get("kind") == "comparison":
            return Comparison.model_validate(payload)
        if payload.get("kind") == "cannot_compare":
            return CannotCompare.model_validate(payload)
    except ValidationError:
        raise DocumentRefusal("result_malformed") from None
    raise DocumentRefusal("result_malformed")


def normalise_quote(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _numbers(value: str) -> set[str]:
    return {match.replace(",", "") for match in _NUMBER.findall(unicodedata.normalize("NFKC", value))}


class NotGroundedError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"The comparison is not grounded in the disclosed excerpts ({code}).")
        self.code = code


def verify_grounding(projection: Projection, result: CompareResult) -> list[list[dict[str, str]]]:
    """Every quote must occur (after `normalise_quote`) in the excerpt of the document it names; every number
    in a finding must appear in that finding's quotes. Returns the evidence to store: the provider's quote as
    given, verified to occur in the projection -- not a copy of the projection's own span."""
    if isinstance(result, CannotCompare):
        return []
    stored: list[list[dict[str, str]]] = []
    for finding in result.findings:
        quotes: list[str] = []
        kept: list[dict[str, str]] = []
        for item in finding.evidence:
            excerpt = projection.excerpt(item.doc_ref)
            if excerpt is None:
                raise NotGroundedError("unknown_document")
            quote = normalise_quote(item.quote)
            haystack = normalise_quote(excerpt)
            if not quote or quote not in haystack:
                raise NotGroundedError("quote_not_in_document")
            if len(quote) < MIN_QUOTE_CHARS:
                raise NotGroundedError("quote_too_short")
            quotes.append(item.quote)
            kept.append({"doc_ref": item.doc_ref, "quote": item.quote})
        available: set[str] = set().union(*(_numbers(quote) for quote in quotes))
        if not _numbers(finding.text) <= available:
            raise NotGroundedError("number_not_in_evidence")
        stored.append(kept)
    if not _numbers(result.summary) <= set().union(
        *(_numbers(item["quote"]) for group in stored for item in group)
    ):
        raise NotGroundedError("number_not_in_evidence")
    return stored
