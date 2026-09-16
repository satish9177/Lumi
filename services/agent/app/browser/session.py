"""Worker identity, and the credential that guards the channel to it.

Milestone 2 introduced `runtime_generations`: one row per runtime process, so
startup can tell its own in-flight work from work a dead process left behind.
A browser worker is a second process that can die independently, so it needs the
same trick, and no more than that.

A worker generation is minted once, in memory, when the worker starts. It is not
a lease: there is no heartbeat, no expiry and no renewal, because with one
worker at a time none of those change any decision. What it does buy is the one
guarantee this milestone needs -- a result produced by a worker process that no
longer exists, or that the runtime is no longer talking to, is refused rather
than written into the ledger.

How this becomes a lease later, without changing anything above it: add
`expires_at` and `heartbeat_at` to `browser_worker_generations`, have the worker
renew, and make "is this generation current" a query instead of an equality
check. Every caller already asks that question through
`BrowserSessionService.reject_stale`, so the call sites do not move.
"""

import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import SecretStr

#: 32 bytes of entropy, urlsafe-encoded. Minted by trusted bootstrap code and
#: handed to the worker out of band (an environment variable on a process the
#: runtime's operator starts), never derived from anything guessable and never
#: written to a log, a URL, the database or the Electron renderer.
TOKEN_BYTES = 32


def generate_worker_token() -> SecretStr:
    return SecretStr(secrets.token_urlsafe(TOKEN_BYTES))


def token_matches(presented: str | None, expected: SecretStr) -> bool:
    """Constant-time comparison, and a missing token is simply a mismatch."""
    if not presented:
        return False
    return hmac.compare_digest(presented, expected.get_secret_value())


@dataclass(frozen=True, slots=True)
class WorkerGeneration:
    """One run of the browser worker process."""

    id: uuid.UUID
    started_at: datetime

    @classmethod
    def mint(cls) -> "WorkerGeneration":
        return cls(id=uuid.uuid4(), started_at=datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class BrowserSession:
    """One browser context used for exactly one dispatch.

    Contexts are not shared between dispatches and never outlive one. There is
    no cookie jar, no storage state and no login to persist yet; when there is,
    it attaches here rather than to the worker process, so one task's session
    can never leak into another's.
    """

    id: uuid.UUID
    dispatch_id: uuid.UUID
    worker_generation: uuid.UUID
    origin: str
