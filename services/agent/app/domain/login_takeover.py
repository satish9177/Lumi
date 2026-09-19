"""Manual login and human takeover (Milestone 8a S2).

A takeover is a bounded interval in which the human, not Lumi, drives the
browser. What this module models is deliberately thin:

* **A login attempt is not an authorization to act.** It records only that a
  bounded interval existed in which the user controlled the browser, and how
  it ended. It funds nothing, grants nothing, and is never treated as proof
  that a sign-in succeeded.
* **`LoginAttemptStatus` is closed and small**, matching the state machine in
  `docs/plans/milestone-8.md` §8:

  ```text
  OPEN          the takeover is active; the human owns the browser
  UNCONFIRMED   the trusted "I'm signed in" click landed; the deterministic
                post-takeover check is running or has not yet completed
  COMPLETED     the check ran to completion (whatever it found)
  CANCELLED     the trusted "Cancel" click ended it; no authentication claim
  EXPIRED       the hard timeout elapsed; no authentication claim
  INTERRUPTED   the owning process died before the attempt could complete;
                no authentication claim
  ```

  There is no "FAILED" state: a click on "I'm signed in" followed by a
  completed check that still finds a login surface is `COMPLETED`, not a
  failure of the attempt -- the attempt did what an attempt does, which is
  bound an interval and produce one fresh, deterministic answer. Whether that
  answer was "authenticated" lives entirely on `browser_profiles.status`,
  never on the attempt.

* **`TakeoverSiteScope` is the only page-derived signal permitted to leave the
  worker while, or immediately after, a takeover is open.** Not a host
  string: a closed three-value enum, because anything richer starts to be an
  observation of the very login page this module exists to keep unobserved.

* **`CredentialSignal` is a detector's output, not a claim.** It is
  over-inclusive by design (see `docs/plans/milestone-8.md` §22b) and it is
  never accompanied by the text, title or URL of the page that produced it.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class LoginAttemptStatus(StrEnum):
    OPEN = "OPEN"
    UNCONFIRMED = "UNCONFIRMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    INTERRUPTED = "INTERRUPTED"


#: Statuses in which the human-driven interval is still (believed) live.
OPEN_LOGIN_ATTEMPT_STATUSES = (LoginAttemptStatus.OPEN, LoginAttemptStatus.UNCONFIRMED)
#: Statuses that are final and will never change again.
TERMINAL_LOGIN_ATTEMPT_STATUSES = (
    LoginAttemptStatus.COMPLETED,
    LoginAttemptStatus.CANCELLED,
    LoginAttemptStatus.EXPIRED,
    LoginAttemptStatus.INTERRUPTED,
)


class TakeoverSiteScope(StrEnum):
    """Where the takeover ended, as a closed enum -- never a host string."""

    #: The tracked page's registrable domain matches the profile's bound site.
    IN_PROFILE_SITE = "IN_PROFILE_SITE"
    #: A page exists, but its registrable domain is some other public site.
    OTHER_PUBLIC_SITE = "OTHER_PUBLIC_SITE"
    #: The tracked page/tab no longer exists (the user closed it).
    NO_PAGE = "NO_PAGE"


class CredentialSignal(StrEnum):
    """One over-inclusive signal a deterministic detector may report.

    Signals only. Never accompanied by page text, a title or a URL.
    """

    PASSWORD_FIELD = "PASSWORD_FIELD"
    NEW_PASSWORD_FIELD = "NEW_PASSWORD_FIELD"
    ONE_TIME_CODE_FIELD = "ONE_TIME_CODE_FIELD"
    WEBAUTHN_HINT = "WEBAUTHN_HINT"


#: Default and bounds for the takeover hard timeout. Fifteen minutes by
#: default, per the architecture review; configurable within a range that
#: keeps it a bounded, visible interval rather than an unattended one.
DEFAULT_TAKEOVER_TTL_SECONDS = 900
MIN_TAKEOVER_TTL_SECONDS = 60
MAX_TAKEOVER_TTL_SECONDS = 3_600


class TakeoverRefusal(Exception):
    """A refused takeover operation. `code` is stable, safe to log and show."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The login takeover operation was refused ({code}).")
        self.code = code


@dataclass(frozen=True, slots=True)
class LoginAttempt:
    """One row of `login_attempts`.

    Note what is absent: no page text, no URL, no credential signal, no
    account identity. Those never become durable state -- they exist only as
    transient values inside one request/response pair, and only the *verdict*
    they produce (a `browser_profiles.status` transition) is persisted.
    """

    id: UUID
    profile_id: UUID
    runtime_generation: UUID
    worker_generation: UUID | None
    profile_revision: int
    started_at: datetime
    expires_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None
    status: LoginAttemptStatus

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_LOGIN_ATTEMPT_STATUSES

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


def hash_identity(raw: str) -> str:
    """A stable, one-way fingerprint of an identity string. Never reversible.

    Used only where the *raw* string must not be the thing that persists --
    the caller hashes immediately and discards the input. This function
    itself never touches a database, a log or a network call.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_TAKEOVER_TTL_SECONDS",
    "MAX_TAKEOVER_TTL_SECONDS",
    "MIN_TAKEOVER_TTL_SECONDS",
    "OPEN_LOGIN_ATTEMPT_STATUSES",
    "TERMINAL_LOGIN_ATTEMPT_STATUSES",
    "CredentialSignal",
    "LoginAttempt",
    "LoginAttemptStatus",
    "TakeoverRefusal",
    "TakeoverSiteScope",
    "hash_identity",
]
