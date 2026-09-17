"""The versioned page observation, the inspection proposal, and grounded answers.

A `PageObservation` is what one approved `inspect_public_page` dispatch saw. It
is deliberately small and flat: visible text split into numbered blocks, a
bounded list of links, where the browser actually ended up, and a content hash.
There is no DOM, no attribute bag, no screenshot, no cookie, no header and no
storage state -- nothing that could carry a secret the page never displayed.

Everything in it is `untrusted_environment` data. The only authority a page has
over Lumi is that its text may be quoted as evidence for an answer to the
question the *user* asked. Block and link ids are evidence references: an
answer must cite a block, and the cited quote must really be in that block.

Both Electron main (before it records an answer) and this runtime (when it
accepts one) run `verify_grounding`, so a model that invents a number, quotes a
sentence the page never showed, or cites a block that does not exist cannot get
that answer stored.
"""

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.digest import canonical_json

OBSERVATION_SCHEMA_VERSION = 1
PROPOSAL_SCHEMA_VERSION = 1

INSPECT_PUBLIC_PAGE = "inspect_public_page"
PAGE_INSPECTION_TASK_TYPE = "page_inspection"
#: The reserved "site" name public inspection dispatches carry. The worker maps
#: it to its configured destination policy, never to an origin.
PUBLIC_WEB_SITE = "public_web"

MAX_BLOCKS = 200
MAX_BLOCK_CHARS = 500
MAX_TEXT_CHARS = 12_000
MAX_LINKS = 20
MAX_LINK_TEXT_CHARS = 120
MAX_URL_CHARS = 2_048
MAX_TITLE_CHARS = 200
MAX_REDIRECTS = 5
MAX_QUESTION_CHARS = 500
MAX_ANSWER_CHARS = 600
MAX_QUOTE_CHARS = 300
MAX_EVIDENCE = 3
MAX_RECIPIENTS = 3

TEXT_PROVIDERS = ("openai", "gemini", "deepseek", "scripted")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BLOCK_ID = r"^b[1-9][0-9]{0,2}$"
_LINK_ID = r"^l[1-9][0-9]?$"
_DIGEST = r"^[0-9a-f]{64}$"
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _plain(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("control characters are not allowed")
    return value


class TextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_BLOCK_ID)
    text: str = Field(min_length=1, max_length=MAX_BLOCK_CHARS)

    @field_validator("text")
    @classmethod
    def _plain_text(cls, value: str) -> str:
        return _plain(value)


class PageLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_LINK_ID)
    text: str = Field(max_length=MAX_LINK_TEXT_CHARS)
    url: str = Field(min_length=1, max_length=MAX_URL_CHARS, pattern=r"^https?://[^\s]+$")

    @field_validator("text", "url")
    @classmethod
    def _plain_fields(cls, value: str) -> str:
        return _plain(value)


