"""Milestone 8a S3: the closed vocabulary of authenticated account reading.

The capability is called `account_scoped_read`, and its definition is written
to be true:

    Lumi issues only GET and HEAD requests, to one site, in a browser carrying
    your session for that site. Lumi performs no intentional change. The
    website may still record the visit, mark content as read, update "last
    active", extend a session, record analytics, or write account activity.
    Lumi cannot prevent or reliably detect those effects.

It is **not** "read-only browsing", "no-effect browsing" or "invisible
browsing". None of those claims is true and none is made anywhere in Lumi.

What lives here, and what deliberately does not:

* **The step vocabulary.** `navigate`, `observe`, `reveal`, `tab`, `history`.
  A strict discriminated union that forbids unknown keys. There is no field
  for a URL, a host, a selector, a script, a coordinate, a method, a header, a
  cookie, a key press, text to type, or a provider. A model cannot express
  those things because the shape it writes into has nowhere to put them.
* **Opaque refs only.** `o<n>` an observation, `b<n>` a text block, `l<n>` a
  link, `t<n>` a tab. **No address is ever persisted or shown.** The worker
  owns the actual target table for each document epoch; the runtime and the
  model see refs, labels and hosts. An authenticated link can itself be a
  capability (`?invite=…`, a signed URL), so it is never mirrored into a table
  the way M7b mirrors public ones.
* **The immutable grant scope** (`AuthenticatedReadScope`), bound to exactly one
  profile, site, account fingerprint, profile revoke epoch and *one* provider.
* **Account-private observations and answers.** Their own types, their own
  tables, and a `classification` that is a property of the type, not of a
  column somebody might forget to filter on.

S4 (M8b) adds `AuthenticatedObservation` schema version 2: a bounded, value-free
form/element inventory and a per-tab `form_epoch` (see `authenticated_forms`).
It changes nothing about the step vocabulary above.

Not here: field writes, protected values,
drafts, uploads, downloads, click/invoke, keyboard input. Those are S4-S6 or
out of scope entirely.
"""

import hashlib
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

from app.domain.authenticated_forms import EMPTY_INVENTORY, FormInventory
from app.domain.digest import canonical_json
from app.domain.login_takeover import CredentialSignal
from app.domain.redaction import is_redacted
from app.domain.research import (
    BLOCK_REF,
    LINK_REF,
    MAX_BLOCK_CHARS,
    MAX_HOST_CHARS,
    MAX_SEQUENCE,
    MAX_TAB_SLOTS,
    MAX_TITLE_CHARS,
    OBSERVATION_REF,
    ObservedLink,
    ResearchAnswer,
    TextBlock,
    _plain,
    compute_content_hash,
)

AUTHENTICATED_READ_TASK_TYPE = "authenticated_read"
#: The reserved worker "site" name an authenticated dispatch carries. Like
#: `public_research`, it maps to a worker-side capability, never to an origin.
AUTHENTICATED_READ_SITE = "authenticated_read"
AUTHENTICATED_POLICY_VERSION: Literal["authenticated-read-v1"] = "authenticated-read-v1"
ACCOUNT_PRIVATE: Literal["account_private"] = "account_private"

#: Reviewed maxima. A grant may narrow these and can never exceed them; nothing
#: a model says can raise them. Smaller than M7b's public research by design.
MAX_AUTH_TEXT_CHARS = 4_000
MAX_AUTH_BLOCKS = 60
MAX_AUTH_LINKS = 25
MAX_AUTH_STEPS = 12
MAX_AUTH_OBSERVATIONS = 12
MAX_AUTH_PLANNER_CALLS = 12
MAX_AUTH_ANSWER_CALLS = 2
MAX_AUTH_TABS = 3
MAX_AUTH_ACTIVE_SECONDS = 300
MAX_AUTH_ORIGINS = 4

_DIGEST = r"^[0-9a-f]{64}$"
#: An authenticated read never has more than three tabs, so no step, proposal or
#: observation can name a fourth.
AUTH_TAB_REF = r"^t[1-3]$"

#: The one place a provider is named. Exactly one, chosen from a bounded list
#: main built from its own configuration -- never by a page or a model.
Recipient = Literal["openai", "gemini", "deepseek", "scripted"]


