"""Milestone 7b: the closed vocabulary of public web research.

This module holds every shape that crosses a trust boundary during a research
task, and nothing that executes. Five things live here:

1. **The step vocabulary** (`ResearchStep`). A planner in Electron main chooses
   exactly one of these per turn. It is a discriminated union that forbids
   unknown keys, so there is no field in which a model could smuggle a CSS
   selector, an XPath expression, a JavaScript snippet, a raw URL, an HTTP
   method, a header, a cookie, a click coordinate or a Playwright call. What a
   model can say is: "search for this text", "go to link l5 of observation o3",
   "observe tab t1", "scroll down", "go back", "open/close a tab", "finish".

2. **Opaque semantic refs.** `o<n>` an observation, `b<n>` a text block, `l<n>`
   an observed link, `r<n>` a search result, `t<n>` a task-owned tab, `s<n>` a
   seed address the *user* typed. A ref is meaningless outside the observation
   that issued it: the controller resolves `l5` to an address only for the
   document epoch that produced it, and the worker refuses it once its tab has
   committed a different document. A model cannot mint one by printing a
   plausible string, because resolution is a table lookup, not parsing.

3. **The immutable step proposal** (`ResearchProposal`). What one single-use
   `StepAuthorization` is bound to, digest and all: the operation, the resolved
   destination, the session, the tab, the document epoch, the grant and the
   scope digest. Nothing the page says afterwards can edit it.

4. **The grant scope** (`ResearchScope`). What the user allowed once, in the
   trusted UI, in words the card shows: operations, network rules, budgets and
   the providers that may receive page text. The model can neither create nor
   widen it, and every step is checked against it by deterministic code.

5. **Observations and grounded answers.** A bounded, hashed projection of what
   a page or a search actually showed, marked `untrusted_environment`, plus the
   deterministic check that an answer quotes only blocks that really exist.

Everything a website contributes is data. It has no authority over which
operation runs next, where the browser goes, what Lumi is allowed to do, or
what the final answer claims.
"""

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime
from enum import StrEnum
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.digest import canonical_json

RESEARCH_SCHEMA_VERSION = 1
PUBLIC_RESEARCH_TASK_TYPE = "public_research"
#: The reserved worker "site" name a research dispatch carries. Like
#: `public_web`, it maps to the worker's own destination policy, never to an
#: origin taken from the request.
PUBLIC_RESEARCH_SITE = "public_research"

MAX_OBJECTIVE_CHARS = 500
MAX_QUERY_CHARS = 200
MAX_BLOCKS = 120
MAX_BLOCK_CHARS = 500
MAX_TEXT_CHARS = 10_000
MAX_LINKS = 25
MAX_LINK_TEXT_CHARS = 120
MAX_RESULTS = 10
MAX_SNIPPET_CHARS = 300
MAX_TITLE_CHARS = 200
MAX_URL_CHARS = 2_048
MAX_HOST_CHARS = 253
MAX_REDIRECTS = 5
MAX_SEEDS = 3
MAX_ANSWER_CHARS = 1_200
MAX_QUOTE_CHARS = 300
MAX_EVIDENCE = 6
MAX_RECIPIENTS = 3
MAX_TAB_SLOTS = 5
MAX_SEQUENCE = 10_000

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
OBSERVATION_REF = r"^o[1-9][0-9]{0,3}$"
BLOCK_REF = r"^b[1-9][0-9]{0,2}$"
LINK_REF = r"^l[1-9][0-9]?$"
RESULT_REF = r"^r[1-9][0-9]?$"
TAB_REF = r"^t[1-5]$"
SEED_REF = r"^s[1-3]$"
_DIGEST = r"^[0-9a-f]{64}$"
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
#: Anything that looks like an address, an account or a long identifier has no
#: business in a search query built from a research objective. A query is the
#: one place a model chooses free text that leaves the machine, so the policy
#: that stops "helpfully" appending private details is here, not in a prompt.
_QUERY_FORBIDDEN = (
    (re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE), "query_contains_url"),
    (re.compile(r"\S+@\S+"), "query_contains_address"),
    (re.compile(r"\d{7,}"), "query_contains_identifier"),
    (re.compile(r"(?i)\b(?:password|passwd|api[_ -]?key|secret|token|otp)\b"), "query_contains_secret"),
)


