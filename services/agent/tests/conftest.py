import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from alembic import command
from fastapi import FastAPI
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import AGENT_ROOT, Settings
from app.db.engine import create_database_engine
from app.db.migrations import alembic_config
from app.main import create_app
from app.api.security import authorization_header
from app.services.actions import ActionService
from app.services.runtime import RuntimeGeneration, register_runtime_generation
from app.services.tasks import TaskService


class _TestEnvironment(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=AGENT_ROOT / ".env", extra="ignore", hide_input_in_errors=True
    )

    test_database_url: SecretStr | None = None


TRUNCATE_ALL = (
    "TRUNCATE orchestration_steps, orchestrations, workflow_values, workflow_candidates, workflow_steps, workflows, project_runs, project_recipes, projects, file_transfers, action_effect_keys, document_answers, document_disclosures, documents, file_refs, file_roots, desktop_dispatches, desktop_answers, desktop_disclosures, desktop_action_plans, "
    "desktop_vision_disclosures, desktop_captures, desktop_observations, desktop_worker_generations, form_drafts, protected_values, authenticated_answers, authenticated_observations, research_answers, "
    "research_observations, page_observations, browser_dispatches, "
    "research_sessions, step_authorizations, task_grants, login_attempts, browser_profiles, "
    "browser_worker_generations, action_attempts, approvals, "
    "actions, runtime_generations, task_events, tasks RESTART IDENTITY"
)
TEST_RUNTIME_TOKEN = SecretStr("test-runtime-token-with-at-least-32-bytes")
#: Milestone 10 S1 tables: absent at any revision before `0017`, for migration tests standing there.
M10_S1_TABLES = ("document_answers", "document_disclosures", "documents", "file_refs", "file_roots")
#: Milestone 10 S2 tables: absent before `0018`.
M10_S2_TABLES = ("file_transfers", "action_effect_keys")
#: Milestone 10 S3 tables: absent before `0019`.
M10_S3_TABLES = ("project_runs", "project_recipes", "projects")
#: Milestone 10 S4 tables: absent before `0020`.
M10_S4_TABLES = ("workflow_values", "workflow_candidates", "workflow_steps", "workflows")
#: Milestone 11 S2 tables: absent before `0021`.
M11_S2_TABLES = ("orchestration_steps", "orchestrations")


def truncate_all(database_url: str, *, without: tuple[str, ...] = ()) -> None:
    """Synchronous reset, for tests that drive the runtime as a subprocess.

    `without` names tables that do not exist at the (older) revision a migration test is standing on.
    """
    statement = TRUNCATE_ALL
    for table in without:
        statement = statement.replace(f"{table}, ", "")

    async def run() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(run())


def migrate(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url, configure_logging=False), revision)


def downgrade(database_url: str, revision: str) -> None:
    command.downgrade(alembic_config(database_url, configure_logging=False), revision)


def head_revision() -> str:
    """The newest migration, so a test standing on an older one need not name today's head."""
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(alembic_config("postgresql://unused/unused_test", configure_logging=False)).get_current_head()
    assert head is not None
    return head


@pytest.fixture(scope="session")
def test_database_url() -> str:
    configured = _TestEnvironment().test_database_url
    if configured is None:
        pytest.fail(
            "TEST_DATABASE_URL is not set. These tests need a real PostgreSQL database; "
            "see services/agent/README.md.",
            pytrace=False,
        )
    url = configured.get_secret_value()
    # The suite truncates and downgrades this database.
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must name a database ending in _test.", pytrace=False)
    return url


@pytest.fixture(scope="session")
def migrated_database_url(test_database_url: str) -> str:
    migrate(test_database_url)
    return test_database_url


@pytest.fixture
def settings(migrated_database_url: str) -> Settings:
    return Settings(
        database_url=SecretStr(migrated_database_url), runtime_token=TEST_RUNTIME_TOKEN
    )


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_database_engine(settings)
    async with engine.begin() as connection:
        await connection.execute(text(TRUNCATE_ALL))
    yield engine
    await engine.dispose()


@pytest.fixture
async def runtime_generation(engine: AsyncEngine) -> RuntimeGeneration:
    """A generation for service-level tests, which do not run the app lifespan."""
    return await register_runtime_generation(engine)


@pytest.fixture
def action_service(engine: AsyncEngine, runtime_generation: RuntimeGeneration) -> ActionService:
    return ActionService(
        engine, runtime_generation=runtime_generation.id, approval_ttl_seconds=300
    )


@pytest.fixture
def task_service(engine: AsyncEngine) -> TaskService:
    return TaskService(engine)


@asynccontextmanager
async def running_app(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's real lifespan (engine creation, schema check, disposal)."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            headers={"Authorization": authorization_header(TEST_RUNTIME_TOKEN)},
        ) as client:
            yield client


@pytest.fixture
async def client(settings: Settings, engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async with running_app(create_app(settings)) as client:
        yield client
