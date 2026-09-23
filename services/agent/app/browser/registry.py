"""The closed registry of reviewed browser operations.

Nothing outside this registry can be made to happen in a browser. A dispatch
names an operation; the name is looked up here or the dispatch is refused. There
is no path by which a model, a caller, or a web page can describe an operation
that is not already in this file -- no selector parameter, no URL parameter, no
script parameter, no "click this element" escape hatch.

Every operation declares more than a function. The declarations are what let the
runtime reason about an operation it did not write:

* **effect** -- may this change the outside world at all.
* **preconditions / postconditions** -- what must hold before it runs, and what
  must be *verified* before it may be called successful. A click that succeeded
  is not a booking that exists.
* **timeout** -- and, more importantly, what a timeout means for this operation.
* **retry classification** -- whether a lost answer may be retried, or whether it
  must go to reconciliation instead.
* **reconciliation** -- which read-only operation can establish the truth
  afterwards.

A consequential operation with no reconciliation path would be a design error:
there would be no way out of `OUTCOME_UNKNOWN` except guessing.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from playwright.async_api import Page
from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime only.
    from app.browser.authenticated_session import AuthenticatedReadSession
    from app.browser.local_form_draft import FormFreezeController
    from app.browser.research_session import ResearchBrowserSession

from app.browser.network_guard import PublicNetworkGuard
from app.browser.protocol import OperationStatus
from app.domain.browser_dispatch import BrowserEffect
from app.domain.public_url import PublicUrlPolicy

#: The domain's effect classification, re-exported so an adapter has one
#: import for everything it needs to declare an operation.
Effect = BrowserEffect


class RetryPolicy(StrEnum):
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    #: A lost answer must never be retried. Establish what happened first.
    RECONCILE_BEFORE_RETRY = "RECONCILE_BEFORE_RETRY"
    #: Nothing consequential can have happened, but the browser may have moved,
    #: so repeating the same step blindly would act on a document nobody looked
    #: at. Re-observe the tab and let the planner choose again.
    OBSERVE_THEN_REPLAN = "OBSERVE_THEN_REPLAN"
    #: Never repeated by Lumi, even though nothing consequential can have
    #: happened: a repeat is a new action the user approves again.
    NEW_APPROVAL_REQUIRED = "NEW_APPROVAL_REQUIRED"


class OperationTarget(StrEnum):
    """Where an operation's browser may go, and who decides."""

    #: A reviewed site: the worker resolves the request's site name to an
    #: origin from its own allowlist. The request never carries a URL.
    REVIEWED_SITE = "REVIEWED_SITE"
    #: One approved public page: the URL comes from the approved proposal and
    #: is checked by the worker's own destination policy and network guard.
    PUBLIC_PAGE = "PUBLIC_PAGE"
    #: Milestone 7b: a step inside a task-owned public research session. The
    #: destination comes from a semantic ref the worker itself issued (or from
    #: an address the user typed), and is checked by the same policy and guard.
    RESEARCH_SESSION = "RESEARCH_SESSION"
    #: Milestone 8a S3: a step inside the one persistent, Lumi-managed profile
    #: for one site, carrying the user's session. Structurally separate from
    #: `RESEARCH_SESSION`: a dispatch names exactly one kind of context, and the
    #: worker refuses an id of the other kind rather than falling back to it.
    AUTHENTICATED_SESSION = "AUTHENTICATED_SESSION"
    #: Milestone 10 S2: one approved public URL, fetched in a fresh, cookie-less context
    #: through the same destination guard as PUBLIC_PAGE, with its body captured into the
    #: worker's quarantine. The URL comes from the approved grant scope, never a page.
    DOWNLOAD = "DOWNLOAD"


