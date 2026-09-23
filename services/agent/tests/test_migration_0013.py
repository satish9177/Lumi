"""Milestone 9 S2, migration 0013: `desktop_disclosures`, `desktop_answers` and the `desktop_disclose` grant kind.

Modeled on the 0012 migration test: a synchronous test that drives alembic and inspects the real
database, and always leaves it at head.
"""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import M10_S1_TABLES, M10_S2_TABLES, M10_S3_TABLES, M10_S4_TABLES, head_revision, downgrade, migrate, truncate_all

NEW_TABLES = ("desktop_disclosures", "desktop_answers")
DIGEST = "a" * 64


def run(url: str, sql: str, **params: Any) -> Any:
    """One statement in its own transaction; returns the scalar (or None)."""

    async def go() -> Any:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(sql), params)
                return result.scalar() if result.returns_rows else None
        finally:
            await engine.dispose()

    return asyncio.run(go())


def exists(url: str, table: str) -> bool:
    return bool(run(url, f"SELECT to_regclass('public.{table}') IS NOT NULL"))


def revision(url: str) -> str:
    return str(run(url, "SELECT version_num FROM alembic_version"))


def columns(url: str, table: str) -> set[str]:
    async def go() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"), {"t": table}
                )
                return {row[0] for row in rows}
        finally:
            await engine.dispose()

    return asyncio.run(go())


def insert_task(url: str) -> uuid.UUID:
    task = uuid.uuid4()
    run(
        url,
        "INSERT INTO tasks (id, status, revision, last_event_sequence, request) "
        "VALUES (:id, 'CREATED', 1, 1, CAST('{}' AS jsonb))",
        id=task,
    )
    return task


def insert_grant(url: str, task: uuid.UUID, kind: str) -> uuid.UUID:
    grant = uuid.uuid4()
    run(
        url,
        "INSERT INTO task_grants (id, task_id, kind, status, revision, policy_version, scope, scope_digest) "
        "VALUES (:id, :task, :kind, 'PENDING', 1, 'v1', CAST('{}' AS jsonb), :digest)",
        id=grant, task=task, kind=kind, digest=DIGEST,
    )
    return grant


def insert_disclosure(url: str, task: uuid.UUID, grant: uuid.UUID, *, status: str = "STARTED") -> uuid.UUID:
    disclosure = uuid.uuid4()
    finished = "now()" if status != "STARTED" else "NULL"
    error = "'x'" if status in ("FAILED", "OUTCOME_UNKNOWN") else "NULL"
    run(
        url,
        "INSERT INTO desktop_disclosures (id, task_id, grant_id, observation_id, snapshot_digest, recipient, model, "
        "projection_digest, node_count, text_bytes, redaction_count, truncated, status, finished_at, error_code) "
        f"VALUES (:id, :task, :grant, :obs, :d, 'openai', 'm', :d, 1, 1, 0, false, :status, {finished}, {error})",
        id=disclosure, task=task, grant=grant, obs=uuid.uuid4(), d=DIGEST, status=status,
    )
    return disclosure


def insert_answer(url: str, task: uuid.UUID, disclosure: uuid.UUID, *, classification: str = "desktop_private") -> None:
    run(
        url,
        "INSERT INTO desktop_answers (id, task_id, disclosure_id, observation_id, classification, recipient, model, "
        "kind, answer, evidence) VALUES (:id, :task, :disclosure, :obs, :c, 'openai', 'm', 'answer', 'a', "
        "CAST('[]' AS jsonb))",
        id=uuid.uuid4(), task=task, disclosure=disclosure, obs=uuid.uuid4(), c=classification,
    )


def insert_observation(url: str) -> uuid.UUID:
    """A 0012-era row: runtime generation, worker generation, observation."""
    runtime, worker, observation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    run(url, "INSERT INTO runtime_generations (id) VALUES (:id)", id=runtime)
    run(
        url,
        "INSERT INTO desktop_worker_generations (id, runtime_generation, worker_started_at) VALUES (:id, :r, now())",
        id=worker, r=runtime,
    )
    run(
        url,
        "INSERT INTO desktop_observations (id, worker_generation, surface_ref, surface_epoch, schema_version, "
        "classification, snapshot, snapshot_digest, truncated) VALUES (:id, :w, 's1', 1, 1, 'desktop_private', "
        "CAST('{}' AS jsonb), :d, false)",
        id=observation, w=worker, d=DIGEST,
    )
    return observation


