import asyncio
import re
from enum import StrEnum

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from pydantic import SecretStr
from sqlalchemy import Connection, text

from app.config import Settings
from app.db.engine import create_database_engine
from app.db.migrations import SchemaNotCurrentError
from app.db.tables import metadata
from app.domain.action_status import ActionStatus, ApprovalStatus, AttemptOutcome, RiskTier
from app.domain.browser_dispatch import DispatchStatus
from app.domain.task_status import TaskStatus
from app.main import create_app
from tests.conftest import downgrade, migrate


def _diff(connection: Connection) -> list[object]:
    return list(compare_metadata(MigrationContext.configure(connection), metadata))


async def test_migrations_match_table_definitions(settings: Settings) -> None:
    engine = create_database_engine(settings)
    try:
        async with engine.connect() as connection:
            assert await connection.run_sync(_diff) == []
    finally:
        await engine.dispose()


async def _constraint(settings: Settings, name: str) -> str:
    engine = create_database_engine(settings)
    try:
        async with engine.connect() as connection:
            definition = await connection.scalar(
                text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
                {"name": name},
            )
    finally:
        await engine.dispose()
    assert isinstance(definition, str), f"constraint {name} does not exist"
    return definition


@pytest.mark.parametrize(
    ("constraint", "members"),
    [
        ("ck_tasks_status", TaskStatus),
        ("ck_actions_status", ActionStatus),
        ("ck_actions_risk_tier", RiskTier),
        ("ck_approvals_status", ApprovalStatus),
        ("ck_action_attempts_outcome", AttemptOutcome),
        ("ck_browser_dispatches_status", DispatchStatus),
    ],
)
async def test_check_constraints_admit_exactly_the_domain_states(
    settings: Settings, constraint: str, members: type[StrEnum]
) -> None:
    """Catches a new enum member added without a migration."""
    definition = await _constraint(settings, constraint)
    assert set(re.findall(r"'([A-Z0-9_]+)'", definition)) == {member.value for member in members}


async def test_the_proposal_immutability_trigger_is_installed(settings: Settings) -> None:
    engine = create_database_engine(settings)
    try:
        async with engine.connect() as connection:
            installed = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgname = 'actions_immutable_columns'"
                )
            )
    finally:
        await engine.dispose()
    assert installed == 1


def test_startup_refuses_an_unmigrated_database(migrated_database_url: str) -> None:
    # Sync test: Alembic's env.py runs its own event loop.
    settings = Settings(database_url=SecretStr(migrated_database_url))
    downgrade(migrated_database_url, "base")
    try:
        app = create_app(settings)

        async def start() -> None:
            async with app.router.lifespan_context(app):
                pass

        with pytest.raises(SchemaNotCurrentError, match="alembic upgrade head"):
            asyncio.run(start())
        # Startup must not have created anything from ORM metadata.
        assert not asyncio.run(_tasks_table_exists(settings))
    finally:
        migrate(migrated_database_url)


async def _tasks_table_exists(settings: Settings) -> bool:
    engine = create_database_engine(settings)
    try:
        async with engine.connect() as connection:
            return bool(await connection.scalar(text("SELECT to_regclass('public.tasks') IS NOT NULL")))
    finally:
        await engine.dispose()
