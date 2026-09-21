"""Vocabulary shared by the ledger, the wire protocol and the registry.

These enums live in `app/domain` rather than next to the worker so that the
database layer can name them without importing Playwright. `app/db/tables.py`
needs `DispatchStatus` and `BrowserEffect` to build CHECK constraints; it has no
business pulling a browser driver into the process that talks to PostgreSQL.
"""

from enum import StrEnum

from app.domain.action_status import AttemptOutcome


class BrowserEffect(StrEnum):
    """How far a reviewed browser operation can reach."""

    #: Reads a page. Changes nothing anywhere. Always safe to repeat.
    READ_ONLY = "READ_ONLY"
    #: Milestone 8a S3, user-facing name `account_scoped_read`. GET and HEAD
    #: only, to one site, in a browser carrying the user's session for it.
    #: Lumi performs no intentional change -- but the website may still record
    #: the visit (mark something read, update "last active", extend a session,
    #: write account activity), and Lumi can neither prevent nor detect that.
    #: Deliberately **not** `READ_ONLY`, because that word promises the opposite.
    ACCOUNT_READ = "ACCOUNT_READ"
    #: Navigates and fills fields. Browser-local state only; the site is not
    #: asked to do anything.
    PREPARE = "PREPARE"
    #: Milestone 8b S6. Writes only to browser-local, authenticated form controls
    #: while the browser is under the *verified* S6 network freeze: both the
    #: Playwright guard and the egress broker refuse everything, and no relay is
    #: open. Deliberately not `PREPARE`, which is the booking adapter's and does
    #: not carry that guarantee. It has no reconciliation path: there is no
    #: authoritative verifier for "did this site save my draft", and the freeze
    #: is what makes the question moot rather than answerable.
    LOCAL_DRAFT = "LOCAL_DRAFT"
    #: Can change the outside world irreversibly. Requires a durable approval,
    #: a persisted execution attempt, and a reconciliation path.
    CONSEQUENTIAL = "CONSEQUENTIAL"


class OperationStatus(StrEnum):
    """What the worker established about one operation.

    Deliberately not the ledger's vocabulary. The worker reports what it
    observed; only the runtime decides what an action's status becomes. Keeping
    the two apart is what stops a browser page from naming an action's outcome.
    """

    #: Completed, and the operation's postcondition was verified.
    OK = "OK"
    #: A consequential value no longer matched the approved proposal. Nothing
    #: was submitted, and this approval can never authorise the new values.
    CHANGED_RESOURCE = "CHANGED_RESOURCE"
    #: The target is gone, or the site definitively refused the submission.
    #: Either way the site has said that nothing was booked.
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    #: Failed, and the worker can show the failure happened before any
    #: submission was issued. A known failure: no side effect exists.
    FAILED_BEFORE_EFFECT = "FAILED_BEFORE_EFFECT"
    #: Failed at or after the submission. Whether the side effect happened is
    #: not knowable from the browser, and must not be guessed.
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class LookupStatus(StrEnum):
    """The authoritative answer a read-only reconciliation lookup can give.

    `UNKNOWN` is a first-class answer, not an error. "I could not tell" and
    "it is not there" are different facts, and a reconciler that conflates them
    resolves an action it has not actually established anything about.
    """

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"


class DispatchStatus(StrEnum):
    """A row in `browser_dispatches`: in flight, or one of the worker's answers."""

    DISPATCHED = "DISPATCHED"
    OK = "OK"
    CHANGED_RESOURCE = "CHANGED_RESOURCE"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    FAILED_BEFORE_EFFECT = "FAILED_BEFORE_EFFECT"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"

    @classmethod
    def of(cls, status: OperationStatus) -> "DispatchStatus":
        return cls(status.value)


#: Operation statuses that prove no consequential effect reached the site.
NO_EFFECT_STATUSES: frozenset[OperationStatus] = frozenset(
    {
        OperationStatus.CHANGED_RESOURCE,
        OperationStatus.RESOURCE_UNAVAILABLE,
        OperationStatus.FAILED_BEFORE_EFFECT,
    }
)


def attempt_outcome_for(status: OperationStatus) -> AttemptOutcome:
    """Translate the worker's finding into a ledger outcome.

    The only status that becomes `SUCCEEDED` is one where a postcondition was
    verified. The only status that becomes `OUTCOME_UNKNOWN` is one where the
    worker could not establish what happened. Everything else is a failure Lumi
    can actually stand behind, because the worker showed that nothing reached
    the site -- either it never submitted, or the site itself said no.
    """
    if status is OperationStatus.OK:
        return AttemptOutcome.SUCCEEDED
    if status is OperationStatus.OUTCOME_UNKNOWN:
        return AttemptOutcome.OUTCOME_UNKNOWN
    return AttemptOutcome.FAILED