def _plain(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("control characters are not allowed")
    return value


class ResearchRefusal(ValueError):
    """A refused research input. `code` is stable, safe to log and to show.

    A `ValueError` so that a validator which parses a step (the immutable
    proposal, for one) reports it as an ordinary validation failure rather than
    escaping as an unexpected exception.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"The research step was refused ({code}).")
        self.code = code


def parse_objective(value: Any) -> str:
    """The user's own words. Trusted as the objective, never as a command."""
    if not isinstance(value, str):
        raise ResearchRefusal("objective_invalid")
    objective = " ".join(value.split())
    if not objective or len(objective) > MAX_OBJECTIVE_CHARS:
        raise ResearchRefusal("objective_invalid")
    try:
        return _plain(objective)
    except ValueError:
        raise ResearchRefusal("objective_invalid") from None


def parse_search_query(value: Any) -> str:
    """A model-chosen query, checked as an egress payload rather than as prose."""
    if not isinstance(value, str):
        raise ResearchRefusal("query_invalid")
    query = " ".join(value.split())
    if not query or len(query) > MAX_QUERY_CHARS or _CONTROL.search(query):
        raise ResearchRefusal("query_invalid")
    for pattern, code in _QUERY_FORBIDDEN:
        if pattern.search(query):
            raise ResearchRefusal(code)
    return query


# ---- the step vocabulary -------------------------------------------------------


class ResearchOperation(StrEnum):
    """The closed set of things a research task can do. Nothing else exists."""

    SEARCH = "public_search"
    NAVIGATE = "navigate"
    OBSERVE = "observe"
    SCROLL = "scroll"
    HISTORY = "history"
    TAB = "tab"


#: The ledger tool name for each operation. One per operation, so a task's
#: action list reads as what actually happened.
TOOL_NAMES: dict[ResearchOperation, str] = {
    ResearchOperation.SEARCH: "research_search",
    ResearchOperation.NAVIGATE: "research_navigate",
    ResearchOperation.OBSERVE: "research_observe",
    ResearchOperation.SCROLL: "research_scroll",
    ResearchOperation.HISTORY: "research_history",
    ResearchOperation.TAB: "research_tab",
}
RESEARCH_TOOL_NAMES: frozenset[str] = frozenset(TOOL_NAMES.values())
#: Operations the isolated browser worker executes. `public_search` is not one
#: of them: it is a bounded JSON GET the trusted runtime makes itself, so no
#: search credential and no search-result HTML ever reaches the browser.
BROWSER_OPERATIONS: frozenset[ResearchOperation] = frozenset(
    {
        ResearchOperation.NAVIGATE,
        ResearchOperation.OBSERVE,
        ResearchOperation.SCROLL,
        ResearchOperation.HISTORY,
        ResearchOperation.TAB,
    }
)


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LinkTarget(_Step):
    """Link `ref` of observation `observation`. Valid only for its epoch."""

    kind: Literal["link"] = "link"
    observation: str = Field(pattern=OBSERVATION_REF)
    ref: str = Field(pattern=LINK_REF)


class ResultTarget(_Step):
    kind: Literal["result"] = "result"
    observation: str = Field(pattern=OBSERVATION_REF)
    ref: str = Field(pattern=RESULT_REF)


class SeedTarget(_Step):
    """An address the user themselves typed in the objective."""

    kind: Literal["seed"] = "seed"
    ref: str = Field(pattern=SEED_REF)


NavigationTarget = Annotated[LinkTarget | ResultTarget | SeedTarget, Field(discriminator="kind")]


class SearchStep(_Step):
    operation: Literal[ResearchOperation.SEARCH] = ResearchOperation.SEARCH
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)


class NavigateStep(_Step):
    operation: Literal[ResearchOperation.NAVIGATE] = ResearchOperation.NAVIGATE
    tab: str = Field(pattern=TAB_REF)
    target: NavigationTarget


class ObserveStep(_Step):
    operation: Literal[ResearchOperation.OBSERVE] = ResearchOperation.OBSERVE
    tab: str = Field(pattern=TAB_REF)


