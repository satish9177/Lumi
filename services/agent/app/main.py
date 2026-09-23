import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.routes import router
from app.api.document_routes import router as document_router
from app.api.transfer_routes import router as transfer_router
from app.api.project_routes import router as project_router
from app.api.security import RuntimeSecurityMiddleware
from app.config import AGENT_ROOT, Settings
from app.db.engine import create_database_engine, ping_database
from app.db.migrations import verify_schema_is_current
from app.browser.managed import ManagedBrowserWorker
from app.desktop.managed import ManagedDesktopWorker
from app.services.actions import ActionService
from app.services.authenticated_read import AuthenticatedReadService
from app.services.form_draft import FormDraftService
from app.services.form_prepare import FormPrepareService
from app.services.form_state import FormStateRegistry
from app.services.booking_preparation import BookingPreparationService
from app.services.booking_tasks import BookingTaskService
from app.services.clinic_info import ClinicInfoService
from app.services.page_inspection import PageInspectionService
from app.services.browser_profiles import BrowserProfileService
from app.services.desktop import DesktopService, desktop_exclusion_roots
from app.desktop.registry import AppRegistry
from app.services.desktop_actions import DesktopActionService
from app.services.desktop_disclosure import DesktopDisclosureService
from app.services.desktop_planning import DesktopPlanningService
from app.services.desktop_vision import DesktopVisionService
from app.services.documents import DocumentService
from app.services.transfers import TransferService
from app.services.projects import ProjectService, default_run_root
from app.files.quarantine import default_quarantine_root
from app.services.windows_job import runtime_job_is_active
from app.services.browser_execution import (
    BrowserExecutionService,
    BrowserWorkerConfig,
    WorkerSource,
)
from app.services.login_takeover import LoginTakeoverService
from app.services.parent_watchdog import ParentLiveness, parent_liveness, watch_liveness
from app.services.research_search import PublicSearchProvider, SearchConfig
from app.services.research_tasks import ResearchService
from app.services.recovery import FormDraftRecovery, RecoveryService
from app.services.runtime import register_runtime_generation, runtime_ownership
from app.services.tasks import TaskService

logger = logging.getLogger("lumi.runtime")


def _worker_source(settings: Settings) -> WorkerSource | None:
    """An externally started worker, a runtime-owned one, or none at all."""
    external = _worker_config(settings)
    if external is not None:
        return external
    if (
        settings.browser_site_origin is None
        and not settings.public_policy.configured
        and not settings.research_policy.configured
        and not settings.auth_test_origins
    ):
        return None
    return ManagedBrowserWorker(
        site_origin=settings.browser_site_origin,
        profile_root=settings.browser_profile_root,
        app_version=settings.app_version,
        headless=settings.browser_headless,
        timeout_seconds=settings.browser_worker_timeout_seconds,
        public_hosts=settings.public_inspection_hosts,
        inspection_test_origins=settings.inspection_test_origins,
        research_any_public_host=settings.research_any_public_host,
        research_hosts=settings.research_hosts,
        research_test_origins=settings.research_test_origins,
        research_max_tabs=settings.research_max_tabs,
        auth_test_origins=settings.auth_test_origins,
        quarantine_root=settings.download_quarantine_root or default_quarantine_root(),
    )


def _worker_config(settings: Settings) -> BrowserWorkerConfig | None:
    """A browser worker only exists when both halves of its credential do.

    A URL without a token would be an unauthenticated channel to a component
    that performs real, irreversible side effects, so the pair is required
    together or the capability is simply absent.
    """
    if settings.browser_worker_url is None or settings.browser_worker_token is None:
        return None
    return BrowserWorkerConfig(
        base_url=settings.browser_worker_url,
        token=settings.browser_worker_token,
        timeout_seconds=settings.browser_worker_timeout_seconds,
    )


