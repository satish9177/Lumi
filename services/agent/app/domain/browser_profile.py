"""What a Lumi-managed browser profile *is*, in the controller's vocabulary.

A profile is an identity and a lifecycle, and nothing else. Everything that
makes it valuable to an attacker -- cookies, tokens, `localStorage`,
IndexedDB, Chromium's credential database -- lives inside Chromium's own
profile directory and is never read, copied, serialised or summarised by Lumi.
The split is the whole design:

| Lumi owns | The browser owns |
|---|---|
| the profile id, its label, its site | every cookie and token |
| its status and version metadata | `localStorage`, `sessionStorage`, IndexedDB |
| who holds the lease, and until when | Chromium's `Login Data`, `Cookies`, `Web Data` |

So there is no `storage_state`, no cookie jar, no export and no import in this
milestone or any later one. A profile is opened by a browser or it is deleted;
those are the only two ways its contents are ever touched.

**One profile, one registrable domain.** The binding is decided once, at
creation, from the Public Suffix List, and it is immutable afterwards -- in the
service, and again in a database trigger. There is deliberately no operation
that re-points a profile at a different site: a profile carrying site A's
session cookies that starts calling itself site B is precisely the confusion
the one-site rule exists to prevent. Changing sites means deleting the profile
and creating another.

**Status is a lifecycle marker, never an authentication claim.** `NEW` means a
profile directory may not even exist yet; `NEEDS_LOGIN` means nobody has signed
in through it, or Lumi does not currently believe anyone has. `AUTHENTICATED`
exists in the vocabulary because S2 will need it, and **S1 contains no code path
that sets it** -- a profile is never called signed in merely because it opened.
"""

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.domain.public_suffix import PublicSuffixError, registrable_domain

#: Profile labels are shown to a person, so they have to be legible; they are
#: also never a path component, never a lookup key and never an authority
#: signal, so they only have to be *bounded*, not unique.
MAX_LABEL_LENGTH = 60
MIN_LABEL_LENGTH = 1
#: How long one runtime generation may hold a profile before another may
#: reclaim it. Renewed while a context is open; short enough that a crashed
#: generation does not lock a profile out for a working day.
DEFAULT_LEASE_TTL_SECONDS = 300

#: A closed, legible character set: word characters in any script, spaces, and
#: the punctuation a person actually types into a profile name -- including the
#: en and em dashes, because "GitHub - Personal" is usually written with one.
#: No angle brackets, no quotes, no backslash, no path separators that a naive
#: future consumer might treat as structure.
_ALLOWED_LABEL_CHARACTERS = re.compile(
    "^[\\w .,'()\\[\\]&/+@#!?:\\u2010-\\u2015-]+$", re.UNICODE
)
_COLLAPSE_SPACES = re.compile(r"\s+")


class ProfileStatus(StrEnum):
    """The lifecycle of one profile row.

    `DELETED` is terminal and the row stays: other tables will reference a
    profile with `ondelete=RESTRICT`, and a deleted profile's history has to
    remain readable. A `DELETED` row can never be reopened, re-leased or
    renamed.
    """

    #: Created in the database; its directory has not been opened yet.
    NEW = "NEW"
    #: A browser has opened it, and nobody has completed a sign-in through it
    #: (S1 never leaves this state by any other route).
    NEEDS_LOGIN = "NEEDS_LOGIN"
    #: Reserved for S2. **Nothing in S1 assigns this.**
    AUTHENTICATED = "AUTHENTICATED"
    #: Local state removed. Terminal.
    DELETED = "DELETED"


#: Statuses whose profile still has (or may have) a directory on disk.
LIVE_STATUSES = (ProfileStatus.NEW, ProfileStatus.NEEDS_LOGIN, ProfileStatus.AUTHENTICATED)


class BrowserContextKind(StrEnum):
    """Which kind of browser context a request is asking for.

    The two kinds are *not* interchangeable, and the dispatch paths refuse each
    other rather than falling back. A public research task must never be handed
    a context carrying somebody's session, and an authenticated profile must
    never be quietly substituted for the disposable context M7b expects.
    """

    #: Milestone 7b: task-owned, unauthenticated, disposable, no user data dir.
    RESEARCH_SESSION = "RESEARCH_SESSION"
    #: Milestone 8a: one persistent, Lumi-managed profile bound to one site.
    AUTHENTICATED_PROFILE = "AUTHENTICATED_PROFILE"


