import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.routes import router
from app.api.security import RuntimeSecurityMiddleware
from app.config import Settings
from app.db.engine import create_database_engine, ping_database
from app.db.migrations import verify_schema_is_current
from app.browser.managed import ManagedBrowserWorker
from app.services.actions import ActionService
from app.services.authenticated_read import AuthenticatedReadService
from app.services.form_prepare import FormPrepareService
from app.services.booking_preparation import BookingPreparationService
from app.services.booking_tasks import BookingTaskService
from app.services.clinic_info import ClinicInfoService
from app.services.page_inspection import PageInspectionService
from app.services.browser_profiles import BrowserProfileService
from app.services.browser_execution import (
    BrowserExecutionService,
    BrowserWorkerConfig,
    WorkerSource,
)
from app.services.login_takeover import LoginTakeoverService
from app.services.parent_watchdog import ParentLiveness, parent_liveness, watch_liveness
from app.services.research_search import PublicSearchProvider, SearchConfig
from app.services.research_tasks import ResearchService
from app.services.recovery import RecoveryService
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
        try:
            await ping_database(engine)
            await verify_schema_is_current(engine)
            async with runtime_ownership(engine):
                generation = await register_runtime_generation(engine)
                # Acquire exclusive ownership before recovery. Otherwise a
                # second live runtime could mark the first one's work unknown.
                await RecoveryService(engine).recover_unfinished_attempts(generation.id)
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
                app.state.browser_profile_service = BrowserProfileService(
                    engine,
                    runtime_generation=generation.id,
                    browser=app.state.browser_execution_service,
                    paths=resolved.profile_paths,
                    lease_ttl_seconds=resolved.browser_profile_lease_ttl_seconds,
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
                )
                # Milestone 8b S5. Needs no worker and never opens a browser: it
                # builds and approves an exact disclosure manifest and stops.
                app.state.form_prepare_service = FormPrepareService(
                    engine,
                    actions=action_service,
                    grant_ttl_seconds=resolved.authenticated_grant_ttl_seconds,
                )
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
            if takeover_watchdog is not None:
                takeover_watchdog.cancel()
                await asyncio.gather(takeover_watchdog, return_exceptions=True)
            if warm_up is not None:
                warm_up.cancel()
                await asyncio.gather(warm_up, return_exceptions=True)
            if isinstance(worker, ManagedBrowserWorker):
                await worker.aclose()
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
    return app