class AuthenticatedRefusal(ValueError):
    """A refused authenticated-read input. `code` is stable and safe to show."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The authenticated read was refused ({code}).")
        self.code = code


# ---- the step vocabulary -------------------------------------------------------


class AuthOperation(StrEnum):
    """The closed set of things an authenticated read can do. Nothing else exists."""

    NAVIGATE = "navigate"
    OBSERVE = "observe"
    REVEAL = "reveal"
    TAB = "tab"
    HISTORY = "history"


TOOL_NAMES: dict[AuthOperation, str] = {
    AuthOperation.NAVIGATE: "authenticated_navigate",
    AuthOperation.OBSERVE: "authenticated_observe",
    AuthOperation.REVEAL: "authenticated_reveal",
    AuthOperation.TAB: "authenticated_tab",
    AuthOperation.HISTORY: "authenticated_history",
}
AUTHENTICATED_TOOL_NAMES: frozenset[str] = frozenset(TOOL_NAMES.values())


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LinkTarget(_Step):
    """Link `ref` of observation `observation`. Valid only for its epoch."""

    kind: Literal["link"] = "link"
    observation: str = Field(pattern=OBSERVATION_REF)
    ref: str = Field(pattern=LINK_REF)


class BlockTarget(_Step):
    kind: Literal["block"] = "block"
    observation: str = Field(pattern=OBSERVATION_REF)
    ref: str = Field(pattern=BLOCK_REF)


class NavigateStep(_Step):
    operation: Literal[AuthOperation.NAVIGATE] = AuthOperation.NAVIGATE
    tab: str = Field(pattern=AUTH_TAB_REF)
    target: LinkTarget


class ObserveStep(_Step):
    operation: Literal[AuthOperation.OBSERVE] = AuthOperation.OBSERVE
    tab: str = Field(pattern=AUTH_TAB_REF)


class RevealStep(_Step):
    """Scroll one observed block or link into view. No keyboard, no coordinates."""

    operation: Literal[AuthOperation.REVEAL] = AuthOperation.REVEAL
    tab: str = Field(pattern=AUTH_TAB_REF)
    target: Annotated[LinkTarget | BlockTarget, Field(discriminator="kind")]


class TabStep(_Step):
    operation: Literal[AuthOperation.TAB] = AuthOperation.TAB
    action: Literal["open", "activate", "close"]
    tab: str | None = Field(default=None, pattern=AUTH_TAB_REF)

    @model_validator(mode="after")
    def _tab_matches_action(self) -> Self:
        if self.action == "open" and self.tab is not None:
            raise ValueError("opening a tab does not take a tab ref")
        if self.action != "open" and self.tab is None:
            raise ValueError("this tab action needs a tab ref")
        return self


class HistoryStep(_Step):
    operation: Literal[AuthOperation.HISTORY] = AuthOperation.HISTORY
    tab: str = Field(pattern=AUTH_TAB_REF)
    direction: Literal["back", "forward"]


AuthenticatedStep = Annotated[
    NavigateStep | ObserveStep | RevealStep | TabStep | HistoryStep,
    Field(discriminator="operation"),
]
_STEP_ADAPTER: TypeAdapter[AuthenticatedStep] = TypeAdapter(AuthenticatedStep)


def parse_authenticated_step(payload: Any) -> AuthenticatedStep:
    """Strictly parse one step. Anything outside the vocabulary is refused."""
    try:
        return _STEP_ADAPTER.validate_python(payload)
    except ValidationError:
        raise AuthenticatedRefusal("unsupported_operation") from None


def operation_of(step: AuthenticatedStep) -> AuthOperation:
    return AuthOperation(step.operation)


class AuthenticatedStepEnvelope(BaseModel):
    """The one step a caller submits. `request_id` makes it idempotent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    step: AuthenticatedStep
    planner_calls: int = Field(default=0, ge=0, le=1_000)


# ---- the grant scope -----------------------------------------------------------


