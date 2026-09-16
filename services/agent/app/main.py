from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.routes import router
from app.config import Settings
from app.db.engine import create_database_engine, ping_database
from app.db.migrations import verify_schema_is_current
from app.services.actions import ActionService
from app.services.recovery import RecoveryService
from app.services.runtime import register_runtime_generation
from app.services.tasks import TaskService


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Run with `uvicorn --factory app.main:create_app`."""
    # Missing or invalid configuration fails here, before the server binds.
    resolved = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_database_engine(resolved)
        try:
            await ping_database(engine)
            await verify_schema_is_current(engine)
            generation = await register_runtime_generation(engine)
            # Before serving anything: any execution a previous runtime started
            # but never finished becomes OUTCOME_UNKNOWN. Running this first
            # means no request can ever observe such an action as still
            # EXECUTING, and nothing is retried on the strength of a guess.
            await RecoveryService(engine).recover_unfinished_attempts(generation.id)
            app.state.engine = engine
            app.state.runtime_generation = generation
            app.state.task_service = TaskService(engine)
            app.state.action_service = ActionService(
                engine,
                runtime_generation=generation.id,
                approval_ttl_seconds=resolved.approval_ttl_seconds,
            )
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Lumi Agent Runtime", version="0.2.0", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(router)
    return app
