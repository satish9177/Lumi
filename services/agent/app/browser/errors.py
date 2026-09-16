"""Errors at the runtime/browser-worker boundary.

The distinction that matters here is the same one the action ledger makes: an
error raised *before* a consequential submission is a known failure, and an
error raised once a submission may have gone out is not. Every exception below
declares which it is, so no caller has to infer it from a message.
"""

import uuid


class BrowserWorkerError(Exception):
    """Base class. `outcome_is_known` decides what the ledger may record."""

    outcome_is_known = True
    code = "browser_worker_error"


class BrowserWorkerNotConfiguredError(BrowserWorkerError):
    code = "browser_worker_not_configured"

    def __init__(self) -> None:
        super().__init__(
            "No browser worker is configured. Set BROWSER_WORKER_URL and "
            "BROWSER_WORKER_TOKEN to enable browser execution."
        )


class BrowserWorkerUnavailableError(BrowserWorkerError):
    """The worker could not be reached *at all*, so nothing was dispatched.

    Raised only for failures that happen before the request is delivered --
    connection refused, DNS, TLS. A connection that dropped mid-request is a
    `BrowserWorkerLostResponseError` instead, because by then the worker may
    already have acted.
    """

    outcome_is_known = True
    code = "browser_worker_unavailable"

    def __init__(self, reason: str) -> None:
        super().__init__(f"The browser worker could not be reached: {reason}.")
        self.reason = reason


class BrowserWorkerLostResponseError(BrowserWorkerError):
    """The dispatch went out and no usable answer came back.

    This is the honest `OUTCOME_UNKNOWN` case at the RPC layer: a timeout, a
    dropped connection, or a malformed reply. The operation may have completed.
    """

    outcome_is_known = False
    code = "browser_worker_lost_response"

    def __init__(self, reason: str) -> None:
        super().__init__(f"The browser worker did not return a usable result: {reason}.")
        self.reason = reason


class BrowserWorkerRejectedError(BrowserWorkerError):
    """The worker refused the dispatch, so it certainly did not act.

    Authentication failure, an unknown operation, a stale worker generation or a
    disallowed origin all land here. Refusal happens before any browser work.
    """

    outcome_is_known = True
    code = "browser_worker_rejected"

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        super().__init__(f"The browser worker refused the dispatch: {reason}.")
        self.reason = reason
        self.status_code = status_code


class StaleWorkerResultError(BrowserWorkerError):
    """A result that does not belong to this runtime or this worker generation.

    Accepting it would let a dead worker, or a worker answering a previous
    runtime, write an outcome into the ledger. The result is discarded and the
    attempt is treated as having no answer.
    """

    outcome_is_known = False
    code = "stale_worker_result"

    def __init__(self, reason: str) -> None:
        super().__init__(f"Discarding a stale browser-worker result: {reason}.")
        self.reason = reason


class BrowserExecutionNotSupportedError(Exception):
    """The action's tool is not one the browser worker executes."""

    def __init__(self, action_id: uuid.UUID, tool_name: str) -> None:
        super().__init__(
            f"Action {action_id} uses tool {tool_name!r}, which has no browser implementation."
        )
        self.action_id = action_id
        self.tool_name = tool_name