def test_migration_0013_adds_exactly_the_disclosure_tables_and_downgrades_cleanly(migrated_database_url: str) -> None:
    url = migrated_database_url
    truncate_all(url)
    try:
        # ---- clean at head ----
        assert revision(url) == head_revision()  # later slices sit on top; this test pins 0013 by stepping down to it
        downgrade(url, "0013")
        assert revision(url) == "0013"
        assert all(exists(url, table) for table in NEW_TABLES)
        # Audit metadata only: no column can hold a title, handle, snapshot, question or desktop text.
        assert columns(url, "desktop_disclosures") == {
            "id", "task_id", "grant_id", "observation_id", "snapshot_digest", "recipient", "model",
            "projection_digest", "node_count", "text_bytes", "redaction_count", "truncated", "status",
            "started_at", "finished_at", "error_code", "created_at",
        }
        assert columns(url, "desktop_answers") == {
            "id", "task_id", "disclosure_id", "observation_id", "classification", "recipient", "model", "kind",
            "answer", "reason", "evidence", "created_at",
        }

        # ---- 0012 data survives the upgrade ----
        downgrade(url, "0012")
        assert revision(url) == "0012" and not any(exists(url, table) for table in NEW_TABLES)
        assert exists(url, "desktop_observations") and exists(url, "form_drafts")
        task = insert_task(url)
        with pytest.raises(IntegrityError):  # the old kind set does not know the new kind
            insert_grant(url, task, "desktop_disclose")
        observation = insert_observation(url)
        migrate(url, "0013")
        assert revision(url) == "0013" and all(exists(url, table) for table in NEW_TABLES)
        assert run(url, "SELECT count(*) FROM desktop_observations WHERE id = :id AND classification = 'desktop_private'", id=observation) == 1

        # ---- the widened grant kind ----
        grant = insert_grant(url, task, "desktop_disclose")
        assert run(url, "SELECT profile_id IS NULL FROM task_grants WHERE id = :id", id=grant) is True
        with pytest.raises(IntegrityError):  # still a closed set
            insert_grant(url, task, "desktop_control")
        # profile_binding is intact: a desktop grant carries no profile, an account grant still must.
        with pytest.raises(IntegrityError):
            insert_grant(url, task, "form_prepare")

        # ---- one approval, one disclosure ----
        disclosure = insert_disclosure(url, task, grant)
        with pytest.raises(IntegrityError):
            insert_disclosure(url, task, grant)
        other_task = insert_task(url)
        with pytest.raises(IntegrityError):  # a fresh task but the same grant
            insert_disclosure(url, other_task, grant)
        with pytest.raises(IntegrityError):  # closed statuses
            insert_disclosure(url, other_task, insert_grant(url, other_task, "desktop_disclose"), status="MAYBE")

        # ---- private answers ----
        with pytest.raises(IntegrityError):
            insert_answer(url, task, disclosure, classification="account_private")
        insert_answer(url, task, disclosure)
        with pytest.raises(IntegrityError):  # one answer per disclosure
            insert_answer(url, task, disclosure)

        # ---- downgrade refuses to destroy a disclosure grant, and changes nothing ----
        with pytest.raises(Exception, match="refusing to downgrade"):
            downgrade(url, "0012")
        assert revision(url) == "0013" and all(exists(url, table) for table in NEW_TABLES)

        # ---- with no desktop grant left, downgrade removes exactly the two tables and restores the kinds ----
        truncate_all(
            url,
            without=(
                "desktop_dispatches", "desktop_action_plans", "desktop_vision_disclosures", "desktop_captures",
                *M10_S1_TABLES, *M10_S2_TABLES, *M10_S3_TABLES, *M10_S4_TABLES,
            ),
        )
        downgrade(url, "0012")
        assert revision(url) == "0012" and not any(exists(url, table) for table in NEW_TABLES)
        assert exists(url, "desktop_observations") and exists(url, "desktop_worker_generations")
        with pytest.raises(IntegrityError):
            insert_grant(url, insert_task(url), "desktop_disclose")
        insert_grant(url, insert_task(url), "public_research")

        # ---- re-upgrade restores ----
        run(url, "DELETE FROM task_grants")
        migrate(url, "0013")
        assert revision(url) == "0013" and all(exists(url, table) for table in NEW_TABLES)
    finally:
        migrate(url)
        truncate_all(url)
    assert revision(url) == head_revision()