class AuthenticatedBudgets(BaseModel):
    """Hard stops. Never larger than M7b's public research budgets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(default=MAX_AUTH_STEPS, ge=1, le=MAX_AUTH_STEPS)
    max_observations: int = Field(default=MAX_AUTH_OBSERVATIONS, ge=1, le=MAX_AUTH_OBSERVATIONS)
    max_planner_calls: int = Field(default=MAX_AUTH_PLANNER_CALLS, ge=1, le=MAX_AUTH_PLANNER_CALLS)
    max_answer_calls: int = Field(default=MAX_AUTH_ANSWER_CALLS, ge=1, le=MAX_AUTH_ANSWER_CALLS)
    max_tabs: int = Field(default=MAX_AUTH_TABS, ge=1, le=MAX_AUTH_TABS)
    max_active_seconds: int = Field(default=MAX_AUTH_ACTIVE_SECONDS, ge=10, le=MAX_AUTH_ACTIVE_SECONDS)
    #: Structurally zero. No authenticated screenshot ever reaches a provider,
    #: so there is no value a grant could carry that raises it.
    max_vision_calls: Literal[0] = 0


class AuthenticatedDisclosure(BaseModel):
    """Who receives account text, and how much. Exactly one recipient."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recipient: Recipient
    max_text_chars: int = Field(default=MAX_AUTH_TEXT_CHARS, ge=1, le=MAX_AUTH_TEXT_CHARS)
    max_blocks: int = Field(default=MAX_AUTH_BLOCKS, ge=1, le=MAX_AUTH_BLOCKS)
    #: Identifiers are reduced before sending. This is not anonymisation.
    identifiers_reduced: Literal[True] = True
    #: If the recipient is unavailable Lumi stops; it never tries another.
    failover: Literal["none"] = "none"


ALLOWED_SUMMARY: tuple[str, ...] = (
    "read_pages_on_site",
    "follow_links_within_site",
    "own_account_reading_tabs",
)
FORBIDDEN_SUMMARY: tuple[str, ...] = (
    "sign_in_for_you",
    "ask_for_password_or_code",
    "type_into_forms",
    "submit_anything",
    "leave_site",
    "uploads_and_downloads",
    "purchases_and_messages",
    "non_get_requests",
)


class AuthenticatedReadScope(BaseModel):
    """Exactly what one grant authorises. Immutable once the user confirms it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["authenticated_read"] = "authenticated_read"
    policy_version: Literal["authenticated-read-v1"] = "authenticated-read-v1"
    profile_id: uuid.UUID
    site: str = Field(min_length=3, max_length=MAX_HOST_CHARS)
    allowed_origins: list[str] = Field(min_length=1, max_length=MAX_AUTH_ORIGINS)
    allowed_operations: list[AuthOperation] = Field(min_length=1, max_length=5)
    methods: list[Literal["GET", "HEAD"]] = Field(default=["GET", "HEAD"], max_length=2)
    classification: Literal["account_private"] = ACCOUNT_PRIVATE
    allowed: list[str] = Field(default=list(ALLOWED_SUMMARY), max_length=16)
    forbidden: list[str] = Field(default=list(FORBIDDEN_SUMMARY), max_length=16)
    #: True, and shown on the card: reading can change website state.
    website_side_effects_possible: Literal[True] = True
    disclosure: AuthenticatedDisclosure
    budgets: AuthenticatedBudgets = Field(default_factory=AuthenticatedBudgets)
    #: The hash of the identity signal observed at login. Never the raw string.
    account_fingerprint: str = Field(pattern=_DIGEST)
    profile_revoke_epoch: int = Field(ge=0)

    @field_validator("allowed_operations")
    @classmethod
    def _unique_operations(cls, value: list[AuthOperation]) -> list[AuthOperation]:
        if len(set(value)) != len(value):
            raise ValueError("allowed operations must be unique")
        return value

    @field_validator("methods")
    @classmethod
    def _only_reads(cls, value: list[str]) -> list[str]:
        if not value or set(value) - {"GET", "HEAD"}:
            raise ValueError("only GET and HEAD may be granted")
        return value

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()

    def permits(self, operation: AuthOperation) -> bool:
        return operation in self.allowed_operations


# ---- the immutable step proposal ------------------------------------------------


class AuthenticatedProposal(BaseModel):
    """One step, as the ledger stores it and the authorization digest covers it.

    Note the absence of any address. The worker resolves the ref from its own
    per-epoch table; the ledger never learns where a private link points.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["authenticated_read_step"] = "authenticated_read_step"
    classification: Literal["account_private"] = ACCOUNT_PRIVATE
    operation: AuthOperation
    step_number: int = Field(ge=1, le=MAX_SEQUENCE)
    policy_version: str = Field(min_length=1, max_length=40)
    grant_id: uuid.UUID
    scope_digest: str = Field(pattern=_DIGEST)
    profile_id: uuid.UUID
    profile_revoke_epoch: int = Field(ge=0)
    step: dict[str, Any]
    tab: str | None = Field(default=None, pattern=AUTH_TAB_REF)
    expected_document_epoch: int | None = Field(default=None, ge=1, le=MAX_SEQUENCE)
    source_observation_id: uuid.UUID | None = None
    worker_generation: uuid.UUID | None = None

    @field_validator("step")
    @classmethod
    def _step_is_valid(cls, value: dict[str, Any]) -> dict[str, Any]:
        parse_authenticated_step(value)
        return value