class ScrollStep(_Step):
    operation: Literal[ResearchOperation.SCROLL] = ResearchOperation.SCROLL
    tab: str = Field(pattern=TAB_REF)
    direction: Literal["down", "up"]


class HistoryStep(_Step):
    operation: Literal[ResearchOperation.HISTORY] = ResearchOperation.HISTORY
    tab: str = Field(pattern=TAB_REF)
    direction: Literal["back", "forward"]


class TabStep(_Step):
    operation: Literal[ResearchOperation.TAB] = ResearchOperation.TAB
    action: Literal["open", "activate", "close"]
    #: Required to activate or close; absent when opening (the controller
    #: allocates the next free slot, so a model cannot choose an identity).
    tab: str | None = Field(default=None, pattern=TAB_REF)

    @model_validator(mode="after")
    def _tab_matches_action(self) -> Self:
        if self.action == "open" and self.tab is not None:
            raise ValueError("opening a tab does not take a tab ref")
        if self.action != "open" and self.tab is None:
            raise ValueError("this tab action needs a tab ref")
        return self


ResearchStep = Annotated[
    SearchStep | NavigateStep | ObserveStep | ScrollStep | HistoryStep | TabStep,
    Field(discriminator="operation"),
]


class ResearchStepEnvelope(BaseModel):
    """The one step a caller submits. `request_id` makes the submission idempotent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    step: ResearchStep
    #: Planner calls main has spent on this task so far, for the shared budget.
    planner_calls: int = Field(default=0, ge=0, le=1_000)


def parse_step(payload: Any) -> ResearchStep:
    """Strictly parse one step. Anything outside the vocabulary is refused."""
    from pydantic import TypeAdapter, ValidationError

    try:
        step: ResearchStep = TypeAdapter(ResearchStep).validate_python(payload)
    except ValidationError:
        raise ResearchRefusal("unsupported_operation") from None
    return step


def operation_of(step: ResearchStep) -> ResearchOperation:
    return ResearchOperation(step.operation)


# ---- the grant scope -----------------------------------------------------------


class ResearchBudgets(BaseModel):
    """Hard stops. Reaching one ends the task with an honest partial result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(default=20, ge=1, le=200)
    max_observations: int = Field(default=30, ge=1, le=300)
    max_planner_calls: int = Field(default=20, ge=1, le=200)
    max_tabs: int = Field(default=5, ge=1, le=MAX_TAB_SLOTS)
    max_active_seconds: int = Field(default=300, ge=10, le=3_600)
    max_model_input_tokens: int = Field(default=60_000, ge=1_000, le=1_000_000)
    max_model_output_tokens: int = Field(default=8_000, ge=100, le=64_000)
    max_vision_calls: int = Field(default=2, ge=0, le=20)


class ResearchDisclosure(BaseModel):
    """Who may receive observed page text in order to plan and answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recipients: list[Literal["openai", "gemini", "deepseek", "scripted"]] = Field(
        min_length=1, max_length=MAX_RECIPIENTS
    )
    max_text_chars: int = Field(ge=1, le=MAX_TEXT_CHARS)

    @field_validator("recipients")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("recipients must be unique")
        return value


#: What the trusted card promises, in the order it is shown. These strings are
#: the contract: the runtime enforces each one, and the UI renders them.
ALLOWED_SUMMARY: tuple[str, ...] = (
    "public_search",
    "public_https_navigation",
    "follow_public_links",
    "read_page_text",
    "task_owned_tabs",
)
FORBIDDEN_SUMMARY: tuple[str, ...] = (
    "login",
    "forms_and_typing",
    "uploads_and_downloads",
    "purchases_and_payments",
    "messages",
    "files",
    "private_network",
    "non_get_requests",
)


class ResearchScope(BaseModel):
    """Exactly what one grant authorises. Immutable once the user confirms it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["public_research"] = "public_research"
    policy_version: str = Field(min_length=1, max_length=40)
    allowed_operations: list[ResearchOperation] = Field(min_length=1, max_length=10)
    allowed: list[str] = Field(default=list(ALLOWED_SUMMARY), max_length=16)
    forbidden: list[str] = Field(default=list(FORBIDDEN_SUMMARY), max_length=16)
    schemes: list[Literal["https", "http_test_origin"]] = Field(default=["https"], max_length=2)
    methods: list[Literal["GET", "HEAD"]] = Field(default=["GET", "HEAD"], max_length=2)
    #: "any_public" for ordinary research; a host list when configuration
    #: narrows it. Never a private, local or reserved destination either way.
    hosts: Literal["any_public"] | list[str] = "any_public"
    budgets: ResearchBudgets = Field(default_factory=ResearchBudgets)
    disclosure: ResearchDisclosure
    #: Addresses taken from the user's own objective text, if any.
    seeds: list[str] = Field(default_factory=list, max_length=MAX_SEEDS)

    @field_validator("allowed_operations")
    @classmethod
    def _unique_operations(cls, value: list[ResearchOperation]) -> list[ResearchOperation]:
        if len(set(value)) != len(value):
            raise ValueError("allowed operations must be unique")
        return value

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()

    def permits(self, operation: ResearchOperation) -> bool:
        return operation in self.allowed_operations

    def seed(self, ref: str) -> str | None:
        index = int(ref[1:]) - 1
        return self.seeds[index] if 0 <= index < len(self.seeds) else None


