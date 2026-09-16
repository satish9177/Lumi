from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.routes import router
from app.config import Settings
from app.db.engine import create_database_engine, ping_database
from app.db.migrations import verify_schema_is_current
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
            app.state.engine = engine
            app.state.task_service = TaskService(engine)
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Lumi Agent Runtime", version="0.1.0", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(router)
    return app
