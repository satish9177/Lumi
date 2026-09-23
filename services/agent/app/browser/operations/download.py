"""`download_to_quarantine`: fetch ONE approved URL into the worker's quarantine (Milestone 10 S2).

Its whole input is the approved URL, the transfer's UUID and a byte limit -- no path, header, cookie,
filename, selector or script. It composes a fixed sequence:

    check the URL -> create <quarantine>/<transfer_id>/started.json (BEFORE any request)
      -> navigate a fresh, cookie-less context through the destination guard in capture mode,
         following redirects one validated hop at a time
      -> the guard keeps the main document's body (bounded, GET only, public addresses only)
      -> sniff the bytes (never the name) -> write payload.bin, Mark-of-the-Web, then complete.json

Nothing is opened, executed, rendered or placed here. Placement into a user folder is a separate runtime
step with its own authorization. A body whose bytes are an executable, a script, a shortcut, a macro
document or an OLE container is refused and never reaches `payload.bin`.
"""

import hashlib
import uuid
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict, Field

from app.browser.network_guard import DownloadCapture
from app.browser.protocol import OperationStatus
from app.browser.registry import (
    BrowserOperation,
    Effect,
    OperationContext,
    OperationResult,
    OperationTarget,
    Reconciliation,
    RetryPolicy,
)
from app.documents.sniff import dangerous_kind, sniff
from app.domain.page_observation import MAX_REDIRECTS, MAX_URL_CHARS
from app.domain.public_url import UrlPolicyError
from app.domain.transfers import DOWNLOAD_TO_QUARANTINE
from app.files import quarantine

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_KINDS = frozenset({"pdf", "docx", "txt"})


class DownloadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1, max_length=MAX_URL_CHARS)
    transfer_id: uuid.UUID
    max_bytes: int = Field(ge=1, le=MAX_DOWNLOAD_BYTES)


class DownloadObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transfer_id: uuid.UUID
    final_url: str
    redirects: int
    length: int
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: str
    content_type: str


def source_digest(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _failed(code: str, **observation: Any) -> OperationResult:
    return OperationResult(status=OperationStatus.FAILED_BEFORE_EFFECT, observation=observation, error_code=code)


def _unknown(code: str) -> OperationResult:
    return OperationResult(status=OperationStatus.OUTCOME_UNKNOWN, observation={}, error_code=code)


async def download_to_quarantine(context: OperationContext, payload: DownloadInput) -> OperationResult:
    policy, guard, page, root = context.public_policy, context.network_guard, context.page, context.quarantine_root
    if policy is None or guard is None or not policy.configured or not root:
        return _failed("download_not_configured")
    try:
        requested = policy.check(payload.url)
    except UrlPolicyError as error:
        return _failed(error.code)
    try:
        directory = quarantine.begin(root, payload.transfer_id, source_digest=source_digest(requested.url))
    except FileExistsError:
        # This transfer id already made (or may have made) its one request. Never a second.
        return _failed("transfer_already_started")
    except OSError:
        return _failed("quarantine_unavailable")
    # From here the request may leave the machine: any doubt is OUTCOME_UNKNOWN, never FAILED.
    context.submitted = True
    capture = DownloadCapture(max_bytes=payload.max_bytes)
    guard.capture = capture
    guard.following_redirects = True
    url = requested.url
    redirects = 0
    try:
        for _ in range(MAX_REDIRECTS + 1):
            guard.pending_redirect = None
            guard.main_frame_block = None
            try:
                await page.goto(url, wait_until="commit")
            except PlaywrightError:
                if guard.main_frame_block is not None:
                    return _unknown(guard.main_frame_block)
                return _unknown("download_interrupted")
            target = guard.pending_redirect
            if target is None:
                break
            try:
                url = policy.check(target).url
            except UrlPolicyError:
                return _unknown("redirect_blocked")
            redirects += 1
            if redirects > MAX_REDIRECTS:
                return _unknown("too_many_redirects")
    finally:
        guard.following_redirects = False
        guard.capture = None
    if capture.body is None:
        return _unknown(guard.main_frame_block or "nothing_captured")
    kind = sniff(capture.body)
    if dangerous_kind(kind) or kind not in ALLOWED_KINDS:
        # The bytes are known; they are simply refused. Nothing reaches payload.bin. The GET did happen (a
        # read), but the DOWNLOAD effect this ledger tracks is "a payload was kept", and none was (S2 review
        # finding 3): the refusal is final, and the started marker records that a request was made.
        return _failed("download_type_refused", kind=kind if len(kind) <= 16 else "unknown")
    parts = urlsplit(url)
    try:
        manifest = quarantine.finish(
            directory, capture.body, kind=kind, content_type=capture.content_type, host_url=f"{parts.scheme}://{parts.netloc}/"
        )
    except OSError:
        return _unknown("quarantine_write_failed")
    observation = DownloadObservation(
        transfer_id=payload.transfer_id,
        final_url=url,
        redirects=redirects,
        length=manifest.length,
        sha256=manifest.sha256,
        kind=manifest.kind,
        content_type=manifest.content_type,
    )
    return OperationResult(status=OperationStatus.OK, observation=observation.model_dump(mode="json"))


OPERATIONS: tuple[BrowserOperation, ...] = (
    BrowserOperation(
        name=DOWNLOAD_TO_QUARANTINE,
        description=(
            "Fetch one approved public URL in a fresh cookie-less context, through the destination guard, "
            "into this worker's quarantine. GET only. Never opened, executed or placed here."
        ),
        input_model=DownloadInput,
        output_model=DownloadObservation,
        effect=Effect.DOWNLOAD,
        retry=RetryPolicy.RECONCILE_BEFORE_RETRY,
        reconciliation=Reconciliation.INSPECT_QUARANTINE,
        timeout_seconds=60.0,
        timeout_meaning=(
            "The request may have been made and the body may be partly written. The quarantine's own markers "
            "decide what happened; the URL is never fetched again for this transfer."
        ),
        preconditions=(
            "a transfer step authorization from a confirmed file_transfer grant was consumed by an attempt",
            "the URL passes this worker's own destination policy and resolves only to public addresses",
            "no quarantine directory exists yet for this transfer id",
        ),
        postconditions=(
            "started.json was written before any network request",
            "payload.bin, its Zone.Identifier stream and complete.json exist, and the length and SHA-256 match",
            "the bytes are a PDF, DOCX or text file by signature, never an executable, script, shortcut or macro document",
        ),
        handler=download_to_quarantine,
        target=OperationTarget.DOWNLOAD,
    ),
)
