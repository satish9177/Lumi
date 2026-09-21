"""Milestone 9 S2: explicit desktop disclosure and read-only provider reasoning.

S1 let Lumi *read* a Windows application locally. S2 changes **who may see an already
captured snapshot**, and nothing else: the desktop worker stays observation-only, and this
module has no verb, no operation and no target handle.

```text
one exact observation (S1, desktop_private, persisted)
   -> trusted card: ONE provider, THIS snapshot, redacted, once
   -> trusted click: DesktopDiscloseScope grant (task_grants, kind=desktop_disclose)
   -> claim: grant ACTIVE -> COMPLETED and a UNIQUE disclosure row, in one transaction
   -> the redacted projection of THAT observation goes to Electron main
   -> exactly ONE provider attempt; no failover, no retry, no image
   -> a read-only, grounded result is recorded (or the failure, or OUTCOME_UNKNOWN)
```

**What a grant binds.** One task, one observation id, that observation's exact snapshot
digest, one surface identity, one recipient and model, one redaction policy, one provider
call, one expiry. Observing the window again produces a new observation and a new digest;
the old grant does not cover it. Approving one snapshot is not approval for whatever text
the same window shows next.

**What the provider receives.** A second, provider-specific projection (never the raw S1
snapshot): closed semantic fields only, at most 120 nodes and 8 KB of text, with every name
and text passed through the identifier redactor first. The top-level window's own title is
withheld (a title quoted inside other text is a known residual), and there is no handle,
process, AutomationId, class name, RuntimeId, coordinate or pattern metadata.

**What the provider can say.** A closed read-only result: an answer with quotes that are located
in projected controls, or `cannot_answer`. That proves the quotes are in the approved snapshot, not
that the answer is right. There is no field for an
operation, tool, action, focus, invoke, value, click, coordinate, approval, grant or
provider. A desktop disclosure grant is *not* a desktop control grant, and no control grant
exists in S2.

Everything read from the desktop is `untrusted_environment` text: it is data about the
world, never an instruction and never an approval.
"""

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.desktop.protocol import (
    CONTROL_REF_PATTERN,
    DesktopObservation,
    MAX_TEXT_PER_NODE,
    DesktopRole,
    clean_text,
)
from app.domain.authenticated import Recipient
from app.domain.digest import canonical_json
from app.domain.redaction import Redactor, is_redacted

DESKTOP_READ_TASK_TYPE: Final = "desktop_read"
DESKTOP_DISCLOSE_KIND: Final = "desktop_disclose"
DESKTOP_DISCLOSE_POLICY_VERSION: Final = "desktop-disclose-v1"
REDACTION_POLICY: Final = "identifier-redaction-v1"
PROJECTION_SCHEMA_VERSION: Final = 1
DESKTOP_PRIVATE: Final = "desktop_private"

#: A disclosure approval is short-lived. It is still single-use: expiry never makes it reusable.
DEFAULT_GRANT_TTL_SECONDS: Final = 10 * 60
#: A snapshot older than this is not offered for disclosure. The user takes a new observation.
MAX_OBSERVATION_AGE_SECONDS: Final = 10 * 60
#: A STARTED disclosure whose runtime never heard back is not left claiming to be in flight.
STALE_CLAIM_SECONDS: Final = 5 * 60

MAX_OBJECTIVE_CHARS: Final = 500
MAX_PROJECTED_NODES: Final = 120
MAX_PROJECTED_TEXT_BYTES: Final = 8 * 1024
MAX_ANSWER_CHARS: Final = 1_200
MAX_QUOTE_CHARS: Final = 200
MAX_EVIDENCE: Final = 6
MAX_MODEL_NAME: Final = 64

#: Exactly the node fields a provider may receive. A fixed, closed set: a node attribute that is
#: not listed here cannot reach a provider by being added to the S1 schema.
ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    "control_ref",
    "parent_ref",
    "role",
    "name",
    "text",
    "enabled",
    "visible",
    "focused",
    "selected",
    "checked",
    "expanded",
)

