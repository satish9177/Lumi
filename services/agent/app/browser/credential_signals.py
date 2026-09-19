"""The deterministic credential-surface detector (Milestone 8a S2, plan §22b).

Runs against exactly one tracked page, after a takeover ends, before any
observation of any kind could be built from it. Every check here is a bounded
DOM *count*, never a text read: `locator(...).count()` answers "how many
elements match", not "what do they say".

**Over-inclusive on purpose.** A false positive costs one extra "finish
signing in yourself" message. A false negative would mean an authenticated
transition on a page that still needs credentials from the user, which is the
failure this module exists to prevent. When in doubt, report the signal.

**Not a proof.** A login form built without `type=password` -- a custom
canvas, an image-based keypad, a neutral-named cross-origin iframe -- will not
be detected. Passkey and WebAuthn prompts are native browser UI the DOM
cannot see at all. Stated here and in `docs/SECURITY.md`, not hidden.
"""

from playwright.async_api import Error as PlaywrightError, Page

from app.domain.login_takeover import CredentialSignal, hash_identity

#: Bounded, reviewed locator expressions. Each one is a *count*, not a read.
_PASSWORD_SELECTORS = (
    'input[type="password"]',
    '[autocomplete="current-password"]',
)
_NEW_PASSWORD_SELECTORS = ('[autocomplete="new-password"]',)
_OTP_SELECTORS = (
    '[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
)
_WEBAUTHN_SELECTORS = ('[autocomplete="webauthn"]',)

#: The fixed, documented convention the S2 synthetic fixture uses to expose a
#: stable per-account identity signal. A real site's equivalent signal is an
#: S3 design question; S2 builds the mechanism and proves it deterministically
#: against a fixture that follows this one convention.
ACCOUNT_IDENTITY_SELECTOR = "[data-lumi-account-id]"
ACCOUNT_IDENTITY_ATTRIBUTE = "data-lumi-account-id"

DETECTOR_TIMEOUT_SECONDS = 5.0


async def _count(page: Page, selector: str) -> int:
    try:
        return await page.locator(selector).count()
    except PlaywrightError:  # pragma: no cover - the page navigated away.
        return 0


async def detect_credential_surface(page: Page) -> list[CredentialSignal]:
    """Every over-inclusive signal this page currently shows. May be empty."""
    signals: list[CredentialSignal] = []
    for selectors, signal in (
        (_PASSWORD_SELECTORS, CredentialSignal.PASSWORD_FIELD),
        (_NEW_PASSWORD_SELECTORS, CredentialSignal.NEW_PASSWORD_FIELD),
        (_OTP_SELECTORS, CredentialSignal.ONE_TIME_CODE_FIELD),
        (_WEBAUTHN_SELECTORS, CredentialSignal.WEBAUTHN_HINT),
    ):
        for selector in selectors:
            if await _count(page, selector) > 0:
                signals.append(signal)
                break
    return signals


async def account_fingerprint(page: Page) -> str | None:
    """A SHA-256 hash of a bounded identity signal, or `None` if absent.

    The raw string is read, hashed and discarded in this one function. It is
    never returned, never logged and never stored -- only the hash is.
    `None` means "no stable signal was found", and callers must treat that as
    `unknown`, never as a fabricated identity.
    """
    try:
        locator = page.locator(ACCOUNT_IDENTITY_SELECTOR).first
        if await locator.count() == 0:
            return None
        raw = await locator.get_attribute(ACCOUNT_IDENTITY_ATTRIBUTE)
    except PlaywrightError:  # pragma: no cover - the page navigated away.
        return None
    if not raw:
        return None
    return hash_identity(raw)


__all__ = [
    "ACCOUNT_IDENTITY_ATTRIBUTE",
    "ACCOUNT_IDENTITY_SELECTOR",
    "DETECTOR_TIMEOUT_SECONDS",
    "account_fingerprint",
    "detect_credential_surface",
]