# ---- observations ----------------------------------------------------------------


class AuthenticatedObservation(BaseModel):
    """What one authenticated step saw, **after** redaction. Untrusted data.

    Everything textual here is the redacted projection: it is what the provider
    receives, what grounding checks against, and what the database stores. The
    raw text never leaves the worker. There is no URL anywhere in this shape.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 1 = the S3 text/link observation, which has no element inventory. 2 = the
    #: S4 observation with a bounded, value-free form/element inventory. Every new
    #: observation is 2; a stored v1 row stays v1 and is never reinterpreted.
    schema_version: Literal[1, 2] = 2
    observation_id: uuid.UUID
    provenance: Literal["untrusted_environment"] = "untrusted_environment"
    classification: Literal["account_private"] = ACCOUNT_PRIVATE
    kind: Literal["page", "tab_state"]
    operation: AuthOperation
    #: 1-based within the task, so `o<sequence>` is the model-facing ref.
    sequence: int = Field(ge=1, le=MAX_SEQUENCE)
    profile_id: uuid.UUID
    tab: str | None = Field(default=None, pattern=AUTH_TAB_REF)
    document_epoch: int = Field(default=1, ge=1, le=MAX_SEQUENCE)
    host: str | None = Field(default=None, max_length=MAX_HOST_CHARS)
    title: str = Field(default="", max_length=MAX_TITLE_CHARS)
    settled: bool = True
    truncated: bool = False
    observed_at: datetime
    blocks: list[TextBlock] = Field(default_factory=list, max_length=MAX_AUTH_BLOCKS)
    links: list[ObservedLink] = Field(default_factory=list, max_length=MAX_AUTH_LINKS)
    open_tabs: list[str] = Field(default_factory=list, max_length=MAX_TAB_SLOTS)
    total_text_chars: int = Field(default=0, ge=0)
    total_link_count: int = Field(default=0, ge=0)
    #: How many identifiers were reduced, by kind. Counts only.
    redactions: dict[str, int] = Field(default_factory=dict)
    content_hash: str = Field(pattern=_DIGEST)
    #: One monotonic counter per authenticated tab. It rises whenever the tab's
    #: form/control inventory changes, so a page that re-renders its form without
    #: navigating still invalidates every element ref. 0 on a v1 row.
    form_epoch: int = Field(default=0, ge=0, le=MAX_SEQUENCE)
    inventory: FormInventory = EMPTY_INVENTORY

    @field_validator("title")
    @classmethod
    def _plain_title(cls, value: str) -> str:
        return _plain(value)

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must carry a UTC offset")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if sum(len(block.text) for block in self.blocks) > MAX_AUTH_TEXT_CHARS:
            raise ValueError("the observation exceeds its private text budget")
        if any(len(block.text) > MAX_BLOCK_CHARS for block in self.blocks):
            raise ValueError("a block exceeds its size")
        for prefix, items in (
            ("b", [block.id for block in self.blocks]),
            ("l", [link.id for link in self.links]),
        ):
            if items != [f"{prefix}{index}" for index in range(1, len(items) + 1)]:
                raise ValueError(f"{prefix} refs must be sequential")
        if self.kind == "page" and self.host is None:
            raise ValueError("a page observation carries the host it is on")
        if self.kind == "tab_state" and (self.blocks or self.links or self.inventory.elements):
            raise ValueError("a tab-state observation carries no page content")
        if self.schema_version == 1 and (self.form_epoch != 0 or self.inventory != EMPTY_INVENTORY):
            raise ValueError("a version 1 observation has no form inventory")
        # The redaction guarantee is checked here as well as where it is
        # applied: text that still contains an identifier-shaped run was not
        # redacted where it should have been, and is never stored or sent.
        for text in (self.title, *(block.text for block in self.blocks), *(link.text for link in self.links)):
            if not is_redacted(text):
                raise ValueError("the observation still contains an unredacted identifier")
        expected = compute_content_hash(
            kind=self.kind,
            final_url=self.host or "",
            title=self.title,
            blocks=self.blocks,
            links=self.links,
            results=[],
        )
        if expected != self.content_hash:
            raise ValueError("content_hash does not match the observation")
        return self

    @property
    def ref(self) -> str:
        return f"o{self.sequence}"

    def block(self, block_id: str) -> TextBlock | None:
        for block in self.blocks:
            if block.id == block_id:
                return block
        return None


class CredentialSurfaceResult(BaseModel):
    """A credential surface was found. **Signals only** -- no title, no text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["credential_surface"] = "credential_surface"
    signals: list[CredentialSignal] = Field(min_length=1, max_length=8)
    tab: str | None = Field(default=None, pattern=AUTH_TAB_REF)
    document_epoch: int = Field(default=1, ge=1, le=MAX_SEQUENCE)
    observed_at: datetime