#: Closed reasons a provider may give for not answering.
CANNOT_ANSWER_REASONS: Final[tuple[str, ...]] = (
    "not_in_snapshot",
    "snapshot_incomplete",
    "unclear_question",
    "not_supported",
)

#: Stable, text-free failure codes recorded against a disclosure.
ERROR_MODEL_UNAVAILABLE: Final = "model_unavailable"
ERROR_INVALID_OUTPUT: Final = "invalid_output"
ERROR_NOT_GROUNDED: Final = "answer_not_grounded"
ERROR_RUNTIME_RESTART: Final = "runtime_restart"
ERROR_OBSERVATION_GONE: Final = "observation_unavailable"
PROVIDER_FAILURE_CODES: Final = frozenset({ERROR_MODEL_UNAVAILABLE, ERROR_INVALID_OUTPUT})

_DIGEST = r"^[0-9a-f]{64}$"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class DesktopDisclosureRefusal(ValueError):
    """A refused desktop-disclosure input or state. `code` is stable and text-free."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The desktop disclosure was refused ({code}).")
        self.code = code


#: Refusals that are *state* (the world moved on since the card), not a malformed request.
STATE_CODES: Final = frozenset(
    {
        "grant_not_found",
        "grant_not_pending",
        "grant_not_active",
        "grant_expired",
        "grant_changed",
        "observation_unavailable",
        "observation_changed",
        "observation_stale",
        "disclosure_not_started",
        "disclosure_already_recorded",
        "wrong_recipient",
    }
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_DISPLAY_DROPPED = str.maketrans({"\u0022": None, "\u201c": None, "\u201d": None, "\u201e": None, "\u00ab": None, "\u00bb": None})


def _clean_display(value: str) -> str:
    """Display text another program chose, made unable to imitate Lumi's own words."""
    kept = "".join(
        " " if unicodedata.category(character) in ("Zl", "Zp") or character == "\u0085" else character
        for character in clean_text(value)
        if unicodedata.category(character) != "Cf"
    )
    return " ".join(_CONTROL.sub(" ", kept.translate(_DISPLAY_DROPPED)).split())


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---- the grant scope -----------------------------------------------------------------------


class DisplayTarget(_Frozen):
    """What the trusted card shows to identify the window. Local display only.

    These strings are `untrusted_environment` (a hostile application chose its own title). They
    are never put in an event or a log, and cannot alter the card wording, the recipient or the
    scope: they are rendered as inert text. The window's own title is withheld from the provider
    (see `build_projection`); a title repeated inside other text is not.

    They are cleaned so they cannot imitate Lumi's own wording: control characters, bidirectional and
    other format characters, line separators and double-quote characters are removed.
    """

    application_label: str = Field(max_length=64)
    window_title: str = Field(max_length=120)

    @field_validator("application_label", "window_title")
    @classmethod
    def _plain(cls, value: str) -> str:
        return _clean_display(value)


class DesktopDiscloseScope(_Frozen):
    """Exactly what one disclosure grant authorises. Immutable once inserted.

    It permits one thing: letting *one* named provider see the redacted, bounded projection of
    *one* exact observation, once. It permits no action on any application.
    """

    schema_version: Literal[1] = 1
    kind: Literal["desktop_disclose"] = DESKTOP_DISCLOSE_KIND
    policy_version: Literal["desktop-disclose-v1"] = DESKTOP_DISCLOSE_POLICY_VERSION
    observation_id: uuid.UUID
    snapshot_digest: str = Field(pattern=_DIGEST)
    #: When Lumi captured that snapshot. The provider's answer is about *this*, not the live window.
    observed_at: datetime
    worker_generation: uuid.UUID
    surface_ref: str = Field(pattern=r"^s(?:[1-9]|1[0-6])$")
    surface_epoch: int = Field(ge=1)
    recipient: Recipient
    model: str = Field(pattern=_MODEL.pattern)
    allowed_fields: tuple[str, ...] = ALLOWED_FIELDS
    max_nodes: int = Field(default=MAX_PROJECTED_NODES, ge=1, le=MAX_PROJECTED_NODES)
    max_text_bytes: int = Field(default=MAX_PROJECTED_TEXT_BYTES, ge=1, le=MAX_PROJECTED_TEXT_BYTES)
    redaction_policy: Literal["identifier-redaction-v1"] = REDACTION_POLICY
    #: Exactly one provider attempt for this approval, and none after a failed or lost one.
    max_provider_calls: Literal[1] = 1
    #: If the recipient is unavailable Lumi stops. There is no second provider or model.
    failover: Literal["none"] = "none"
    classification: Literal["desktop_private"] = DESKTOP_PRIVATE
    display: DisplayTarget

    @field_validator("allowed_fields")
    @classmethod
    def _fixed_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != ALLOWED_FIELDS:
            raise ValueError("the projected fields are a fixed, closed set")
        return value

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))

    def withheld(self) -> tuple[str, ...]:
        """The display strings the provider must not receive back as node text (see `build_projection`)."""
        return tuple(item for item in (self.display.window_title, self.display.application_label) if item)


