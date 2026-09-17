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
from typing import Any

from playwright.async_api import Page
from pydantic import BaseModel

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


class Reconciliation(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    LOOKUP_BOOKING = "lookup_booking"


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
    one generic, read-only public-page operation beside it.
    """
    from app.browser.adapters import appointment_fixture
    from app.browser.operations import public_page

    return OperationRegistry(appointment_fixture.OPERATIONS + public_page.OPERATIONS)
