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
from app.services.actions import ActionService
from app.services.browser_execution import BrowserExecutionService, BrowserWorkerConfig
from app.services.parent_watchdog import ParentLiveness, parent_liveness, watch_liveness
from app.services.recovery import RecoveryService
from app.services.runtime import register_runtime_generation, runtime_ownership
from app.services.tasks import TaskService


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
                app.state.task_service = TaskService(engine)
                action_service = ActionService(
                    engine,
                    runtime_generation=generation.id,
                    approval_ttl_seconds=resolved.approval_ttl_seconds,
                )
                app.state.action_service = action_service
                app.state.browser_execution_service = BrowserExecutionService(
                    engine,
                    actions=action_service,
                    runtime_generation=generation.id,
                    worker=_worker_config(resolved),
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
                yield
        finally:
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
