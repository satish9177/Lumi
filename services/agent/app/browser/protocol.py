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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.browser_dispatch import (
    NO_EFFECT_STATUSES,
    LookupStatus,
    OperationStatus,
)
from app.domain.login_takeover import CredentialSignal, TakeoverSiteScope

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
    "ProfileSessionRequest",
    "ProfileSessionResponse",
    "SessionRequest",
    "SessionResponse",
    "TakeoverConfirmRequest",
    "TakeoverConfirmResponse",
    "TakeoverStartRequest",
    "TakeoverStartResponse",
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
    #: Milestone 7b: the task-owned research session this step runs in. The
    #: worker refuses a session id it has never opened rather than creating
    #: one, so a task can never be handed a browser with no history.
    session_id: uuid.UUID | None = None
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


class SessionRequest(BaseModel):
    """Open or close one research session, addressed to one worker generation."""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID
    runtime_generation: uuid.UUID
    expected_worker_generation: uuid.UUID


class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID
    worker_generation: uuid.UUID
    status: Literal["OPEN", "CLOSED", "NOT_FOUND"]
    open_tabs: list[str] = Field(default_factory=list)


class ProfileSessionRequest(BaseModel):
    """Open or close one persistent browser profile (Milestone 8a S1).

    Note what this request does **not** carry, and cannot be made to carry:

    * **No path.** The worker derives the profile directory from the id with
      `app/browser/profile_paths.py`, from its own configured base. A path is
      not a field here, so the runtime cannot name one and neither can anything
      upstream of it.
    * **No executable path, no `storageState`, no cookie file, no profile
      contents of any kind.**
    * **No site scope.** S1 opens a profile; it navigates nowhere. Origin scope
      belongs to the S3 grant, and adding it here before there is an operation
      to bound would be a field with no enforcement behind it.

    `recorded_chromium_build` is what the *database* believes last opened this
    profile. The worker compares it with its own build and refuses a downgrade
    before touching the directory.
    """

    model_config = ConfigDict(extra="forbid")

    profile_id: uuid.UUID
    runtime_generation: uuid.UUID
    expected_worker_generation: uuid.UUID
    recorded_chromium_build: str | None = Field(default=None, max_length=64)
    #: Milestone 8a S2. A visible Chromium window for a human-driven manual
    #: login. `False` (the default) keeps S1's headless-by-worker-setting
    #: behaviour for every other caller.
    headed: bool = False


class ProfileSessionResponse(BaseModel):
    """What the worker did with the profile, and which browser it used."""

    model_config = ConfigDict(extra="forbid")

    profile_id: uuid.UUID
    worker_generation: uuid.UUID
    status: Literal["OPEN", "CLOSED", "NOT_FOUND"]
    #: The build that opened it, so the runtime can record version progression.
    #: Absent when nothing was opened.
    chromium_build: str | None = Field(default=None, max_length=64)
    playwright_version: str | None = Field(default=None, max_length=32)
    #: Whether this worker holds the exclusive OS handle on the directory.
    lock_held: bool = False


#: A registrable domain, exactly the shape `browser_profiles.site` already
#: enforces. Echoing this back to the worker is not a caller-chosen URL: it
#: is the profile's own immutable, database-recorded site, so the worker can
#: navigate the takeover tab and judge its ending scope without ever holding
#: a database connection.
_SITE_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"


class TakeoverStartRequest(BaseModel):
    """Open the headed tab for a manual sign-in and hand it to the human.

    No URL field: `site` is the profile's own bound registrable domain, and
    the worker derives `https://{site}/` from it. There is no other
    destination this request can name.
    """

    model_config = ConfigDict(extra="forbid")

    profile_id: uuid.UUID
    runtime_generation: uuid.UUID
    expected_worker_generation: uuid.UUID
    site: str = Field(min_length=1, max_length=253, pattern=_SITE_PATTERN)


class TakeoverStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: uuid.UUID
    worker_generation: uuid.UUID
    status: Literal["OPEN", "PROFILE_NOT_OPEN", "NAVIGATION_FAILED"]


class TakeoverConfirmRequest(BaseModel):
    """End a takeover and run the deterministic post-takeover check.

    Carries the same bound `site`, for the same reason, and nothing else: no
    URL, no selector, no page content.
    """

    model_config = ConfigDict(extra="forbid")

    profile_id: uuid.UUID
    runtime_generation: uuid.UUID
    expected_worker_generation: uuid.UUID
    site: str = Field(min_length=1, max_length=253, pattern=_SITE_PATTERN)


class TakeoverConfirmResponse(BaseModel):
    """What the deterministic check found. Signals only -- never page text,
    never a title, never a URL."""

    model_config = ConfigDict(extra="forbid")

    profile_id: uuid.UUID
    worker_generation: uuid.UUID
    status: Literal["CHECKED", "PROFILE_NOT_OPEN"]
    scope: TakeoverSiteScope = TakeoverSiteScope.NO_PAGE
    credential_surface: bool = False
    signals: list[CredentialSignal] = Field(default_factory=list)
    #: A SHA-256 hash of a stable identity signal the worker located and
    #: hashed itself. Never the raw identity string, which never leaves the
    #: worker process.
    account_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)


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