# ---- the provider projection ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Projection:
    """What one provider may see, plus the counts a trusted card and a diagnostic may show."""

    payload: dict[str, Any]
    digest: str
    node_count: int
    text_bytes: int
    redaction_count: int
    truncated: bool
    truncation: tuple[str, ...]

    def node(self, control_ref: str) -> dict[str, Any] | None:
        for node in self.payload["nodes"]:
            if node["control_ref"] == control_ref:
                return node  # type: ignore[no-any-return]
        return None


def _text_bytes(value: str | None) -> int:
    return len(value.encode("utf-8")) if value else 0


_PARTIAL_EMAIL = re.compile(r"[\w.+-]*@[\w.-]*$")
_PARTIAL_DIGITS = re.compile(r"\d{4,}$")


def _scrub_clip_edge(value: str) -> str:
    """S1 clips every string at 120 characters *before* redaction, which can cut an identifier in half so
    the redactor no longer recognises it. At a clipped edge, drop a trailing partial address or long digit
    run. This is best effort (not anonymisation): a string exactly 120 characters long is treated as clipped."""
    if len(value) < MAX_TEXT_PER_NODE:
        return value
    return _PARTIAL_DIGITS.sub("", _PARTIAL_EMAIL.sub("", value))


def _same_text(left: str, right: str) -> bool:
    return normalise_quote(left) == normalise_quote(right)