class GrantStatus(StrEnum):
    """A grant's life. Only `ACTIVE` authorises anything."""

    #: Displayed on the trusted card, authorising nothing at all.
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    #: The user declined, cancelled the task, or pressed Stop.
    REVOKED = "REVOKED"
    #: Its window closed. Never revived; a new grant is a new confirmation.
    EXPIRED = "EXPIRED"
    #: The task finished under it.
    COMPLETED = "COMPLETED"


OPEN_GRANT_STATUSES: frozenset[GrantStatus] = frozenset({GrantStatus.PENDING, GrantStatus.ACTIVE})


# ---- the immutable step proposal ------------------------------------------------


class ResearchProposal(BaseModel):
    """One step, as the ledger stores it and the authorization digest covers it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["public_research_step"] = "public_research_step"
    operation: ResearchOperation
    step_number: int = Field(ge=1, le=MAX_SEQUENCE)
    policy_version: str = Field(min_length=1, max_length=40)
    grant_id: uuid.UUID
    scope_digest: str = Field(pattern=_DIGEST)
    #: The validated step exactly as the planner chose it.
    step: dict[str, Any]
    #: Present for `navigate`: the destination the *controller* resolved from
    #: the ref, checked against the destination policy before it was written.
    #: A model never supplies this field; it never sees it either.
    destination_url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    destination_host: str | None = Field(default=None, max_length=MAX_HOST_CHARS)
    session_id: uuid.UUID | None = None
    tab: str | None = Field(default=None, pattern=TAB_REF)
    #: The document the ref was issued for. The worker refuses the step if its
    #: tab has committed a different document since.
    expected_document_epoch: int | None = Field(default=None, ge=1, le=MAX_SEQUENCE)
    #: The observation whose ref table resolves this step's target.
    source_observation_id: uuid.UUID | None = None

    @field_validator("step")
    @classmethod
    def _step_is_valid(cls, value: dict[str, Any]) -> dict[str, Any]:
        parse_step(value)
        return value


# ---- observations ----------------------------------------------------------------


class TextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=BLOCK_REF)
    text: str = Field(min_length=1, max_length=MAX_BLOCK_CHARS)

    @field_validator("text")
    @classmethod
    def _plain_text(cls, value: str) -> str:
        return _plain(value)


class ObservedLink(BaseModel):
    """A link the page offered. The model gets a ref, a label and a host.

    Deliberately no address: the controller and the worker hold the resolved
    URL, and a ref is all a planner needs to say "follow that one". A page
    therefore cannot get an arbitrary address in front of the model at all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=LINK_REF)
    text: str = Field(max_length=MAX_LINK_TEXT_CHARS)
    host: str = Field(min_length=1, max_length=MAX_HOST_CHARS)

    @field_validator("text", "host")
    @classmethod
    def _plain_fields(cls, value: str) -> str:
        return _plain(value)


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=RESULT_REF)
    title: str = Field(max_length=MAX_TITLE_CHARS)
    host: str = Field(min_length=1, max_length=MAX_HOST_CHARS)
    snippet: str = Field(default="", max_length=MAX_SNIPPET_CHARS)

    @field_validator("title", "host", "snippet")
    @classmethod
    def _plain_fields(cls, value: str) -> str:
        return _plain(value)