class ProfileRefusal(Exception):
    """A refused profile operation. `code` is stable, safe to log and to show."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The profile operation was refused ({code}).")
        self.code = code


def canonical_site(value: str) -> str:
    """A host the user or trusted configuration named -> its registrable domain.

    `https://Sub.GitHub.com/x` and `sub.github.com.` both become `github.com`.
    Anything without a registrable domain -- an address literal, a bare public
    suffix, `localhost`, a name with a port -- is refused here, before a row
    exists, so an unbindable site never becomes a profile.
    """
    raw = value.strip()
    if not raw:
        raise ProfileRefusal("site_required")
    # A caller may reasonably paste a URL; take its authority and nothing else.
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    try:
        return registrable_domain(raw)
    except PublicSuffixError as error:
        raise ProfileRefusal(f"site_{error.code}") from None


def canonical_label(value: str) -> str:
    """Bound and normalise the user-facing label. Never a path, never a key.

    NFC-normalised so two spellings of the same name compare equal, collapsed
    whitespace, no control characters, and a closed character set. The label
    exists for a person to recognise a profile by; it is not used to derive the
    directory name, is not unique, and grants nothing.
    """
    text = unicodedata.normalize("NFC", value).strip()
    # Any run of whitespace -- including a pasted newline or tab -- becomes one
    # space. Normalising is better than refusing here: the label is display
    # text, and a pasted name with a stray newline is a typo, not an attack.
    text = _COLLAPSE_SPACES.sub(" ", text).strip()
    # A control or format character that is *not* whitespace has no legitimate
    # place in a display name, and is how a name is made to render as another.
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise ProfileRefusal("label_control_characters")
    if not MIN_LABEL_LENGTH <= len(text) <= MAX_LABEL_LENGTH:
        raise ProfileRefusal("label_length")
    if not _ALLOWED_LABEL_CHARACTERS.fullmatch(text):
        raise ProfileRefusal("label_characters")
    return text


def allowed_origins_for(site: str) -> tuple[str, ...]:
    """The origins a profile bound to `site` may ever be navigated to.

    `https://github.com` and `https://www.github.com` -- the apex and its `www`
    alias, both `https:` only. Recorded on the row so S2/S3 have a fixed,
    reviewed list rather than deriving one from a page. S1 stores it and
    navigates nowhere.
    """
    return (f"https://{site}", f"https://www.{site}")


@dataclass(frozen=True, slots=True)
class BrowserVersions:
    """What opened this profile last. A profile is forward-compatible only.

    Chromium upgrades a profile directory in place and does not downgrade it,
    so a build older than the one that last wrote the profile must refuse to
    open it rather than risk corrupting a directory the user signed in through.
    """

    chromium_build: str
    playwright_version: str
    app_version: str

    def is_downgrade_from(self, recorded: "BrowserVersions | None") -> bool:
        if recorded is None:
            return False
        return _build_key(self.chromium_build) < _build_key(recorded.chromium_build)

    def is_upgrade_from(self, recorded: "BrowserVersions | None") -> bool:
        if recorded is None:
            return True
        return _build_key(self.chromium_build) > _build_key(recorded.chromium_build)


def _build_key(build: str) -> tuple[int, ...]:
    """Order two Chromium build strings.

    Playwright's Chromium build is a revision number (`1243`) and Chromium's own
    is dotted (`141.0.7390.54`); both compare correctly component-wise. A
    component that is not a number sorts as 0, which is conservative: an
    unparseable recorded build makes the comparison say "not newer", and the
    caller treats "not provably older" as the safe direction only for upgrades.
    """
    return tuple(int(part) if part.isdigit() else 0 for part in build.split("."))


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    """One row of `browser_profiles`, as the controller sees it.

    Note what is absent and will stay absent: the profile directory path, any
    cookie, any token, and the raw account identity. The path is derived inside
    the runtime and the worker from the id; it is not stored, not returned and
    not logged.
    """

    id: uuid.UUID
    label: str
    site: str
    allowed_origins: tuple[str, ...]
    status: ProfileStatus
    revision: int
    chromium_build: str | None
    playwright_version: str | None
    app_version: str | None
    lease_runtime_generation: uuid.UUID | None
    lease_expires_at: datetime | None
    revoke_epoch: int
    account_fingerprint: str | None
    account_label_hash: str | None
    last_login_completed_at: datetime | None
    last_observed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    @property
    def versions(self) -> BrowserVersions | None:
        if self.chromium_build is None or self.playwright_version is None:
            return None
        return BrowserVersions(
            chromium_build=self.chromium_build,
            playwright_version=self.playwright_version,
            app_version=self.app_version or "",
        )

    @property
    def is_deleted(self) -> bool:
        return self.status is ProfileStatus.DELETED


__all__ = [
    "DEFAULT_LEASE_TTL_SECONDS",
    "LIVE_STATUSES",
    "MAX_LABEL_LENGTH",
    "MIN_LABEL_LENGTH",
    "BrowserContextKind",
    "BrowserProfile",
    "BrowserVersions",
    "ProfileRefusal",
    "ProfileStatus",
    "allowed_origins_for",
    "canonical_label",
    "canonical_site",
]
