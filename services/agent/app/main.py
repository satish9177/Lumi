import asyncio
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
from app.services.booking_preparation import BookingPreparationService
from app.services.booking_tasks import BookingTaskService
from app.services.clinic_info import ClinicInfoService
from app.services.page_inspection import PageInspectionService
from app.services.browser_execution import (
    BrowserExecutionService,
    BrowserWorkerConfig,
    WorkerSource,
)
from app.services.parent_watchdog import ParentLiveness, parent_liveness, watch_liveness
from app.services.recovery import RecoveryService
from app.services.runtime import register_runtime_generation, runtime_ownership
from app.services.tasks import TaskService


def _worker_source(settings: Settings) -> WorkerSource | None:
    """An externally started worker, a runtime-owned one, or none at all."""
    external = _worker_config(settings)
    if external is not None:
        return external
    if settings.browser_site_origin is None and not settings.public_policy.configured:
        return None
    return ManagedBrowserWorker(
        site_origin=settings.browser_site_origin,
        headless=settings.browser_headless,
        timeout_seconds=settings.browser_worker_timeout_seconds,
        public_hosts=settings.public_inspection_hosts,
        inspection_test_origins=settings.inspection_test_origins,
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
