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

from app.browser.authenticated_session import AuthenticatedReadSession
from app.browser.config import WorkerSettings
from app.browser.egress_broker import EgressBroker, managed_launch_options
from app.browser.local_form_draft import (
    DraftError,
    FormFreezeController,
    discard_draft,
    handover_draft,
)
from app.browser.network_guard import PublicNetworkGuard
from app.browser.profile_session import (
    ProfileSessionError,
    ProfileSessionStore,
    worker_versions,
)
from app.browser.protocol import (
    WORKER_TOKEN_HEADER,
    DispatchRequest,
    DispatchResponse,
    FormDiscardRequest,
    FormDiscardResponse,
    FormFreezeRequest,
    FormFreezeResponse,
    FormHandoverRequest,
    FormHandoverResponse,
    OperationStatus,
    PrepareCaptureRequest,
    PrepareCaptureResponse,
    PrepareRestoreRequest,
    PrepareRestoreResponse,
    ProfileSessionRequest,
    ProfileSessionResponse,
    SessionRequest,
    SessionResponse,
    TakeoverConfirmRequest,
    TakeoverConfirmResponse,
    TakeoverStartRequest,
    TakeoverStartResponse,
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
from app.browser.research_session import ResearchBrowserSession, SessionError, SessionStore
from app.browser.session import BrowserSession, WorkerGeneration, token_matches
from app.domain.authenticated import AUTHENTICATED_READ_SITE
from app.domain.browser_profile import BrowserVersions
from app.domain.page_observation import PUBLIC_WEB_SITE
from app.domain.public_url import PublicUrlPolicy, parse_test_origins
from app.domain.research import PUBLIC_RESEARCH_SITE

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
    research_policy = resolved.research_policy

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        generation = WorkerGeneration.mint()
        # The broker binds before Chromium launches, because Chromium is
        # launched pointing at it. Its credential and port are minted here and
        # die with this process, so a credential a replaced worker handed out
        # cannot be presented to its successor.
        broker = EgressBroker(
            resolved.broker_policy, configured_origins=resolved.broker_origins
        )
        await broker.start()
        playwright: Playwright = await async_playwright().start()
        browser: Browser = await playwright.chromium.launch(
            **managed_launch_options(broker, headless=resolved.headless)  # type: ignore[arg-type]
        )
        app.state.generation = generation
        app.state.broker = broker
        app.state.playwright = playwright
        app.state.browser = browser
        app.state.ledger = DispatchLedger()
        # One active research task at a time, as this milestone intends.
        app.state.sessions = SessionStore(limit=1)
        # Milestone 8a S1: one persistent, Lumi-managed profile at a time. It
        # is a *separate* store from `sessions` on purpose -- a research session
        # and an authenticated profile are different kinds of context and one
        # must never be substituted for the other.
        app.state.profiles = ProfileSessionStore(limit=1)
        # Milestone 8b S6: the worker-global two-layer network freeze and its owner.
        app.state.freeze = FormFreezeController(broker, generation.id)
        logger.info(
            "browser worker ready",
            extra={
                "worker_generation": str(generation.id),
                "broker_generation": str(broker.generation),
                "headless": resolved.headless,
                "sites": sorted(resolved.origins),
                "operations": registry.names(),
                "research_enabled": research_policy.configured,
                "chromium_build": browser.version,
            },
        )
        try:
            yield
        finally:
            profiles: ProfileSessionStore = app.state.profiles
            # Persistent contexts close first: each owns its own Chromium
            # process and an exclusive handle on a directory, and both have to
            # be let go before the worker exits or the next start is refused.
            await profiles.close_all()
            sessions: SessionStore = app.state.sessions
            await sessions.close_all()
            await browser.close()
            await playwright.stop()
            # After the broker closes there is no egress path at all, which is
            # the intended failure mode: never a fall back to direct networking.
            await broker.aclose()

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

    def _freeze_refusal(
        request: Request, *, profile_id: uuid.UUID | None, dispatch_id: uuid.UUID | None = None
    ) -> JSONResponse | None:
        """While a freeze has an owner, nobody else may use the worker's browsers.

        `form_is_dirty` for the owner's own profile (nothing may move a dirty page),
        `freeze_owned` for everyone else. One task can therefore never open, read,
        close or thaw another task's draft.
        """
        freeze: FormFreezeController = request.app.state.freeze
        code = freeze.refusal_for(profile_id=profile_id, dispatch_id=dispatch_id)
        if code is None:
            return None
        generation: WorkerGeneration = request.app.state.generation
        return _error(
            status.HTTP_409_CONFLICT,
            code,
            "A form draft holds this worker's browser; discard it or take it over first.",
            generation.id,
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
                sites=_site_names(resolved, public_policy, research_policy),
                operations=registry.names(),
            ).model_dump(mode="json")
        )

    @app.post("/v1/sessions/open")
    async def open_session(
        request: Request, body: SessionRequest, token: str = credential
    ) -> Response:
        """Create the task-owned public context, or return the existing one."""
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal
        generation: WorkerGeneration = request.app.state.generation
        if body.expected_worker_generation != generation.id:
            return _error(
                status.HTTP_409_CONFLICT,
                "stale_worker_generation",
                "This session was addressed to a different worker generation.",
                generation.id,
            )
        if not research_policy.configured:
            return _error(
                status.HTTP_403_FORBIDDEN,
                "site_not_allowed",
                "Public research is not enabled on this worker.",
                generation.id,
            )
        held = _freeze_refusal(request, profile_id=None)
        if held is not None:
            return held
        sessions: SessionStore = request.app.state.sessions
        try:
            session = await sessions.open(
                browser=request.app.state.browser,
                session_id=body.session_id,
                policy=research_policy,
                max_tabs=resolved.research_max_tabs,
            )
        except SessionError as error:
            return _error(
                status.HTTP_409_CONFLICT,
                error.code,
                "That research session could not be opened.",
                generation.id,
            )
        return JSONResponse(
            content=SessionResponse(
                session_id=session.id,
                worker_generation=generation.id,
                status="OPEN",
                open_tabs=session.open_tabs,
            ).model_dump(mode="json")
        )

    @app.post("/v1/sessions/close")
    async def close_session(
        request: Request, body: SessionRequest, token: str = credential
    ) -> Response:
        """Dispose of the context and every ref table it issued."""
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal
        generation: WorkerGeneration = request.app.state.generation
        sessions: SessionStore = request.app.state.sessions
        # A session belonging to a previous generation is already gone with the
        # process that held it, so closing it is a no-op, not a conflict.
        closed = (
            await sessions.close(body.session_id)
            if body.expected_worker_generation == generation.id
            else False
        )
        return JSONResponse(
            content=SessionResponse(
                session_id=body.session_id,
                worker_generation=generation.id,
                status="CLOSED" if closed else "NOT_FOUND",
            ).model_dump(mode="json")
        )

    @app.post("/v1/profiles/open")
    async def open_profile(
        request: Request, body: ProfileSessionRequest, token: str = credential
    ) -> Response:
        """Open the persistent context for one Lumi-managed profile.

        The request carries an id and a recorded Chromium build. The directory
        it resolves to is this worker's business alone, and the answer never
        names it.
        """
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal
        generation: WorkerGeneration = request.app.state.generation
        if body.expected_worker_generation != generation.id:
            return _error(
                status.HTTP_409_CONFLICT,
                "stale_worker_generation",
                "This profile was addressed to a different worker generation.",
                generation.id,
            )
        held = _freeze_refusal(request, profile_id=body.profile_id)
        if held is not None and not request.app.state.profiles.is_open(body.profile_id):
            return held
        browser: Browser = request.app.state.browser
        current = worker_versions(
            browser,
            playwright_version=_playwright_version(),
            app_version=resolved.app_version,
        )
        recorded = (
            BrowserVersions(
                chromium_build=body.recorded_chromium_build,
                playwright_version="",
                app_version="",
            )
            if body.recorded_chromium_build
            else None
        )
        profiles: ProfileSessionStore = request.app.state.profiles
        try:
            session = await profiles.open(
                playwright=request.app.state.playwright,
                broker=request.app.state.broker,
                paths=resolved.profile_paths,
                profile_id=body.profile_id,
                recorded=recorded,
                current=current,
                # Milestone 8a S2: a takeover explicitly asks for a visible
                # window. Every other caller keeps this worker's own
                # configured default.
                headless=False if body.headed else resolved.headless,
                timeout_seconds=resolved.operation_timeout_seconds or 30.0,
            )
        except ProfileSessionError as error:
            # A downgrade refusal and a contended lock are both 409: the
            # profile exists and is fine, this worker may simply not open it.
            return _error(
                status.HTTP_409_CONFLICT,
                error.code,
                "That browser profile could not be opened.",
                generation.id,
            )
        except (PlaywrightError, OSError):
            logger.warning(
                "a persistent profile failed to open",
                extra={"profile_id": str(body.profile_id)},
            )
            return _error(
                status.HTTP_409_CONFLICT,
                "profile_open_failed",
                "That browser profile could not be opened.",
                generation.id,
            )
        return JSONResponse(
            content=ProfileSessionResponse(
                profile_id=session.profile_id,
                worker_generation=generation.id,
                status="OPEN",
                chromium_build=session.versions.chromium_build,
                playwright_version=session.versions.playwright_version,
                lock_held=session.lock.held,
            ).model_dump(mode="json")
        )

    @app.post("/v1/profiles/close")
    async def close_profile(
        request: Request, body: ProfileSessionRequest, token: str = credential
    ) -> Response:
        """Close the persistent context and release its exclusive handle.

        Closing a profile belonging to a previous worker generation is a no-op
        rather than a conflict: that context died with the process that held
        it, and so did its lock.
        """
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal
        generation: WorkerGeneration = request.app.state.generation
        profiles: ProfileSessionStore = request.app.state.profiles
        try:
            closed = (
                await profiles.close(body.profile_id)
                if body.expected_worker_generation == generation.id
                else False
            )
        except ProfileSessionError as error:
            # A page holding a local draft is never closed as read cleanup.
            return _error(
                status.HTTP_409_CONFLICT,
                error.code,
                "That browser profile holds a form draft.",
                generation.id,
            )
        return JSONResponse(
            content=ProfileSessionResponse(
                profile_id=body.profile_id,
                worker_generation=generation.id,
                status="CLOSED" if closed else "NOT_FOUND",
            ).model_dump(mode="json")
        )

    @app.post("/v1/profiles/takeover/start")
    async def start_takeover(
        request: Request, body: TakeoverStartRequest, token: str = credential
    ) -> Response:
        """Navigate the profile's one tab to its own site and hand it to the
        human. No planner call, no observation, no provider call: this route
        only opens a tab and installs the wide-but-scheme-checked TAKEOVER
        network mode."""
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal
        generation: WorkerGeneration = request.app.state.generation
        if body.expected_worker_generation != generation.id:
            return _error(
                status.HTTP_409_CONFLICT,
                "stale_worker_generation",
                "This takeover was addressed to a different worker generation.",
                generation.id,
            )
        held = _freeze_refusal(request, profile_id=body.profile_id)
        if held is not None:
            return held
        profiles: ProfileSessionStore = request.app.state.profiles
        try:
            outcome = await profiles.start_takeover(
                body.profile_id,
                site=body.site,
                timeout_seconds=resolved.operation_timeout_seconds or 30.0,
            )
        except ProfileSessionError as error:
            return _error(
                status.HTTP_409_CONFLICT,
                error.code,
                "That takeover could not be started.",
                generation.id,
            )
        return JSONResponse(
            content=TakeoverStartResponse(
                profile_id=body.profile_id, worker_generation=generation.id, status=outcome
            ).model_dump(mode="json")
        )

    @app.post("/v1/profiles/takeover/confirm")
    async def confirm_takeover(
        request: Request, body: TakeoverConfirmRequest, token: str = credential
    ) -> Response:
        """The one deterministic check run when a takeover ends. No planner
        call, no observation, no provider call: only bounded DOM counts and a
        closed site-scope enum leave this route."""
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal
        generation: WorkerGeneration = request.app.state.generation
        if body.expected_worker_generation != generation.id:
            return _error(
                status.HTTP_409_CONFLICT,
                "stale_worker_generation",
                "This takeover was addressed to a different worker generation.",
                generation.id,
            )
        profiles: ProfileSessionStore = request.app.state.profiles
        result = await profiles.confirm_takeover(body.profile_id, site=body.site)
        if result is None:
            return JSONResponse(
                content=TakeoverConfirmResponse(
                    profile_id=body.profile_id,
                    worker_generation=generation.id,
                    status="PROFILE_NOT_OPEN",
                ).model_dump(mode="json")
            )
        return JSONResponse(
            content=TakeoverConfirmResponse(
                profile_id=body.profile_id,
                worker_generation=generation.id,
                status="CHECKED",
                scope=result.scope,
                credential_surface=result.credential_surface,
                signals=list(result.signals),
                account_fingerprint=result.account_fingerprint,
            ).model_dump(mode="json")
        )

    # ---- Milestone 8b S6: form preparation, freeze, discard, handover -------------
    #
    # Five narrow routes and nothing generic. None takes or returns a URL, a
    # selector, a value or a label; each is bound to the dispatch that owns the
    # freeze, so one task cannot thaw another task's draft.

    def _stale_generation(
        request: Request, expected: uuid.UUID, what: str
    ) -> JSONResponse | None:
        generation: WorkerGeneration = request.app.state.generation
        if expected != generation.id:
            return _error(
                status.HTTP_409_CONFLICT,
                "stale_worker_generation",
                f"This {what} was addressed to a different worker generation.",
                generation.id,
            )
        return None

    @app.post("/v1/profiles/prepare-capture")
    async def prepare_capture(
        request: Request, body: PrepareCaptureRequest, token: str = credential
    ) -> Response:
        """Remember, in worker memory, the in-site page being read. Returns no address."""
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal
        stale = _stale_generation(request, body.expected_worker_generation, "preparation")
        if stale is not None:
            return stale
        generation: WorkerGeneration = request.app.state.generation
        held = _freeze_refusal(request, profile_id=body.profile_id)
        if held is not None:
            return held
        profiles: ProfileSessionStore = request.app.state.profiles
        try:
            outcome = await profiles.capture_preparation_destination(body.profile_id, site=body.site)
            error_code = None
        except ProfileSessionError as error:
            outcome, error_code = "REFUSED", error.code
        return JSONResponse(
            content=PrepareCaptureResponse(
                profile_id=body.profile_id,
                worker_generation=generation.id,
                status=outcome,
                error_code=error_code,
            ).model_dump(mode="json")
        )

    @app.post("/v1/profiles/prepare-restore")
    async def prepare_restore(
        request: Request, body: PrepareRestoreRequest, token: str = credential
    ) -> Response:
        """Navigate the headed profile back to the captured page, worker-internally."""
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal
        stale = _stale_generation(request, body.expected_worker_generation, "preparation")
        if stale is not None:
            return stale
        generation: WorkerGeneration = request.app.state.generation
        held = _freeze_refusal(request, profile_id=body.profile_id)
        if held is not None:
            return held
        profiles: ProfileSessionStore = request.app.state.profiles
        try:
            outcome = await profiles.restore_preparation(
                body.profile_id,
                site=body.site,
                test_origins=parse_test_origins(resolved.auth_test_origins),
                timeout_seconds=resolved.operation_timeout_seconds or 30.0,
            )
            error_code = None
        except ProfileSessionError as error:
            outcome, error_code = "REFUSED", error.code
        return JSONResponse(
            content=PrepareRestoreResponse(
                profile_id=body.profile_id,
                worker_generation=generation.id,
                status=outcome,
                error_code=error_code,
            ).model_dump(mode="json")
        )

    @app.post("/v1/profiles/form-freeze")
    async def form_freeze_route(
        request: Request, body: FormFreezeRequest, token: str = credential
    ) -> Response:
        """Enter the two-layer network freeze for the dispatch that will write."""
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal
        stale = _stale_generation(request, body.expected_worker_generation, "freeze")
        if stale is not None:
            return stale
        generation: WorkerGeneration = request.app.state.generation
        profiles: ProfileSessionStore = request.app.state.profiles
        freeze: FormFreezeController = request.app.state.freeze

        def refused(code: str) -> Response:
            return JSONResponse(
                content=FormFreezeResponse(
                    profile_id=body.profile_id,
                    dispatch_id=body.dispatch_id,
                    worker_generation=generation.id,
                    status="REFUSED",
                    error_code=code,
                ).model_dump(mode="json")
            )

        code = freeze.refusal_for(profile_id=body.profile_id, dispatch_id=None)
        if code is not None:
            return refused(code)
        if not profiles.is_open(body.profile_id):
            return refused("preparation_mode_required")
        profile_session = profiles.get(body.profile_id)
        read = profile_session.read_session
        if profile_session.headless or read is None or not read.preparation_mode:
            return refused("preparation_mode_required")
        try:
            proof = await freeze.enter(
                read,
                dispatch_id=body.dispatch_id,
                settle_seconds=resolved.freeze_settle_seconds,
                drain_seconds=resolved.freeze_drain_seconds,
            )
        except DraftError as error:
            return refused(error.code)
        logger.info(
            "network freeze entered",
            extra={
                "dispatch_id": str(body.dispatch_id),
                "profile_id": str(body.profile_id),
                "worker_generation": str(generation.id),
                "freeze_duration_ms": proof.freeze_duration_ms,
                "resolution_count": proof.resolution_count,
                "dial_count": proof.dial_count,
            },
        )
        return JSONResponse(
            content=FormFreezeResponse(
                profile_id=body.profile_id,
                dispatch_id=body.dispatch_id,
                worker_generation=generation.id,
                status="FROZEN",
                guard_in_flight=proof.guard_in_flight,
                broker_active_connections=proof.broker_active_connections,
                resolution_count=proof.resolution_count,
                dial_count=proof.dial_count,
                freeze_duration_ms=proof.freeze_duration_ms,
            ).model_dump(mode="json")
        )

    @app.post("/v1/profiles/form-discard")
    async def form_discard_route(
        request: Request, body: FormDiscardRequest, token: str = credential
    ) -> Response:
        """Destroy the dirty page WHILE FROZEN, verify it is gone, then thaw."""
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal
        stale = _stale_generation(request, body.expected_worker_generation, "discard")
        if stale is not None:
            return stale
        generation: WorkerGeneration = request.app.state.generation
        profiles: ProfileSessionStore = request.app.state.profiles
        freeze: FormFreezeController = request.app.state.freeze

        def answer(state: str, code: str | None = None) -> Response:
            return JSONResponse(
                content=FormDiscardResponse(
                    profile_id=body.profile_id,
                    worker_generation=generation.id,
                    status=state,
                    error_code=code,
                ).model_dump(mode="json")
            )

        if not profiles.is_open(body.profile_id):
            return answer("PROFILE_NOT_OPEN")
        read = profiles.get(body.profile_id).read_session
        owner = freeze.owner
        if read is None or owner is None or owner.profile_id != body.profile_id:
            # Nothing holds the freeze for this profile: no dirty page to destroy.
            return answer("NOTHING_TO_DISCARD")
        if owner.dispatch_id != body.dispatch_id:
            return answer("REFUSED", "freeze_owned")
        try:
            await discard_draft(read, freeze, body.dispatch_id)
        except DraftError as error:
            return answer("REFUSED", error.code)
        return answer("DISCARDED")

    @app.post("/v1/profiles/form-handover")
    async def form_handover_route(
        request: Request, body: FormHandoverRequest, token: str = credential
    ) -> Response:
        """Re-verify the live draft, then restore the network for the human."""
        refusal = _authenticate(token)
        if refusal is not None:
            return refusal
        stale = _stale_generation(request, body.expected_worker_generation, "handover")
        if stale is not None:
            return stale
        generation: WorkerGeneration = request.app.state.generation
        profiles: ProfileSessionStore = request.app.state.profiles
        freeze: FormFreezeController = request.app.state.freeze

        def answer(state: str, code: str | None = None, verified: int = 0) -> Response:
            return JSONResponse(
                content=FormHandoverResponse(
                    profile_id=body.profile_id,
                    worker_generation=generation.id,
                    status=state,
                    error_code=code,
                    verified_count=verified,
                ).model_dump(mode="json")
            )

        if not profiles.is_open(body.profile_id):
            return answer("PROFILE_NOT_OPEN")
        profile_session = profiles.get(body.profile_id)
        read = profile_session.read_session
        if read is None or freeze.owner is None:
            return answer("REFUSED", "draft_changed")
        expected = {item.element_ref: item.verified_local_value_hash for item in body.fields}
        try:
            checked = await handover_draft(
                profile_session, read, freeze, body.dispatch_id, expected
            )
        except DraftError as error:
            # Refused: the network is still frozen. Nothing was restored.
            return answer("REFUSED", error.code)
        return answer("HANDED_OVER", verified=checked)

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

        held = _freeze_refusal(request, profile_id=body.profile_id, dispatch_id=body.dispatch_id)
        if held is not None:
            return held

        operation = registry.get(body.operation)
        if operation is None:
            return _error(
                status.HTTP_404_NOT_FOUND,
                "unknown_operation",
                "That operation is not in the reviewed registry.",
                generation.id,
            )

        # Milestone 8a S3: a research session and an authenticated profile are
        # different kinds of context, named by different fields. A dispatch may
        # carry the one its operation targets and nothing else, so neither id
        # can be passed where the other is expected.
        if operation.target is not OperationTarget.AUTHENTICATED_SESSION and body.profile_id is not None:
            return _error(
                status.HTTP_409_CONFLICT,
                "session_kind_mismatch",
                "A profile id was supplied to an operation that does not run in a profile.",
                generation.id,
            )
        if operation.target is OperationTarget.AUTHENTICATED_SESSION and body.session_id is not None:
            return _error(
                status.HTTP_409_CONFLICT,
                "session_kind_mismatch",
                "A research session id was supplied to an authenticated operation.",
                generation.id,
            )

        research_session: ResearchBrowserSession | None = None
        authenticated_session: AuthenticatedReadSession | None = None
        if operation.target is OperationTarget.AUTHENTICATED_SESSION:
            # No origin and no profile creation here: the profile must already
            # be open in this worker generation, headless and free of any
            # takeover, and its site is pinned by the first step that names it.
            if body.site != AUTHENTICATED_READ_SITE:
                return _error(
                    status.HTTP_403_FORBIDDEN,
                    "site_not_allowed",
                    "Authenticated reading is not addressed to this worker.",
                    generation.id,
                )
            if body.action_id is None or body.attempt_id is None:
                return _error(
                    status.HTTP_400_BAD_REQUEST,
                    "attempt_required",
                    "An authenticated operation requires a persisted execution attempt.",
                    generation.id,
                )
            if body.profile_id is None:
                return _error(
                    status.HTTP_400_BAD_REQUEST,
                    "profile_required",
                    "An authenticated operation requires a profile.",
                    generation.id,
                )
            site = body.input.get("site")
            if not isinstance(site, str):
                return _error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "invalid_operation_input",
                    "An authenticated operation names its profile's site.",
                    generation.id,
                )
            profiles: ProfileSessionStore = request.app.state.profiles
            if (
                operation.effect is Effect.LOCAL_DRAFT
                and profiles.is_open(body.profile_id)
                and profiles.get(body.profile_id).headless
            ):
                # A local draft is only ever made in a window a person can see.
                return _error(
                    status.HTTP_409_CONFLICT,
                    "preparation_mode_required",
                    "That profile is not open in a preparation window.",
                    generation.id,
                )
            try:
                authenticated_session = await profiles.read_session(
                    body.profile_id,
                    site=site,
                    test_origins=parse_test_origins(resolved.auth_test_origins),
                )
            except ProfileSessionError as error:
                return _error(
                    status.HTTP_409_CONFLICT,
                    error.code,
                    "That profile is not available for reading.",
                    generation.id,
                )
            origin = ""
        elif operation.target is OperationTarget.RESEARCH_SESSION:
            # No origin and no session creation here: the session must already
            # exist in this worker generation, and the destination comes from a
            # ref this worker issued or an address the user typed.
            if body.site != PUBLIC_RESEARCH_SITE or not research_policy.configured:
                return _error(
                    status.HTTP_403_FORBIDDEN,
                    "site_not_allowed",
                    "Public research is not enabled on this worker.",
                    generation.id,
                )
            if body.action_id is None or body.attempt_id is None:
                return _error(
                    status.HTTP_400_BAD_REQUEST,
                    "attempt_required",
                    "A research operation requires a persisted execution attempt.",
                    generation.id,
                )
            if body.session_id is None:
                return _error(
                    status.HTTP_400_BAD_REQUEST,
                    "session_required",
                    "A research operation requires a task-owned session.",
                    generation.id,
                )
            sessions: SessionStore = request.app.state.sessions
            try:
                research_session = sessions.get(body.session_id)
            except SessionError as error:
                return _error(
                    status.HTTP_409_CONFLICT,
                    error.code,
                    "That research session does not exist in this worker.",
                    generation.id,
                )
            origin = ""
        elif operation.target in (OperationTarget.PUBLIC_PAGE, OperationTarget.DOWNLOAD):
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
                research_session=research_session,
                authenticated_session=authenticated_session,
                form_freeze=request.app.state.freeze,
                quarantine_root=resolved.quarantine_directory,
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
    research_session: ResearchBrowserSession | None = None,
    authenticated_session: AuthenticatedReadSession | None = None,
    form_freeze: FormFreezeController | None = None,
    quarantine_root: str | None = None,
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
    #: A research step runs in the task's own long-lived context, so this
    #: dispatch must neither create one nor close one when it finishes.
    owns_context = operation.target not in (
        OperationTarget.RESEARCH_SESSION,
        OperationTarget.AUTHENTICATED_SESSION,
    )
    if operation.target is OperationTarget.AUTHENTICATED_SESSION and authenticated_session is not None:
        # The profile's own persistent context. A dispatch neither creates nor
        # closes it: it belongs to the profile's owner, and closing it would
        # end the user's session state along with the step.
        context = authenticated_session.context
        active = authenticated_session.active
        page = (
            authenticated_session.tabs[active].page
            if active is not None and active in authenticated_session.tabs
            else await context.new_page()
        )
    elif operation.target is OperationTarget.RESEARCH_SESSION and research_session is not None:
        context = research_session.context
        active = research_session.active
        page = (
            research_session.tabs[active].page
            if active is not None and active in research_session.tabs
            else await context.new_page()
        )
    elif operation.target in (OperationTarget.PUBLIC_PAGE, OperationTarget.DOWNLOAD) and public_policy is not None:
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
        research_session=research_session,
        authenticated_session=authenticated_session,
        form_freeze=form_freeze,
        quarantine_root=quarantine_root if operation.target is OperationTarget.DOWNLOAD else None,
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
        if owns_context:
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
            "research_session_id": str(research_session.id) if research_session else None,
            "profile_id": str(authenticated_session.profile_id) if authenticated_session else None,
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


def _playwright_version() -> str:
    """The installed Playwright version, recorded on a profile row.

    Looked up rather than hard-coded, so an upgrade that changes the Chromium a
    profile was written by is visible in the profile's own metadata.
    """
    try:
        import importlib.metadata

        return importlib.metadata.version("playwright")
    except Exception:  # pragma: no cover - metadata is present in every build.
        return "unknown"


def _site_names(
    settings: WorkerSettings, policy: PublicUrlPolicy, research: PublicUrlPolicy
) -> list[str]:
    names = set(settings.origins)
    if policy.configured:
        names.add(PUBLIC_WEB_SITE)
    if research.configured:
        names.add(PUBLIC_RESEARCH_SITE)
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