def compute_content_hash(
    *, final_url: str, title: str, blocks: list[TextBlock], links: list[PageLink]
) -> str:
    """SHA-256 over the canonical projection a model may see."""
    payload = {
        "final_url": final_url,
        "title": title,
        "blocks": [block.model_dump() for block in blocks],
        "links": [link.model_dump() for link in links],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class PageObservation(BaseModel):
    """What the worker returns for one inspection, validated before storage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    observation_id: uuid.UUID
    provenance: Literal["untrusted_environment"] = "untrusted_environment"
    requested_url: str = Field(min_length=1, max_length=MAX_URL_CHARS)
    final_url: str = Field(min_length=1, max_length=MAX_URL_CHARS)
    redirects: list[str] = Field(default_factory=list, max_length=MAX_REDIRECTS)
    title: str = Field(max_length=MAX_TITLE_CHARS)
    #: How many main-frame documents were committed during the dispatch. 1 for
    #: a plain page; more when the page replaced itself before it settled.
    document_epoch: int = Field(ge=1, le=1_000)
    #: The visible text stopped changing before the observation was taken.
    settled: bool
    observed_at: datetime
    blocks: list[TextBlock] = Field(max_length=MAX_BLOCKS)
    links: list[PageLink] = Field(max_length=MAX_LINKS)
    truncated: bool
    total_text_chars: int = Field(ge=0)
    total_link_count: int = Field(ge=0)
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
        if [block.id for block in self.blocks] != [f"b{i}" for i in range(1, len(self.blocks) + 1)]:
            raise ValueError("block ids must be sequential")
        if [link.id for link in self.links] != [f"l{i}" for i in range(1, len(self.links) + 1)]:
            raise ValueError("link ids must be sequential")
        for url in (self.requested_url, self.final_url, *self.redirects):
            if not re.fullmatch(r"https?://[^\s]+", url):
                raise ValueError("observation URLs must be http(s)")
        expected = compute_content_hash(
            final_url=self.final_url, title=self.title, blocks=self.blocks, links=self.links
        )
        if expected != self.content_hash:
            raise ValueError("content_hash does not match the observation")
        return self

    def block(self, block_id: str) -> TextBlock | None:
        for block in self.blocks:
            if block.id == block_id:
                return block
        return None


# ---- the proposal the user approves ---------------------------------------------


class DisclosureSpec(BaseModel):
    """Who may receive page text to answer the question, bound into the digest."""

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


class InspectionLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_blocks: int = MAX_BLOCKS
    max_text_chars: int = MAX_TEXT_CHARS
    max_links: int = MAX_LINKS
    max_redirects: int = MAX_REDIRECTS


class InspectionProposal(BaseModel):
    """Exactly what one approval authorises. Immutable once stored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    operation: Literal["inspect_public_page"] = "inspect_public_page"
    effect: Literal["public_read"] = "public_read"
    url: str = Field(min_length=1, max_length=MAX_URL_CHARS)
    host: str = Field(min_length=1, max_length=253)
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    policy_version: str = Field(min_length=1, max_length=40)
    limits: InspectionLimits = Field(default_factory=InspectionLimits)
    disclosure: DisclosureSpec

    @field_validator("question")
    @classmethod
    def _plain_question(cls, value: str) -> str:
        return _plain(value)


def parse_question(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("question must be text")
    question = " ".join(value.split())
    if not question or len(question) > MAX_QUESTION_CHARS:
        raise ValueError("question length")
    return _plain(question)


# ---- answers ------------------------------------------------------------------------

AnswerStatus = Literal["answered", "not_found", "ambiguous", "not_verified"]


class EvidenceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    block: str = Field(pattern=_BLOCK_ID)
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)

    @field_validator("quote")
    @classmethod
    def _plain_quote(cls, value: str) -> str:
        return _plain(value)


class PageAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AnswerStatus
    answer: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)
    evidence: list[EvidenceQuote] = Field(default_factory=list, max_length=MAX_EVIDENCE)

    @field_validator("answer")
    @classmethod
    def _plain_answer(cls, value: str) -> str:
        return _plain(value)


class AnswerNotGroundedError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"The answer is not grounded in the observation ({code}).")
        self.code = code


def normalise_evidence_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _numbers(value: str) -> set[str]:
    return {match.replace(",", "") for match in _NUMBER.findall(unicodedata.normalize("NFKC", value))}


def verify_grounding(observation: PageObservation, answer: PageAnswer) -> None:
    """Refuse an answer the observation does not support, deterministically.

    * An `answered` result must cite at least one block.
    * Every cited block must exist, and its quote must occur in that block.
    * Every number in an `answered` answer must occur in a cited quote, so a
      value cannot be invented, or borrowed from a block nobody cited.
    """
    if answer.status == "answered" and not answer.evidence:
        raise AnswerNotGroundedError("no_evidence")
    quoted: list[str] = []
    for item in answer.evidence:
        block = observation.block(item.block)
        if block is None:
            raise AnswerNotGroundedError("unknown_block")
        quote = normalise_evidence_text(item.quote)
        if not quote or quote not in normalise_evidence_text(block.text):
            raise AnswerNotGroundedError("quote_not_in_block")
        quoted.append(item.quote)
    if answer.status == "answered":
        available = set().union(*(_numbers(quote) for quote in quoted)) if quoted else set()
        if not _numbers(answer.answer) <= available:
            raise AnswerNotGroundedError("number_not_in_evidence")