class Reconciliation(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    LOOKUP_BOOKING = "lookup_booking"
    #: Milestone 10 S2: the quarantine's own markers (started / complete) are the evidence.
    INSPECT_QUARANTINE = "inspect_quarantine"


@dataclass(slots=True)
class OperationContext:
    """Everything one operation is allowed to touch.

    Note what is absent: no database connection, no settings object, no
    credentials, no approval API, no filesystem, no task history, no other
    task's data. An operation gets a page, an origin, its own typed input, and a
    place to record that it has issued a submission.
    """

    page: Page
    origin: str
    dispatch_id: uuid.UUID
    observation_id: uuid.UUID
    #: Set by the operation immediately *before* it issues a consequential
    #: request, never after. If the process dies between the flag and the click
    #: the flag is already lost with it; if it dies between the click and the
    #: response, the flag is what stops the failure being called a known one.
    submitted: bool = False
    #: PUBLIC_PAGE operations only: the worker's destination policy and the
    #: guard already installed on this page's context.
    public_policy: PublicUrlPolicy | None = None
    network_guard: PublicNetworkGuard | None = None
    #: RESEARCH_SESSION operations only: the task-owned session this step runs
    #: in, which owns its own policy, guard, tabs and ref tables.
    research_session: "ResearchBrowserSession | None" = None
    #: AUTHENTICATED_SESSION operations only: the read session of the one open
    #: persistent profile this step runs in.
    authenticated_session: "AuthenticatedReadSession | None" = None
    #: LOCAL_DRAFT operations only (Milestone 8b S6): the worker's freeze
    #: controller, which owns the two-layer network freeze and its owner.
    form_freeze: "FormFreezeController | None" = None
    #: DOWNLOAD operations only (Milestone 10 S2): the worker's configured quarantine root.
    #: The operation appends the runtime-minted transfer UUID itself; no request names a path.
    quarantine_root: str | None = None


@dataclass(frozen=True, slots=True)
class OperationResult:
    status: OperationStatus
    observation: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


Handler = Callable[[OperationContext, Any], Awaitable[OperationResult]]


@dataclass(frozen=True, slots=True)
class BrowserOperation:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    effect: Effect
    retry: RetryPolicy
    reconciliation: Reconciliation
    timeout_seconds: float
    #: What a timeout means here, in one line, for whoever reads the ledger.
    timeout_meaning: str
    preconditions: tuple[str, ...]
    postconditions: tuple[str, ...]
    handler: Handler
    target: OperationTarget = OperationTarget.REVIEWED_SITE

    def parse_input(self, payload: dict[str, Any]) -> BaseModel:
        return self.input_model.model_validate(payload)


class OperationRegistry:
    """An immutable name -> operation map. Nothing registers at request time."""

    def __init__(self, operations: tuple[BrowserOperation, ...]) -> None:
        duplicates = {op.name for op in operations if sum(o.name == op.name for o in operations) > 1}
        if duplicates:
            raise ValueError(f"duplicate browser operations: {sorted(duplicates)}")
        for operation in operations:
            if (
                operation.effect is Effect.CONSEQUENTIAL
                and operation.reconciliation is Reconciliation.NOT_REQUIRED
            ):
                raise ValueError(
                    f"consequential operation {operation.name!r} declares no reconciliation "
                    "path; there would be no way out of OUTCOME_UNKNOWN but a guess"
                )
            if operation.effect is Effect.CONSEQUENTIAL and operation.retry is not (
                RetryPolicy.RECONCILE_BEFORE_RETRY
            ):
                raise ValueError(
                    f"consequential operation {operation.name!r} must not be safe to retry"
                )
            if operation.target is OperationTarget.PUBLIC_PAGE and (
                operation.effect is not Effect.READ_ONLY
                or operation.retry is RetryPolicy.SAFE_TO_RETRY
            ):
                raise ValueError(
                    f"public-page operation {operation.name!r} must be read-only and must "
                    "not be retried without a new approval"
                )
            if (
                operation.effect is Effect.ACCOUNT_READ
                and operation.target is not OperationTarget.AUTHENTICATED_SESSION
            ):
                raise ValueError(
                    f"account-read operation {operation.name!r} may only target an "
                    "authenticated session"
                )
            if operation.effect is Effect.LOCAL_DRAFT and (
                operation.target is not OperationTarget.AUTHENTICATED_SESSION
                or operation.retry is not RetryPolicy.NEW_APPROVAL_REQUIRED
            ):
                # A local draft is a write into the user's own signed-in browser.
                # It may only run in an authenticated session, and a repeat is
                # never automatic: it is a new exact approval, because the first
                # approval was spent and the draft may or may not still exist.
                raise ValueError(
                    f"local-draft operation {operation.name!r} may only target an authenticated "
                    "session and must require a new approval to repeat"
                )
            if operation.target is OperationTarget.AUTHENTICATED_SESSION and not (
                (
                    operation.effect is Effect.ACCOUNT_READ
                    and operation.retry is RetryPolicy.OBSERVE_THEN_REPLAN
                )
                or (
                    operation.effect is Effect.LOCAL_DRAFT
                    and operation.retry is RetryPolicy.NEW_APPROVAL_REQUIRED
                )
            ):
                raise ValueError(
                    f"authenticated operation {operation.name!r} must be an account read that is "
                    "recovered by re-observing, or a local draft that needs a new approval to repeat"
                )
            if (
                operation.target is OperationTarget.AUTHENTICATED_SESSION
                and operation.reconciliation is not Reconciliation.NOT_REQUIRED
            ):
                # There is no authoritative verifier for "did this site record
                # my visit". A booking-style reconciliation path reachable from
                # here would claim to establish something nothing can.
                raise ValueError(
                    f"authenticated operation {operation.name!r} must not declare a "
                    "reconciliation strategy"
                )
            if (operation.effect is Effect.DOWNLOAD) != (operation.target is OperationTarget.DOWNLOAD) or (
                operation.effect is Effect.DOWNLOAD
                and (
                    operation.retry is not RetryPolicy.RECONCILE_BEFORE_RETRY
                    or operation.reconciliation is not Reconciliation.INSPECT_QUARANTINE
                )
            ):
                # A download may only run in its own capture context, is never repeated
                # blindly, and is reconciled from the quarantine's markers.
                raise ValueError(
                    f"download operation {operation.name!r} must target a download context, "
                    "reconcile before any retry, and reconcile from the quarantine"
                )
            if operation.target is OperationTarget.RESEARCH_SESSION and (
                operation.effect is not Effect.READ_ONLY
                or operation.retry is not RetryPolicy.OBSERVE_THEN_REPLAN
            ):
                raise ValueError(
                    f"research operation {operation.name!r} must be read-only and must be "
                    "recovered by re-observing rather than by repeating itself"
                )
        self._operations = {operation.name: operation for operation in operations}

    def get(self, name: str) -> BrowserOperation | None:
        return self._operations.get(name)

    def names(self) -> list[str]:
        return sorted(self._operations)

    def __contains__(self, name: object) -> bool:
        return name in self._operations

    def __len__(self) -> int:
        return len(self._operations)


def build_registry() -> OperationRegistry:
    """The one registry the worker serves.

    Milestone 3 reviews exactly one site, so there is exactly one adapter. A
    second adapter is a second import here and a second entry in the worker's
    origin allowlist -- it is never a runtime registration. Milestone 7a adds
    one generic, read-only public-page operation beside it, and Milestone 7b
    adds five read-only research operations that run in a task-owned session.
    """
    from app.browser.adapters import appointment_fixture
    from app.browser.operations import authenticated, download, form_draft, public_page, research

    return OperationRegistry(
        appointment_fixture.OPERATIONS
        + public_page.OPERATIONS
        + research.OPERATIONS
        + authenticated.OPERATIONS
        + form_draft.OPERATIONS
        + download.OPERATIONS
    )
