import asyncio
from typing import Any
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
from tests.conftest import TEST_RUNTIME_TOKEN, downgrade, migrate


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
    settings = Settings(
        database_url=SecretStr(migrated_database_url), runtime_token=TEST_RUNTIME_TOKEN
    )
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


def test_migration_0010_round_trips_and_removes_only_its_own_shape(migrated_database_url: str) -> None:
    """Milestone 8b S5: `protected_values` and the `form_prepare` grant kind.

    Downgrading to 0009 removes the table and the third grant kind (and any grant of
    it), keeps every S3/S4 row, and upgrading again restores the S5 shape.
    """
    import uuid

    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        settings = Settings(
            database_url=SecretStr(migrated_database_url), runtime_token=TEST_RUNTIME_TOKEN
        )
        engine = create_database_engine(settings)
        task_id, profile_id = uuid.uuid4(), uuid.uuid4()
        try:
            async with engine.begin() as connection:
                await connection.execute(text("TRUNCATE tasks, browser_profiles, protected_values CASCADE"))
                await connection.execute(
                    text("INSERT INTO tasks (id, status, request, revision, last_event_sequence) VALUES (:t, 'READY', '{}'::jsonb, 1, 1)"),
                    {"t": task_id},
                )
                await connection.execute(
                    text("INSERT INTO browser_profiles (id, label, site, allowed_origins, status, revision, revoke_epoch) VALUES (:p, 'L', 'example.test', '[]'::jsonb, 'AUTHENTICATED', 1, 0)"),
                    {"p": profile_id},
                )
                for kind in ("authenticated_read", "form_prepare"):
                    await connection.execute(
                        text("INSERT INTO task_grants (id, task_id, kind, status, revision, policy_version, scope, scope_digest, profile_id, profile_revoke_epoch) "
                             "VALUES (gen_random_uuid(), :t, :k, 'PENDING', 1, 'v', '{}'::jsonb, :d, :p, 0)"),
                        {"t": task_id, "k": kind, "d": "0" * 64, "p": profile_id},
                    )
                await connection.execute(
                    text("INSERT INTO protected_values (id, kind, value, value_digest, preview) "
                         "VALUES (gen_random_uuid(), 'city', 'Pune', encode(sha256(convert_to('Pune', 'UTF8')), 'hex'), 'saved city')")
                )
        finally:
            await engine.dispose()
        return task_id, profile_id

    async def counts() -> dict[str, Any]:
        settings = Settings(
            database_url=SecretStr(migrated_database_url), runtime_token=TEST_RUNTIME_TOKEN
        )
        engine = create_database_engine(settings)
        try:
            async with engine.connect() as connection:
                kinds = (await connection.execute(text("SELECT kind FROM task_grants ORDER BY kind"))).scalars().all()
                table = await connection.scalar(text("SELECT to_regclass('public.protected_values') IS NOT NULL"))
            return {"kinds": kinds, "table": table}
        finally:
            await engine.dispose()

    asyncio.run(seed())
    assert asyncio.run(counts()) == {"kinds": ["authenticated_read", "form_prepare"], "table": True}
    downgrade(migrated_database_url, "0009")
    try:
        assert asyncio.run(counts()) == {"kinds": ["authenticated_read"], "table": False}
    finally:
        migrate(migrated_database_url)
    assert asyncio.run(counts())["table"] is True