async def _sweep_expired_takeovers(
    takeovers: LoginTakeoverService, *, interval_seconds: float
) -> None:
    """Milestone 8a S2's takeover watchdog: close every headed window whose
    hard timeout has passed. Runs for the life of the runtime process."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await takeovers.sweep_expired()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a watchdog must never take the runtime down.
            logger.exception("the login-takeover watchdog failed to sweep")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Run with `uvicorn --factory app.main:create_app`."""
    # Missing or invalid configuration fails here, before the server binds.
    resolved = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_database_engine(resolved)
        parent_watchdog: asyncio.Task[None] | None = None
        parent_context: AbstractContextManager[ParentLiveness] | None = None
        worker: WorkerSource | None = None
        warm_up: asyncio.Task[None] | None = None
        takeover_watchdog: asyncio.Task[None] | None = None
        desktop_worker: ManagedDesktopWorker | None = None
        try:
            await ping_database(engine)
            await verify_schema_is_current(engine)
            async with runtime_ownership(engine):
                generation = await register_runtime_generation(engine)
                # Acquire exclusive ownership before recovery. Otherwise a
                # second live runtime could mark the first one's work unknown.
                recovery_service = RecoveryService(engine)
                await recovery_service.recover_unfinished_attempts(generation.id)
                # An action a dead process left mid-reconciliation (crashed between
                # `begin_reconciliation` and `finish_reconciliation` committing) goes back to
                # OUTCOME_UNKNOWN so it can be reconciled again, instead of being stuck at
                # RECONCILING forever. Not generation-scoped: reconciliation never touches the
                # desktop, so it does not matter which process asked the question.
                await recovery_service.recover_interrupted_reconciliations()
                # Milestone 8b S6: a local form draft is browser state and the browser died
                # with the last runtime. Its row is closed, never restored or re-filled.
                await FormDraftRecovery(engine).discard_lost_drafts()
                app.state.engine = engine
                app.state.runtime_generation = generation
                task_service = TaskService(engine)
                app.state.task_service = task_service
                action_service = ActionService(
                    engine,
                    runtime_generation=generation.id,
                    approval_ttl_seconds=resolved.approval_ttl_seconds,
                )
                app.state.action_service = action_service
                worker = _worker_source(resolved)
                app.state.browser_execution_service = BrowserExecutionService(
                    engine,
                    actions=action_service,
                    runtime_generation=generation.id,
                    worker=worker,
                    public_policy=resolved.public_policy,
                )
                # Milestone 8b S6: which profiles are in form preparation or hold a local
                # draft. Empty on every start, because that state lives in the browser.
                form_state = FormStateRegistry()
                app.state.browser_profile_service = BrowserProfileService(
                    engine,
                    runtime_generation=generation.id,
                    browser=app.state.browser_execution_service,
                    paths=resolved.profile_paths,
                    lease_ttl_seconds=resolved.browser_profile_lease_ttl_seconds,
                    forms=form_state,
                )
                app.state.login_takeover_service = LoginTakeoverService(
                    engine,
                    runtime_generation=generation.id,
                    browser=app.state.browser_execution_service,
                    profiles=app.state.browser_profile_service,
                    ttl_seconds=resolved.login_attempt_ttl_seconds,
                )
                app.state.page_inspection_service = PageInspectionService(
                    engine,
                    tasks=task_service,
                    actions=action_service,
                    policy=resolved.public_policy,
                )
                app.state.booking_task_service = BookingTaskService(action_service)
                app.state.booking_preparation_service = BookingPreparationService(
                    tasks=task_service,
                    actions=action_service,
                    runtime_generation=generation.id,
                    worker=worker,
                )
                app.state.clinic_info_service = ClinicInfoService(
                    tasks=task_service,
                    actions=action_service,
                    runtime_generation=generation.id,
                    worker=worker,
                )
                research_service = ResearchService(
                    engine,
                    tasks=task_service,
                    actions=action_service,
                    runtime_generation=generation.id,
                    worker=worker,
                    policy=resolved.research_policy,
                    search=PublicSearchProvider(
                        SearchConfig(
                            endpoint=resolved.research_search_endpoint,
                            api_key=resolved.research_search_api_key,
                            header=resolved.research_search_header,
                            timeout_seconds=resolved.research_search_timeout_seconds,
                        ),
                        resolved.research_policy,
                    ),
                    grant_ttl_seconds=resolved.research_grant_ttl_seconds,
                    step_ttl_seconds=resolved.research_step_ttl_seconds,
                    max_tabs=resolved.research_max_tabs,
                )
                app.state.research_service = research_service
                # Milestone 8a S3. Reads through the *same* profile service S1
                # and S2 use: one lease, one directory, one open at a time.
                app.state.authenticated_read_service = AuthenticatedReadService(
                    engine,
                    tasks=task_service,
                    actions=action_service,
                    runtime_generation=generation.id,
                    worker=worker,
                    profiles=app.state.browser_profile_service,
                    grant_ttl_seconds=resolved.authenticated_grant_ttl_seconds,
                    step_ttl_seconds=resolved.authenticated_step_ttl_seconds,
                    max_tabs=resolved.authenticated_max_tabs,
                    forms=form_state,
                )
                # Milestone 8b S5. Needs no worker and never opens a browser: it
                # builds and approves an exact disclosure manifest. Since S6, approving
                # one is carried out by the draft service below.
                app.state.form_prepare_service = FormPrepareService(
                    engine,
                    actions=action_service,
                    grant_ttl_seconds=resolved.authenticated_grant_ttl_seconds,
                    forms=form_state,
                )
                # Milestone 8b S6: the network-frozen local form draft.
                app.state.form_draft_service = FormDraftService(
                    engine,
                    actions=action_service,
                    reads=app.state.authenticated_read_service,
                    profiles=app.state.browser_profile_service,
                    form=app.state.form_prepare_service,
                    runtime_generation=generation.id,
                    worker=worker,
                    state=form_state,
                )
                app.state.form_prepare_service.attach_drafts(app.state.form_draft_service)
                # Milestone 9 S1: Windows semantic observation. The isolated worker is created
                # here but never started until the first desktop request, and only when the
                # capability is explicitly enabled and the platform can do it.
                if resolved.desktop_observation and os.name == "nt":
                    desktop_worker = ManagedDesktopWorker(
                        root_pids=desktop_exclusion_roots(os.getpid(), resolved.runtime_parent_pid),
                        timeout_seconds=resolved.desktop_observation_timeout_seconds,
                        # Every process in the runtime's own kill-on-close job is Lumi's, whatever
                        # its parent chain looks like; only claimed when this runtime made that job.
                        trust_job=runtime_job_is_active(),
                        registered_apps=resolved.desktop_registered_apps,
                    )
                app.state.desktop_service = DesktopService(
                    engine,
                    runtime_generation=generation.id,
                    worker=desktop_worker,
                    timeout_seconds=resolved.desktop_observation_timeout_seconds,
                    unsupported=resolved.desktop_observation and os.name != "nt",
                )
                # Desktop text is retained for a bounded time even if nothing asks for it again.
                await app.state.desktop_service.sweep_expired()
                # Milestone 9 S2: exact desktop disclosure. It shares the S1 service for its two read-only
                # calls and adds no desktop capability of its own. A disclosure a dead runtime left
                # STARTED is OUTCOME_UNKNOWN: Lumi cannot know whether the provider received the private
                # snapshot, so it is never repeated automatically.
                app.state.desktop_disclosure_service = DesktopDisclosureService(
                    engine,
                    desktop=app.state.desktop_service,
                    grant_ttl_seconds=resolved.desktop_disclosure_ttl_seconds,
                )
                await app.state.desktop_disclosure_service.recover_started()
                # Milestone 9 S4: bounded desktop-action planning. Shares S1's two read-only calls, like
                # S2's disclosure service, and adds no desktop capability of its own. It never opens or
                # approves an execution action -- see `DesktopActionService.propose_from_plan` below.
                app.state.desktop_planning_service = DesktopPlanningService(
                    engine,
                    desktop=app.state.desktop_service,
                    grant_ttl_seconds=resolved.desktop_disclosure_ttl_seconds,
                )
                await app.state.desktop_planning_service.recover_started()
                # Milestone 9 S5: scoped visual fallback. Shares S1's two read-only calls and adds ONE
                # native capability of its own -- the worker's capture route -- gated by its own two
                # grant kinds. A capture or disclosure a dead runtime left STARTED is OUTCOME_UNKNOWN:
                # Lumi cannot know whether a screenshot was taken or an image reached a provider, so
                # neither is ever repeated automatically.
                app.state.desktop_vision_service = DesktopVisionService(
                    engine,
                    desktop=app.state.desktop_service,
                    grant_ttl_seconds=resolved.desktop_disclosure_ttl_seconds,
                )
                await app.state.desktop_vision_service.recover_started()
                # Milestone 9 S3/S4: trusted focus, semantic scroll, registered-app launch, and (S4) bounded
                # set-value/select/invoke mutations, on the same action ledger as browser effects. An
                # unfinished desktop dispatch a dead runtime left is closed as OUTCOME_UNKNOWN by
                # RecoveryService (already run above) and is never repeated; an unresolved S4 mutation
                # additionally blocks every new desktop action until reconciled.
                app.state.desktop_action_service = DesktopActionService(
                    engine,
                    actions=action_service,
                    tasks=TaskService(engine),
                    desktop=app.state.desktop_service,
                    registry=AppRegistry.from_config(resolved.desktop_registered_apps),
                )
                # Milestone 10 S1: approved documents. Reads only; a disclosure a dead runtime left
                # STARTED is OUTCOME_UNKNOWN and is never repeated; expired extracted text is purged.
                app.state.document_service = DocumentService(
                    engine,
                    grant_ttl_seconds=resolved.document_disclosure_ttl_seconds,
                    # Lumi-owned trees can never be (or contain) an approved root (S1 review finding 1).
                    forbidden_roots=tuple(
                        path
                        for path in (
                            str(AGENT_ROOT),
                            resolved.browser_profile_root,
                            resolved.download_quarantine_root or default_quarantine_root(),
                        )
                        if path
                    ),
                )
                await app.state.document_service.recover_started()
                await app.state.document_service.sweep_expired()
                # Milestone 10 S2: controlled downloads into a Lumi-owned quarantine, then an atomic,
                # no-overwrite placement. An attempt a dead runtime left is already OUTCOME_UNKNOWN
                # (RecoveryService above); it is reconciled from local evidence, never refetched.
                app.state.transfer_service = TransferService(
                    engine,
                    actions=action_service,
                    browser=app.state.browser_execution_service,
                    policy=resolved.public_policy,
                    quarantine_root=resolved.download_quarantine_root or default_quarantine_root(),
                    runtime_generation=generation.id,
                    grant_ttl_seconds=resolved.transfer_grant_ttl_seconds,
                )
                await app.state.transfer_service.sweep_quarantine()
                # Milestone 10 S3: registered project recipes. No run can survive a runtime restart (its job's
                # only handle died with the old runtime); recovery proves that from (pid, creation time).
                app.state.project_service = ProjectService(
                    engine,
                    actions=action_service,
                    runtime_generation=generation.id,
                    grant_ttl_seconds=resolved.project_run_ttl_seconds,
                    forbidden_roots=tuple(
                        path
                        for path in (
                            str(AGENT_ROOT),
                            resolved.browser_profile_root,
                            resolved.download_quarantine_root or default_quarantine_root(),
                            resolved.project_run_root or default_run_root(),
                        )
                        if path
                    ),
                    run_root=resolved.project_run_root or default_run_root(),
                )
                await app.state.project_service.recover()
                # A research session belongs to the process that created it.
                # Sessions a dead runtime left open describe browser contexts
                # that no longer exist, so every semantic ref they issued has
                # to stop resolving before anything plans again.
                await research_service.invalidate_stale_sessions()
                # A profile lease held by a generation that is gone describes a
                # browser nobody owns. Clearing them here is the database half
                # of stale-lease recovery; the exclusive OS handle on the
                # directory is the half that still refuses if something is
                # genuinely using it.
                await app.state.browser_profile_service.release_stale_leases()
                # Milestone 8a S2: any takeover left OPEN/UNCONFIRMED by a
                # generation that no longer exists is INTERRUPTED, never read
                # back as a successful sign-in. The profile itself is left
                # untouched -- it was never optimistically marked
                # AUTHENTICATED mid-takeover.
                await app.state.login_takeover_service.reconcile_interrupted()
                takeover_watchdog = asyncio.create_task(
                    _sweep_expired_takeovers(
                        app.state.login_takeover_service,
                        interval_seconds=resolved.login_attempt_sweep_interval_seconds,
                    )
                )
                if resolved.runtime_parent_pid is not None:
                    # Open the stable Windows process handle synchronously. If
                    # Electron is already gone or access fails, startup aborts
                    # before the runtime serves authenticated requests.
                    parent_context = parent_liveness(resolved.runtime_parent_pid)
                    is_parent_alive = parent_context.__enter__()
                    if not is_parent_alive():
                        raise RuntimeError("Electron parent is not running")
                    parent_watchdog = asyncio.create_task(
                        watch_liveness(is_parent_alive)
                    )
                if isinstance(worker, ManagedBrowserWorker):
                    # Chromium takes seconds to launch; start it without
                    # delaying readiness. First use awaits the same start.
                    warm_up = asyncio.create_task(worker.warm_up())
                yield
        finally:
            if getattr(app.state, "project_service", None) is not None:
                await app.state.project_service.shutdown()
            if takeover_watchdog is not None:
                takeover_watchdog.cancel()
                await asyncio.gather(takeover_watchdog, return_exceptions=True)
            if warm_up is not None:
                warm_up.cancel()
                await asyncio.gather(warm_up, return_exceptions=True)
            if isinstance(worker, ManagedBrowserWorker):
                await worker.aclose()
            if desktop_worker is not None:
                await desktop_worker.aclose()
            if parent_watchdog is not None:
                parent_watchdog.cancel()
                await asyncio.gather(parent_watchdog, return_exceptions=True)
            if parent_context is not None:
                parent_context.__exit__(None, None, None)
            await engine.dispose()

    app = FastAPI(title="Lumi Agent Runtime", version="0.3.0", lifespan=lifespan)
    app.state.request_shutdown = lambda: None
    app.add_middleware(RuntimeSecurityMiddleware, token=resolved.runtime_token)
    register_error_handlers(app)
    app.include_router(router)
    app.include_router(document_router)
    app.include_router(transfer_router)
    app.include_router(project_router)
    return app