ObservationKind = Literal["page", "search_results", "tab_state"]


def compute_content_hash(
    *,
    kind: str,
    final_url: str,
    title: str,
    blocks: list[TextBlock],
    links: list[ObservedLink],
    results: list[SearchResult],
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "kind": kind,
                "final_url": final_url,
                "title": title,
                "blocks": [block.model_dump() for block in blocks],
                "links": [link.model_dump() for link in links],
                "results": [result.model_dump() for result in results],
            }
        ).encode("utf-8")
    ).hexdigest()


class ResearchObservation(BaseModel):
    """What one research step saw. Untrusted environment data, always."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    observation_id: uuid.UUID
    provenance: Literal["untrusted_environment"] = "untrusted_environment"
    kind: ObservationKind
    operation: ResearchOperation
    #: 1-based within the task, so `o<sequence>` is the model-facing ref.
    sequence: int = Field(ge=1, le=MAX_SEQUENCE)
    session_id: uuid.UUID | None = None
    tab: str | None = Field(default=None, pattern=TAB_REF)
    document_epoch: int = Field(default=1, ge=1, le=MAX_SEQUENCE)
    query: str | None = Field(default=None, max_length=MAX_QUERY_CHARS)
    requested_url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    final_url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    final_host: str | None = Field(default=None, max_length=MAX_HOST_CHARS)
    redirects: list[str] = Field(default_factory=list, max_length=MAX_REDIRECTS)
    title: str = Field(default="", max_length=MAX_TITLE_CHARS)
    settled: bool = True
    truncated: bool = False
    observed_at: datetime
    blocks: list[TextBlock] = Field(default_factory=list, max_length=MAX_BLOCKS)
    links: list[ObservedLink] = Field(default_factory=list, max_length=MAX_LINKS)
    results: list[SearchResult] = Field(default_factory=list, max_length=MAX_RESULTS)
    open_tabs: list[str] = Field(default_factory=list, max_length=MAX_TAB_SLOTS)
    total_text_chars: int = Field(default=0, ge=0)
    total_link_count: int = Field(default=0, ge=0)
    content_hash: str = Field(pattern=_DIGEST)

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
        if sum(len(block.text) for block in self.blocks) > MAX_TEXT_CHARS:
            raise ValueError("the observation exceeds its text budget")
        for prefix, items in (
            ("b", [block.id for block in self.blocks]),
            ("l", [link.id for link in self.links]),
            ("r", [result.id for result in self.results]),
        ):
            if items != [f"{prefix}{index}" for index in range(1, len(items) + 1)]:
                raise ValueError(f"{prefix} refs must be sequential")
        for url in (*([self.requested_url] if self.requested_url else []), *self.redirects):
            if not re.fullmatch(r"https?://[^\s]+", url):
                raise ValueError("observation URLs must be http(s)")
        if self.final_url is not None and not re.fullmatch(r"https?://[^\s]+", self.final_url):
            raise ValueError("observation URLs must be http(s)")
        if self.kind == "search_results" and (self.query is None or self.links or self.blocks):
            raise ValueError("a search observation carries a query and results only")
        if self.kind == "page" and self.final_url is None:
            raise ValueError("a page observation carries the address it ended up at")
        expected = compute_content_hash(
            kind=self.kind,
            final_url=self.final_url or "",
            title=self.title,
            blocks=self.blocks,
            links=self.links,
            results=self.results,
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


# ---- the final answer ------------------------------------------------------------

AnswerStatus = Literal["answered", "partial", "not_found", "not_verified"]
#: Why a research task stopped. Recorded with the answer, shown to the user.
StopReason = Literal[
    "goal_reached",
    "no_evidence",
    "budget_exhausted",
    "blocked",
    "planner_failed",
    "user_stopped",
    "outside_scope",
]


class ResearchEvidence(BaseModel):
    """One quote, bound to the observation and block that really showed it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation: str = Field(pattern=OBSERVATION_REF)
    block: str = Field(pattern=BLOCK_REF)
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)

    @field_validator("quote")
    @classmethod
    def _plain_quote(cls, value: str) -> str:
        return _plain(value)


class ResearchAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AnswerStatus
    stop_reason: StopReason
    answer: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)
    evidence: list[ResearchEvidence] = Field(default_factory=list, max_length=MAX_EVIDENCE)

    @field_validator("answer")
    @classmethod
    def _plain_answer(cls, value: str) -> str:
        return _plain(value)


class AnswerNotGroundedError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"The research answer is not grounded in what was observed ({code}).")
        self.code = code


def normalise_evidence_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _numbers(value: str) -> set[str]:
    return {
        match.replace(",", "")
        for match in _NUMBER.findall(unicodedata.normalize("NFKC", value))
    }


class BlockSource(Protocol):
    """Anything that can say which text block a ref names.

    Public research observations and account-private observations share one
    grounding rule, so the check is written once against this shape.
    """

    def block(self, block_id: str) -> TextBlock | None: ...


def verify_research_grounding(
    observations: Mapping[str, BlockSource], answer: ResearchAnswer
) -> None:
    """Refuse an answer the collected observations do not support.

    * `answered` and `partial` must cite at least one observation.
    * Every cited observation and block must exist in *this task's* set.
    * Every quote must occur in the block it cites.
    * Every number in the answer must occur in a quote, so a figure cannot be
      invented, borrowed from prior model knowledge, or taken from a block
      nobody cited.
    """
    if answer.status in ("answered", "partial") and not answer.evidence:
        raise AnswerNotGroundedError("no_evidence")
    quoted: list[str] = []
    for item in answer.evidence:
        observation = observations.get(item.observation)
        if observation is None:
            raise AnswerNotGroundedError("unknown_observation")
        block = observation.block(item.block)
        if block is None:
            raise AnswerNotGroundedError("unknown_block")
        quote = normalise_evidence_text(item.quote)
        if not quote or quote not in normalise_evidence_text(block.text):
            raise AnswerNotGroundedError("quote_not_in_block")
        quoted.append(item.quote)
    if answer.status in ("answered", "partial"):
        available: set[str] = set().union(*(_numbers(quote) for quote in quoted)) if quoted else set()
        if not _numbers(answer.answer) <= available:
            raise AnswerNotGroundedError("number_not_in_evidence")


# ---- seeds ------------------------------------------------------------------------

_SEED_PATTERN = re.compile(r"\bhttps?://[^\s<>\"']{1,2040}", re.IGNORECASE)


def extract_seeds(objective: str) -> list[str]:
    """Addresses the *user* typed, in order, de-duplicated and bounded.

    Only these become `s<n>` refs. The planner may ask to open one; it can
    never introduce an address of its own.
    """
    seeds: list[str] = []
    for match in _SEED_PATTERN.findall(objective):
        candidate = match.rstrip("),.;!?")
        if candidate not in seeds:
            seeds.append(candidate)
        if len(seeds) >= MAX_SEEDS:
            break
    return seeds


__all__ = [
    "ALLOWED_SUMMARY",
    "BROWSER_OPERATIONS",
    "FORBIDDEN_SUMMARY",
    "MAX_TAB_SLOTS",
    "OPEN_GRANT_STATUSES",
    "PUBLIC_RESEARCH_SITE",
    "PUBLIC_RESEARCH_TASK_TYPE",
    "RESEARCH_SCHEMA_VERSION",
    "RESEARCH_TOOL_NAMES",
    "TOOL_NAMES",
    "AnswerNotGroundedError",
    "BlockSource",
    "GrantStatus",
    "HistoryStep",
    "LinkTarget",
    "NavigateStep",
    "ObserveStep",
    "ObservedLink",
    "ResearchAnswer",
    "ResearchBudgets",
    "ResearchDisclosure",
    "ResearchEvidence",
    "ResearchObservation",
    "ResearchOperation",
    "ResearchProposal",
    "ResearchRefusal",
    "ResearchScope",
    "ResearchStep",
    "ResearchStepEnvelope",
    "ResultTarget",
    "ScrollStep",
    "SearchResult",
    "SearchStep",
    "SeedTarget",
    "TabStep",
    "TextBlock",
    "compute_content_hash",
    "extract_seeds",
    "normalise_evidence_text",
    "operation_of",
    "parse_objective",
    "parse_search_query",
    "parse_step",
    "verify_research_grounding",
]