def build_projection(
    snapshot: dict[str, Any],
    *,
    observed_at: datetime,
    max_nodes: int = MAX_PROJECTED_NODES,
    max_text_bytes: int = MAX_PROJECTED_TEXT_BYTES,
    withhold: tuple[str, ...] = (),
) -> Projection:
    """The deterministic, redacted, bounded provider view of one persisted snapshot.

    The persisted S1 snapshot is validated against the closed `DesktopObservation` schema first;
    anything else is refused rather than serialised. Redaction is applied to every name and text
    before anything is counted, digested or returned, and the same output is what grounding later
    checks a quote against, so a raw identifier can never verify: it was never in the projection.
    """
    try:
        observation = DesktopObservation.model_validate(snapshot)
    except ValidationError:
        raise DesktopDisclosureRefusal("observation_invalid") from None
    if observation.classification != DESKTOP_PRIVATE:  # pragma: no cover - the schema is a Literal.
        raise DesktopDisclosureRefusal("observation_invalid")

    redactor = Redactor()
    # The window's own title is display text for the trusted card, not something the provider is given. UI
    # Automation names the top-level window node with it (and often a title-bar control too), and titles
    # routinely carry file names, customer names and document titles, so the top-level window node keeps
    # its role but not its name, and any node whose name or text is exactly a withheld display string is
    # emptied. A title quoted *inside* other text is not caught: a known residual, documented.
    withheld = [item for item in withhold if item]
    # ...and whatever the observation itself calls its top-level window, so a title that changed
    # between the surface list and the read is withheld from the nodes that repeat it too.
    withheld += [
        source.name for source in observation.nodes
        if source.role is DesktopRole.WINDOW and source.parent_ref is None and source.name
    ]
    nodes: list[dict[str, Any]] = []
    budget = 0
    kept: set[str] = set()
    cut_nodes = False
    cut_text = False
    for source in observation.nodes:
        if len(nodes) >= max_nodes:
            cut_nodes = True
            break
        raw_name = None if source.role is DesktopRole.WINDOW and source.parent_ref is None else source.name
        raw_name = None if raw_name and any(_same_text(raw_name, item) for item in withheld) else raw_name
        raw_text = None if source.text and any(_same_text(source.text, item) for item in withheld) else source.text
        name = redactor.redact(_scrub_clip_edge(raw_name)) if raw_name else None
        text = redactor.redact(_scrub_clip_edge(raw_text)) if raw_text else None
        if budget + _text_bytes(name) + _text_bytes(text) > max_text_bytes:
            cut_text = True
            break
        budget += _text_bytes(name) + _text_bytes(text)
        # A projected parent is always a projected node: nodes come in tree order and we stop at a
        # prefix, so a kept node's parent was kept before it.
        parent = source.parent_ref if source.parent_ref in kept else None
        node: dict[str, Any] = {"control_ref": source.control_ref, "role": source.role.value}
        if parent is not None:
            node["parent_ref"] = parent
        if name:
            node["name"] = name
        if text:
            node["text"] = text
        node["enabled"] = source.enabled
        node["visible"] = source.visible
        node["focused"] = source.focused
        if source.selected is not None:
            node["selected"] = source.selected
        if source.checked is not None:
            node["checked"] = source.checked.value
        if source.expanded is not None:
            node["expanded"] = source.expanded
        kept.add(source.control_ref)
        nodes.append(node)

    truncation = [reason.value for reason in observation.truncation]
    if cut_nodes and "nodes" not in truncation:
        truncation.append("nodes")
    if cut_text and "text" not in truncation:
        truncation.append("text")
    truncated = bool(observation.truncated or cut_nodes or cut_text)
    payload: dict[str, Any] = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "classification": DESKTOP_PRIVATE,
        "trust": "untrusted_environment",
        "observed_at": observed_at.astimezone(UTC).isoformat(timespec="seconds"),
        "truncated": truncated,
        "truncation": sorted(truncation),
        "node_count": len(nodes),
        "nodes": nodes,
    }
    for node in nodes:  # Defence in depth: a redactor that would still change a string is a bug.
        for key in ("name", "text"):
            if key in node and not is_redacted(node[key]):
                raise DesktopDisclosureRefusal("projection_not_redacted")
    return Projection(
        payload=payload,
        digest=_digest(payload),
        node_count=len(nodes),
        text_bytes=budget,
        redaction_count=sum(redactor.counts.values()),
        truncated=truncated,
        truncation=tuple(sorted(truncation)),
    )


# ---- the read-only result -------------------------------------------------------------------


class DesktopEvidence(_Frozen):
    control_ref: str = Field(pattern=CONTROL_REF_PATTERN.pattern)
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)

    @field_validator("quote")
    @classmethod
    def _one_plain_line(cls, value: str) -> str:
        if _CONTROL.search(_encodable(value)):
            raise ValueError("control characters are not allowed")
        return value