class IdentityCheckResult(BaseModel):
    """The page's account identity was unknown or differed. Nothing else."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["account_identity_unknown", "account_changed"]
    tab: str | None = Field(default=None, pattern=AUTH_TAB_REF)
    document_epoch: int = Field(default=1, ge=1, le=MAX_SEQUENCE)
    observed_at: datetime


class WorkerReadResult(BaseModel):
    """What the worker returned for a read: exactly one of three shapes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation: AuthenticatedObservation | None = None
    credential_surface: CredentialSurfaceResult | None = None
    identity: IdentityCheckResult | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if sum(item is not None for item in (self.observation, self.credential_surface, self.identity)) != 1:
            raise ValueError("a read yields exactly one kind of result")
        return self


# ---- answers ---------------------------------------------------------------------

#: An authenticated answer is a `ResearchAnswer` in shape (status, stop reason,
#: prose, quoted evidence). It is stored in its own table and carries its
#: classification from the task; it is never a `research_answers` row.
AuthenticatedAnswer = ResearchAnswer


# ---- pauses ----------------------------------------------------------------------


class PauseReason(StrEnum):
    """Why an authenticated task stopped and needs a human. Never model-chosen."""

    LOGIN_REQUIRED = "login_required"
    ACCOUNT_CHANGED = "account_changed"
    ACCOUNT_IDENTITY_UNKNOWN = "account_identity_unknown"
    LEFT_SITE_SCOPE = "left_site_scope"
    #: Milestone 8b S6. A local form draft exists in the browser window and waits for the
    #: user to discard it or take it over. Set by the controller, never by a model.
    FORM_DRAFT = "form_draft"
    #: The user took the browser over after a handover approval. Lumi does not know what
    #: the site accepted or saved, and never says.
    USER_TAKEOVER = "user_takeover"
    #: The browser that held a local draft is gone (a crash or a restart). The draft is lost.
    BROWSER_LOST = "browser_lost"


__all__ = [
    "ACCOUNT_PRIVATE",
    "ALLOWED_SUMMARY",
    "AUTH_TAB_REF",
    "AUTHENTICATED_POLICY_VERSION",
    "AUTHENTICATED_READ_SITE",
    "AUTHENTICATED_READ_TASK_TYPE",
    "AUTHENTICATED_TOOL_NAMES",
    "FORBIDDEN_SUMMARY",
    "MAX_AUTH_TEXT_CHARS",
    "TOOL_NAMES",
    "AuthOperation",
    "AuthenticatedAnswer",
    "AuthenticatedBudgets",
    "AuthenticatedDisclosure",
    "AuthenticatedObservation",
    "AuthenticatedProposal",
    "AuthenticatedReadScope",
    "AuthenticatedRefusal",
    "AuthenticatedStep",
    "AuthenticatedStepEnvelope",
    "BlockTarget",
    "CredentialSurfaceResult",
    "HistoryStep",
    "IdentityCheckResult",
    "LinkTarget",
    "NavigateStep",
    "ObserveStep",
    "PauseReason",
    "Recipient",
    "RevealStep",
    "TabStep",
    "WorkerReadResult",
    "operation_of",
    "parse_authenticated_step",
]
