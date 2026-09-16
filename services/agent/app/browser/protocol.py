"""The wire contract between the trusted runtime and the isolated browser worker.

This is a *closed* protocol. There is exactly one request shape, it names an
operation from a fixed registry, and it carries a typed input for that operation.
There is no endpoint that takes JavaScript, a selector, a URL to click, or a
script to run: the worker's whole vocabulary is the registry, and the registry is
code that a human reviewed.

Identity travels with every dispatch:

* `runtime_generation` -- which runtime process is asking. The worker echoes it,
  and the runtime discards any answer addressed to a previous generation.
* `expected_worker_generation` -- which worker process the runtime believes it is
  talking to. A worker that has restarted has a new generation and refuses,
  rather than silently doing consequential work the runtime thinks it already
  dispatched elsewhere.
* `dispatch_id` -- one row in `browser_dispatches`, unique per execution attempt.
  The worker deduplicates on it, so a retried or duplicated dispatch can never
  press the button twice.
"""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.browser_dispatch import (
    NO_EFFECT_STATUSES,
    LookupStatus,
    OperationStatus,
)

#: The credential header. A header, never a query parameter: URLs end up in
#: server logs, browser history, referrers and crash dumps.
WORKER_TOKEN_HEADER = "x-lumi-worker-token"

MAX_INPUT_BYTES = 8_000
MAX_OBSERVATION_BYTES = 16_000

#: Re-exported so the worker and the client have one import for the wire
#: contract, while the enums themselves stay in the domain layer.
__all__ = [
    "MAX_INPUT_BYTES",
    "MAX_OBSERVATION_BYTES",
    "NO_EFFECT_STATUSES",
    "WORKER_TOKEN_HEADER",
    "DispatchRequest",
    "DispatchResponse",
    "LookupStatus",
    "OperationStatus",
    "WorkerErrorBody",
    "WorkerIdentity",
]


class DispatchRequest(BaseModel):
    """One typed operation, addressed to one worker generation."""

    model_config = ConfigDict(extra="forbid")

    dispatch_id: uuid.UUID
    runtime_generation: uuid.UUID
    expected_worker_generation: uuid.UUID
    #: The action this work serves. Absent only for read-only discovery (search,
    #: slot observation) that happens before any action exists.
    action_id: uuid.UUID | None = None
    #: Present for consequential work; absent for a read-only lookup, which is
    #: not an execution attempt and never becomes one.
    attempt_id: uuid.UUID | None = None
    operation: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    #: A reviewed site name, resolved by the worker against its own allowlist.
    site: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    #: Validated against the operation's input model by the worker. It is never
    #: passed to the browser as-is.
    input: dict[str, Any] = Field(default_factory=dict)


class DispatchResponse(BaseModel):
    """What the worker did, and how sure it is."""

    model_config = ConfigDict(extra="forbid")

    dispatch_id: uuid.UUID
    runtime_generation: uuid.UUID
    worker_generation: uuid.UUID
    operation: str
    status: OperationStatus
    #: Bounded, structured, and never raw HTML.
    observation: dict[str, Any] = Field(default_factory=dict)
    #: Stable machine code, safe to branch on and safe to log.
    error_code: str | None = Field(default=None, max_length=64)
    duration_ms: int = Field(ge=0)
    #: True when a consequential submission was issued during this dispatch.
    #: The single most important bit in this response: it is what separates a
    #: known failure from an unknown outcome.
    submitted: bool = False
    #: True when this dispatch id had already been handled and the stored answer
    #: was replayed. No browser work happened.
    replayed: bool = False


class WorkerIdentity(BaseModel):
    """The worker's answer to "who are you, and are you still the same process"."""

    model_config = ConfigDict(extra="forbid")

    worker_generation: uuid.UUID
    started_at: str
    headless: bool
    #: Names only. The worker never tells the runtime its own credential, and
    #: origins are resolved worker-side from these names.
    sites: list[str]
    operations: list[str]


class WorkerErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    worker_generation: uuid.UUID | None = None