def test_migration_0011_round_trips_keeps_history_and_enforces_its_constraints(
    migrated_database_url: str,
) -> None:
    """Milestone 8b S6: `form_drafts` and `browser_dispatches.frozen_at`.

    Existing S3/S4/S5 data stays valid (an old dispatch has `frozen_at IS NULL`, and a
    historical `prepared_nothing` result is untouched), the draft table refuses what it must,
    and downgrading to 0010 removes exactly the S6 shape.
    """
    import uuid

    from sqlalchemy.exc import DBAPIError, IntegrityError

    ids = {name: uuid.uuid4() for name in ("task", "profile", "action", "attempt", "dispatch", "runtime", "worker")}

    def engine_for() -> Any:
        settings = Settings(database_url=SecretStr(migrated_database_url), runtime_token=TEST_RUNTIME_TOKEN)
        return create_database_engine(settings)

    async def prepare() -> None:
        engine = engine_for()
        try:
            async with engine.begin() as c:
                await c.execute(text("TRUNCATE form_drafts, browser_dispatches, action_attempts, approvals, actions, tasks, browser_profiles, browser_worker_generations, runtime_generations CASCADE"))
                await c.execute(text("INSERT INTO tasks (id, status, request, revision, last_event_sequence) VALUES (:t, 'READY', '{}'::jsonb, 1, 1)"), {"t": ids["task"]})
                await c.execute(text("INSERT INTO browser_profiles (id, label, site, allowed_origins, status, revision, revoke_epoch) VALUES (:p, 'L', 'example.test', '[]'::jsonb, 'AUTHENTICATED', 1, 0)"), {"p": ids["profile"]})
                await c.execute(text("INSERT INTO runtime_generations (id) VALUES (:r)"), {"r": ids["runtime"]})
                await c.execute(text("INSERT INTO browser_worker_generations (id, runtime_generation, worker_started_at) VALUES (:w, :r, now())"), {"w": ids["worker"], "r": ids["runtime"]})
                await c.execute(
                    text("INSERT INTO actions (id, task_id, idempotency_key, tool_name, risk_tier, proposal, proposal_digest, status, revision) VALUES (:a, :t, 'k', 'prepare_form', 'R2', '{}'::jsonb, :d, 'SUCCEEDED', 1)"),
                    {"a": ids["action"], "t": ids["task"], "d": "0" * 64},
                )
                approval = uuid.uuid4()
                await c.execute(
                    text("INSERT INTO approvals (id, action_id, action_revision, proposal_digest, status, expires_at, approved_at, consumed_at) VALUES (:ap, :a, 1, :d, 'CONSUMED', now() + interval '1 hour', now(), now())"),
                    {"ap": approval, "a": ids["action"], "d": "0" * 64},
                )
                await c.execute(
                    text("INSERT INTO action_attempts (id, action_id, attempt_number, approval_id, runtime_generation, finished_at, outcome, result) VALUES (:at, :a, 1, :ap, :r, now(), 'SUCCEEDED', '{\"code\": \"prepared_nothing\"}'::jsonb)"),
                    {"at": ids["attempt"], "a": ids["action"], "r": ids["runtime"], "ap": approval},
                )
                await c.execute(
                    text("INSERT INTO browser_dispatches (id, action_id, attempt_id, worker_generation, operation, site, effect, status, submitted) VALUES (:d, :a, :at, :w, 'authenticated_observe', 'authenticated', 'ACCOUNT_READ', 'DISPATCHED', false)"),
                    {"d": ids["dispatch"], "a": ids["action"], "at": ids["attempt"], "w": ids["worker"]},
                )
        finally:
            await engine.dispose()

    async def draft(status: str = "PREPARED", *, digest: str = "a" * 64, fields: str = "[]", fresh: bool = False) -> None:
        engine = engine_for()
        try:
            async with engine.begin() as c:
                attempt, dispatch = (uuid.uuid4(), uuid.uuid4()) if fresh else (ids["attempt"], ids["dispatch"])
                await c.execute(
                    text("INSERT INTO form_drafts (id, task_id, profile_id, action_id, attempt_id, dispatch_id, manifest_digest, draft_digest, observation_id, tab, document_epoch, form_epoch, form_ref, status, fields) VALUES (gen_random_uuid(), :t, :p, :a, :at, :d, :m, :dd, gen_random_uuid(), 't1', 1, 1, 'f1', :s, CAST(:f AS jsonb))"),
                    {"t": ids["task"], "p": ids["profile"], "a": ids["action"], "at": attempt, "d": dispatch, "m": "b" * 64, "dd": digest, "s": status, "f": fields},
                )
        finally:
            await engine.dispose()

    async def state() -> dict[str, Any]:
        engine = engine_for()
        try:
            async with engine.connect() as c:
                return {
                    "table": await c.scalar(text("SELECT to_regclass('public.form_drafts') IS NOT NULL")),
                    "column": await c.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_name = 'browser_dispatches' AND column_name = 'frozen_at'")),
                }
        finally:
            await engine.dispose()

    async def old() -> tuple[Any, Any]:
        engine = engine_for()
        try:
            async with engine.connect() as c:
                return (
                    await c.scalar(text("SELECT frozen_at FROM browser_dispatches WHERE id = :d"), {"d": ids["dispatch"]}),
                    await c.scalar(text("SELECT result->>'code' FROM action_attempts WHERE id = :a"), {"a": ids["attempt"]}),
                )
        finally:
            await engine.dispose()

    asyncio.run(prepare())
    assert asyncio.run(state()) == {"table": True, "column": 1}
    assert asyncio.run(old()) == (None, "prepared_nothing")

    asyncio.run(draft("PREPARED"))
    bad_rows = (
        ("ARBITRARY", "a" * 64, "[]"),
        ("DISCARDED", "not-a-digest", "[]"),
        ("DISCARDED", "a" * 64, "{}"),
        ("DISCARDED", "a" * 64, "[" + ",".join(["{}"] * 13) + "]"),
    )
    for status, digest, fields in bad_rows:
        with pytest.raises((IntegrityError, DBAPIError)):
            asyncio.run(draft(status, digest=digest, fields=fields, fresh=True))
    with pytest.raises((IntegrityError, DBAPIError)):
        asyncio.run(draft("STALE"))  # a second live draft for the same profile

    downgrade(migrated_database_url, "0010")
    try:
        assert asyncio.run(state()) == {"table": False, "column": 0}
    finally:
        migrate(migrated_database_url)
    assert asyncio.run(state()) == {"table": True, "column": 1}