_ANSWER_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _encodable(value: str) -> str:
    """Refuse a lone surrogate: no UTF-8 encoder (a database driver's) accepts it, and the failure
    would quote the string."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("not encodable") from None
    return value


def _plain_text(value: str) -> str:
    """Answer prose: newlines and tabs are ordinary; every other control character is not."""
    if _ANSWER_CONTROL.search(_encodable(value)):
        raise ValueError("control characters are not allowed")
    normalised = value.strip()
    if not normalised:
        raise ValueError("must not be empty")
    return normalised


class DesktopAnswer(_Frozen):
    schema_version: Literal[1] = 1
    kind: Literal["answer"] = "answer"
    answer: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)
    evidence: list[DesktopEvidence] = Field(min_length=1, max_length=MAX_EVIDENCE)

    @field_validator("answer")
    @classmethod
    def _plain(cls, value: str) -> str:
        return _plain_text(value)


class DesktopCannotAnswer(_Frozen):
    schema_version: Literal[1] = 1
    kind: Literal["cannot_answer"] = "cannot_answer"
    reason: Literal["not_in_snapshot", "snapshot_incomplete", "unclear_question", "not_supported"]


DesktopReadResult = DesktopAnswer | DesktopCannotAnswer


def parse_read_result(payload: Any) -> DesktopReadResult:
    """Strictly read one provider result. Unknown keys refuse the whole result.

    The shape has nowhere to put an operation, a tool, an action, a value, a coordinate, an
    approval, a grant or a provider; `extra="forbid"` means a reply carrying one is refused, not
    trusted and not stripped.
    """
    if not isinstance(payload, dict):
        raise DesktopDisclosureRefusal("result_malformed")
    kind = payload.get("kind")
    try:
        if kind == "answer":
            return DesktopAnswer.model_validate(payload)
        if kind == "cannot_answer":
            return DesktopCannotAnswer.model_validate(payload)
    except ValidationError:
        raise DesktopDisclosureRefusal("result_malformed") from None
    raise DesktopDisclosureRefusal("result_malformed")


def normalise_quote(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _numbers(value: str) -> set[str]:
    return {match.replace(",", "") for match in _NUMBER.findall(unicodedata.normalize("NFKC", value))}


class AnswerNotGroundedError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"The desktop answer is not grounded in the approved snapshot ({code}).")
        self.code = code


MIN_QUOTE_CHARS: Final = 6


def verify_grounding(projection: Projection, result: DesktopReadResult) -> list[str]:
    """Refuse an answer the approved projection does not support; return the evidence to store.

    * Every `control_ref` must exist in *this* projection: an invented or another
      observation's ref names nothing.
    * Every quote must occur in that control's redacted name or text: the projection is the
      corpus, not the raw observation, so a quote of a raw identifier cannot verify. A quote shorter
      than six characters is only accepted when it is that control's whole name or text (a one-letter
      "quote" is a substring of almost anything and locates nothing).
    * Every number in the answer must occur in one of the quotes.
    * What is *stored* as evidence is the projection's own text for that control, never the provider's
      string, so a stored quote is always exactly what the approved snapshot said.

    This shows that the quotes are in the approved snapshot. It does not show that the answer is a
    correct reading of them: negations, spelled-out numbers and paraphrase are not checked.
    """
    if isinstance(result, DesktopCannotAnswer):
        return []
    quoted: list[str] = []
    stored: list[str] = []
    for item in result.evidence:
        node = projection.node(item.control_ref)
        if node is None:
            raise AnswerNotGroundedError("unknown_control")
        quote = normalise_quote(item.quote)
        fields = [node[key] for key in ("name", "text") if key in node]
        located = [field for field in fields if quote and quote in normalise_quote(field)]
        if not located:
            raise AnswerNotGroundedError("quote_not_in_control")
        if len(quote) < MIN_QUOTE_CHARS and not any(quote == normalise_quote(field) for field in located):
            raise AnswerNotGroundedError("quote_too_short")
        quoted.append(item.quote)
        stored.append(item.quote if item.quote in located[0] else located[0])
    available: set[str] = set().union(*(_numbers(quote) for quote in quoted))
    if not _numbers(result.answer) <= available:
        raise AnswerNotGroundedError("number_not_in_evidence")
    return stored


def validate_objective(value: object) -> str:
    """The user's typed question. Plain text, bounded, no control characters."""
    if not isinstance(value, str):
        raise DesktopDisclosureRefusal("objective_invalid")
    text = value.strip()
    if not text or len(text) > MAX_OBJECTIVE_CHARS or _CONTROL.search(text):
        raise DesktopDisclosureRefusal("objective_invalid")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise DesktopDisclosureRefusal("objective_invalid") from None
    return text


def validate_model(value: object) -> str:
    if not isinstance(value, str) or not _MODEL.fullmatch(value):
        raise DesktopDisclosureRefusal("model_invalid")
    return value


def observation_age_seconds(observed_at: datetime, *, now: datetime | None = None) -> float:
    reference = now or datetime.now(UTC)
    return (reference - observed_at).total_seconds()
