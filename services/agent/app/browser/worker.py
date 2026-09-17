"""The isolated browser worker.

One process, one job: run a named operation from the closed registry against an
allowlisted origin, and report what it observed. What it deliberately does not
have is more interesting than what it does:

* **No database.** It cannot read a task, a proposal, an approval or another
  user's anything, because it holds no connection and is given no URL.
* **No approval surface.** There is no endpoint, parameter or code path by which
  the worker can create, grant, extend or consume an approval. It cannot even
  observe one.
* **No proposal mutation.** The approved values arrive inside a frozen model and
  are only ever compared. A mismatch is reported; it is never accommodated.
* **No generic automation.** There is no `evaluate`, no `javascript`, no
  `click(selector)`, no `goto(url)`. Operations are looked up by name in a
  registry that is a Python tuple, and origins come from this process's own
  allowlist -- never from the request.
* **No secrets.** It is configured with a worker token and a list of origins.
  It is never given a provider key, a database password, or the Electron `.env`.

Authentication is a bearer credential in a header, compared in constant time, on
every request including health. Loopback alone stopped being a sufficient trust
boundary the moment real side effects became possible: any process on the
machine can reach loopback, and a consequential booking is not something to hand
to whoever connects first.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, Request, Response, status
from fastapi.responses import JSONResponse
from playwright.async_api import Browser, Error as PlaywrightError, Playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from pydantic import ValidationError

from app.browser.config import WorkerSettings
from app.browser.network_guard import PublicNetworkGuard
from app.browser.protocol import (
    WORKER_TOKEN_HEADER,
    DispatchRequest,
    DispatchResponse,
    OperationStatus,
    WorkerErrorBody,
    WorkerIdentity,
)
from app.browser.registry import (
    BrowserOperation,
    Effect,
    OperationContext,
    OperationTarget,
    build_registry,
)
from app.browser.session import BrowserSession, WorkerGeneration, token_matches
from app.domain.page_observation import PUBLIC_WEB_SITE
from app.domain.public_url import PublicUrlPolicy

logger = logging.getLogger("lumi.browser.worker")


class DispatchLedger:
    """In-memory record of dispatches this worker process has handled.

    Deduplication is by `dispatch_id`, which the runtime derives from one row in
    `browser_dispatches` and therefore from one execution attempt. A repeat of a
    dispatch that is still running is refused outright; a repeat of one that
    finished replays the stored answer. Either way the browser is not driven
    twice, which is the property that keeps a duplicated dispatch from becoming a
    duplicated booking.

    It is in-memory on purpose. A worker restart takes this with it, and that is
    correct: a new worker has a new generation, so the runtime will not send it a
    dispatch belonging to the old one at all.
    """

    def __init__(self) -> None:
        self._in_flight: set[uuid.UUID] = set()
        self._completed: dict[uuid.UUID, DispatchResponse] = {}

    def claim(self, dispatch_id: uuid.UUID) -> DispatchResponse | None:
        """Take ownership, or explain why this dispatch will not run again."""
        if dispatch_id in self._completed:
            return self._completed[dispatch_id]
        if dispatch_id in self._in_flight:
            raise DuplicateDispatchError(dispatch_id)
        self._in_flight.add(dispatch_id)
        return None

    def complete(self, dispatch_id: uuid.UUID, response: DispatchResponse) -> None:
        self._in_flight.discard(dispatch_id)
        self._completed[dispatch_id] = response

    def release(self, dispatch_id: uuid.UUID) -> None:
        self._in_flight.discard(dispatch_id)


class DuplicateDispatchError(Exception):
    def __init__(self, dispatch_id: uuid.UUID) -> None:
        super().__init__(f"Dispatch {dispatch_id} is already running on this worker.")
        self.dispatch_id = dispatch_id


def _error(status_code: int, code: str, message: str, generation: uuid.UUID | None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=WorkerErrorBody(
            code=code, message=message, worker_generation=generation
        ).model_dump(mode="json"),
    )


def create_worker_app(settings: WorkerSettings | None = None) -> FastAPI:
    """Build the worker app. Invalid configuration fails before anything binds."""
    resolved = settings if settings is not None else WorkerSettings()
    registry = build_registry()
    public_policy = resolved.public_policy

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        generation = WorkerGeneration.mint()
        playwright: Playwright = await async_playwright().start()
        browser: Browser = await playwright.chromium.launch(headless=resolved.headless)
        app.state.generation = generation
        app.state.browser = browser
        app.state.ledger = DispatchLedger()
        logger.info(
            "browser worker ready",
            extra={
                "worker_generation": str(generation.id),
                "headless": resolved.headless,
                "sites": sorted(resolved.origins),
                "operations": registry.names(),
            },
        )
        try:
            yield
        finally:
            await browser.close()
            await playwright.stop()

    app = FastAPI(title="Lumi Browser Worker", version="0.3.0", lifespan=lifespan)
    app.state.settings = resolved
    app.state.registry = registry

    def _authenticate(token: str | None) -> JSONResponse | None:
        if token_matches(token, resolved.token):
            return None
        # Never echo what was presented, and never say which part was wrong.
        logger.warning("rejected an unauthenticated browser-worker request")
        return _error(
            status.HTTP_401_UNAUTHORIZED,
            "unauthenticated",
            "A valid worker credential is required.",
            None,
        )

    # The header name comes from the shared constant, not from the parameter
    # name FastAPI would otherwise derive: renaming one must not silently stop
    # the other being read, which would turn authentication into a no-op.
    credential = Header(default="", alias=WORKER_TOKEN_HEADER)

    @app.get("/health")
    async def health(request: Request, token: str = credential) -> Response:
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal
        generation: WorkerGeneration = request.app.state.generation
        return JSONResponse(
            content=WorkerIdentity(
                worker_generation=generation.id,
                started_at=generation.started_at.isoformat(),
                headless=resolved.headless,
                sites=_site_names(resolved, public_policy),
                operations=registry.names(),
            ).model_dump(mode="json")
        )

    @app.post("/v1/dispatch")
    async def dispatch(
        request: Request, body: DispatchRequest, token: str = credential
    ) -> Response:
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal

        generation: WorkerGeneration = request.app.state.generation
        ledger: DispatchLedger = request.app.state.ledger

        # A worker that restarted is not the worker the runtime addressed. It
        # refuses rather than performing consequential work the runtime believes
        # was already handed to a process that no longer exists.
        if body.expected_worker_generation != generation.id:
            return _error(
                status.HTTP_409_CONFLICT,
                "stale_worker_generation",
                "This dispatch was addressed to a different worker generation.",
                generation.id,
            )

        operation = registry.get(body.operation)
        if operation is None:
            return _error(
                status.HTTP_404_NOT_FOUND,
                "unknown_operation",
                "That operation is not in the reviewed registry.",
                generation.id,
            )

        if operation.target is OperationTarget.PUBLIC_PAGE:
            # No origin to resolve: the approved URL is checked by the
            # operation against this worker's own policy, and by the guard on
            # every request. A public-page operation never runs without a
            # persisted, approval-funded attempt.
            if body.site != PUBLIC_WEB_SITE or not public_policy.configured:
                return _error(
                    status.HTTP_403_FORBIDDEN,
                    "site_not_allowed",
                    "Public page inspection is not enabled on this worker.",
                    generation.id,
                )
            if body.action_id is None or body.attempt_id is None:
                return _error(
                    status.HTTP_400_BAD_REQUEST,
                    "attempt_required",
                    "A public-page operation requires a persisted execution attempt.",
                    generation.id,
                )
            origin = ""
        else:
            reviewed_origin = resolved.origins.get(body.site)
            if reviewed_origin is None:
                return _error(
                    status.HTTP_403_FORBIDDEN,
                    "site_not_allowed",
                    "That site is not in this worker's allowlist.",
                    generation.id,
                )
            origin = reviewed_origin

        try:
            payload = operation.parse_input(body.input)
        except ValidationError as error:
            return _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_operation_input",
                f"The input does not match {operation.name}: {error.error_count()} problem(s).",
                generation.id,
            )

        if operation.effect is not Effect.READ_ONLY and body.action_id is None:
            return _error(
                status.HTTP_400_BAD_REQUEST,
                "action_required",
                "Only read-only observation may run without a persisted action.",
                generation.id,
            )

        if operation.effect is Effect.CONSEQUENTIAL and body.attempt_id is None:
            # Consequential work is only ever done on behalf of a persisted
            # execution attempt. Without one there is nothing to reconcile
            # against later, and no record that the worker was authorised.
            return _error(
                status.HTTP_400_BAD_REQUEST,
                "attempt_required",
                "A consequential operation requires a persisted execution attempt.",
                generation.id,
            )

        try:
            replayed = ledger.claim(body.dispatch_id)
        except DuplicateDispatchError:
            return _error(
                status.HTTP_409_CONFLICT,
                "duplicate_dispatch",
                "That dispatch is already running on this worker.",
                generation.id,
            )
        if replayed is not None:
            logger.info(
                "replayed a duplicate dispatch without touching the browser",
                extra={"dispatch_id": str(body.dispatch_id), "operation": operation.name},
            )
            return JSONResponse(
                content=replayed.model_copy(update={"replayed": True}).model_dump(mode="json")
            )

        try:
            response = await _run(
                browser=request.app.state.browser,
                generation=generation,
                operation=operation,
                body=body,
                origin=origin,
                payload=payload,
                timeout_seconds=resolved.operation_timeout_seconds or operation.timeout_seconds,
                public_policy=public_policy,
            )
        except BaseException:
            ledger.release(body.dispatch_id)
            raise
        ledger.complete(body.dispatch_id, response)
        return JSONResponse(content=response.model_dump(mode="json"))

    return app


async def _run(
    *,
    browser: Browser,
    generation: WorkerGeneration,
    operation: BrowserOperation,
    body: DispatchRequest,
    origin: str,
    payload: Any,
    timeout_seconds: float,
    public_policy: PublicUrlPolicy | None = None,
) -> DispatchResponse:
    """Drive one operation in its own browser context, and classify the result.

    The classification rules, in full:

    * The operation returned -> its own status stands.
    * It raised, and had not yet issued a submission -> a known failure. Nothing
      reached the site.
    * It raised at or after the submission -> `OUTCOME_UNKNOWN`. Not `FAILED`:
      a timeout is a statement about how long we waited, not about whether a
      booking exists.
    """
    started = time.monotonic()
    session = BrowserSession(
        id=uuid.uuid4(),
        dispatch_id=body.dispatch_id,
        worker_generation=generation.id,
        origin=origin,
    )
    guard: PublicNetworkGuard | None = None
    if operation.target is OperationTarget.PUBLIC_PAGE and public_policy is not None:
        # A context that can only read: no service workers (which would sit
        # outside request routing), no downloads, no permissions, no stored
        # state. Every request is routed through the destination guard before
        # the page makes its first one.
        context = await browser.new_context(
            service_workers="block", accept_downloads=False, permissions=[]
        )
        page = await context.new_page()
        guard = PublicNetworkGuard(public_policy)
        await guard.install(context, page)
    else:
        context = await browser.new_context()
        page = await context.new_page()
    page.set_default_timeout(timeout_seconds * 1_000)
    page.set_default_navigation_timeout(timeout_seconds * 1_000)
    operation_context = OperationContext(
        page=page,
        origin=origin,
        dispatch_id=body.dispatch_id,
        observation_id=uuid.uuid4(),
        public_policy=public_policy if guard is not None else None,
        network_guard=guard,
    )

    status_value = OperationStatus.OUTCOME_UNKNOWN
    observation: dict[str, Any] = {}
    error_code: str | None = None
    try:
        try:
            result = await asyncio.wait_for(
                operation.handler(operation_context, payload), timeout=timeout_seconds
            )
            status_value, observation, error_code = (
                result.status,
                result.observation,
                result.error_code,
            )
        except (PlaywrightTimeoutError, TimeoutError, asyncio.TimeoutError):
            status_value, error_code = _classify_failure(operation_context, "timeout")
        except PlaywrightError as error:
            status_value, error_code = _classify_failure(
                operation_context, _browser_error_code(error)
            )
    finally:
        await context.close()

    duration_ms = int((time.monotonic() - started) * 1_000)
    logger.info(
        "browser operation finished",
        extra={
            "action_id": str(body.action_id) if body.action_id else None,
            "attempt_id": str(body.attempt_id) if body.attempt_id else None,
            "runtime_generation": str(body.runtime_generation),
            "worker_generation": str(generation.id),
            "browser_session_id": str(session.id),
            "dispatch_id": str(body.dispatch_id),
            "operation": operation.name,
            "effect": operation.effect.value,
            "observation_id": str(operation_context.observation_id),
            "duration_ms": duration_ms,
            "status": status_value.value,
            "submitted": operation_context.submitted,
            "error_code": error_code,
        },
    )
    return DispatchResponse(
        dispatch_id=body.dispatch_id,
        runtime_generation=body.runtime_generation,
        worker_generation=generation.id,
        operation=operation.name,
        status=status_value,
        observation=observation,
        error_code=error_code,
        duration_ms=duration_ms,
        submitted=operation_context.submitted,
    )


def _site_names(settings: WorkerSettings, policy: PublicUrlPolicy) -> list[str]:
    names = set(settings.origins)
    if policy.configured:
        names.add(PUBLIC_WEB_SITE)
    return sorted(names)


def _classify_failure(context: OperationContext, reason: str) -> tuple[OperationStatus, str]:
    if context.submitted:
        return OperationStatus.OUTCOME_UNKNOWN, f"{reason}_after_submission"
    return OperationStatus.FAILED_BEFORE_EFFECT, f"{reason}_before_submission"


def _browser_error_code(error: PlaywrightError) -> str:
    """A short, stable code. Never the browser's message: it can quote the page."""
    message = str(error)
    if "Target page, context or browser has been closed" in message:
        return "browser_closed"
    if "net::ERR_CONNECTION_REFUSED" in message:
        return "connection_refused"
    if "net::ERR_EMPTY_RESPONSE" in message or "net::ERR_CONNECTION_RESET" in message:
        return "empty_response"
    return "browser_error"
