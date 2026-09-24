"""Milestone 10 S3, migration 0019: projects, recipes, runs and the `project_run` grant kind.

Stands on 0019 explicitly, checks the database-level invariants (one live run per project, a fixed stop
policy, pid with creation time), refuses to downgrade over an audit record, and always leaves the database
at head.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import M10_S4_TABLES, M11_S2_TABLES, downgrade, head_revision, migrate, truncate_all
from tests.test_migration_0013 import exists, insert_grant, insert_task, revision, run

S3_TABLES = ("projects", "project_recipes", "project_runs")
DIGEST = "b" * 64


def _project(url: str) -> uuid.UUID:
    project = uuid.uuid4()
    run(
        url,
        "INSERT INTO projects (id, label, canonical_path, path_key, volume_serial, dir_index, status, revision) "
        "VALUES (:id, 'Lumi', 'C:\\P', :key, 1, '7', 'ACTIVE', 1)",
        id=project, key=f"c:\\p-{project}",
    )
    return project


def _recipe(url: str, project: uuid.UUID, *, stop_policy: str = "terminate_job") -> uuid.UUID:
    recipe = uuid.uuid4()
    run(
        url,
        "INSERT INTO project_recipes (id, project_id, label, script_name, spec, digest, status, revision) "
        "VALUES (:id, :project, 'Start', 'dev', CAST(:spec AS jsonb), :digest, 'ACTIVE', 1)",
        id=recipe, project=project, spec='{"stop_policy": "%s"}' % stop_policy, digest=DIGEST,
    )
    return recipe


def _run(url: str, project: uuid.UUID, recipe: uuid.UUID, *, status: str = "STARTING", pid: int | None = None) -> None:
    task = insert_task(url)
    grant = insert_grant(url, task, "project_run")
    run(
        url,
        "INSERT INTO project_runs (id, task_id, grant_id, recipe_id, project_id, recipe_digest, status, pid) "
        "VALUES (:id, :task, :grant, :recipe, :project, :digest, :status, :pid)",
        id=uuid.uuid4(), task=task, grant=grant, recipe=recipe, project=project, digest=DIGEST, status=status, pid=pid,
    )


def test_migration_0019_adds_projects_and_runs_and_downgrades_only_without_audit_rows(migrated_database_url: str) -> None:
    url = migrated_database_url
    truncate_all(url)
    downgrade(url, "0019")
    try:
        assert revision(url) == "0019"
        for table in S3_TABLES:
            assert exists(url, table), table
        project = _project(url)
        with pytest.raises(IntegrityError):
            _recipe(url, project, stop_policy="kill_by_name")
        recipe = _recipe(url, project)

        _run(url, project, recipe)
        # One live run per project, decided by the database.
        with pytest.raises(IntegrityError):
            _run(url, project, recipe)
        # A pid without its creation time is not a process identity.
        with pytest.raises(IntegrityError):
            _run(url, _project(url), recipe, status="FAILED", pid=42)
        # RUNNING needs a resumed, recorded process.
        with pytest.raises(IntegrityError):
            _run(url, _project(url), recipe, status="RUNNING")

        with pytest.raises(Exception, match="refusing to downgrade"):
            downgrade(url, "0018")
        assert revision(url) == "0019"

        truncate_all(url, without=(*M10_S4_TABLES, *M11_S2_TABLES))
        downgrade(url, "0018")
        assert revision(url) == "0018"
        for table in S3_TABLES:
            assert not exists(url, table), table
        with pytest.raises(IntegrityError):
            insert_grant(url, insert_task(url), "project_run")
        run(url, "DELETE FROM task_grants")
        run(url, "DELETE FROM task_events")
        run(url, "DELETE FROM tasks")
    finally:
        migrate(url)
    assert revision(url) == head_revision()
