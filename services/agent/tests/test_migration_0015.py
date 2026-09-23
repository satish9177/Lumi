"""Milestone 9 S4, migration 0015: the widened `desktop_dispatches` and `desktop_action_plans`.

Modeled on the 0013/0014 migration tests: a synchronous test that drives alembic and inspects the real
database, and always leaves it at head. Row-level exercise of the new dispatch operations' CHECK
constraints (which need real `actions`/`action_attempts`/`desktop_worker_generations` rows to satisfy
their foreign keys) is covered end to end, against real PostgreSQL, by `test_desktop_actions_service.py`;
this file is about the migration's own shape and its downgrade/upgrade cleanliness.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import M10_S1_TABLES, M10_S2_TABLES, M10_S3_TABLES, head_revision, downgrade, migrate, truncate_all
from tests.test_migration_0013 import columns, exists, insert_grant, insert_task, revision, run

NEW_DISPATCH_COLUMNS = ("value_ref", "option_container_ref", "invoke_effect")
OLD_DISPATCH_COLUMNS = {
    "id", "action_id", "attempt_id", "worker_generation", "operation", "surface_ref", "surface_epoch",
    "observation_id", "snapshot_digest", "control_ref", "app_id", "input_tick", "status", "error_code",
    "result", "started_at", "finished_at",
}
PLAN_COLUMNS = {
    "id", "task_id", "grant_id", "observation_id", "snapshot_digest", "recipient", "model",
    "projection_digest", "node_count", "text_bytes", "redaction_count", "truncated", "status",
    "proposed_action", "started_at", "finished_at", "error_code", "created_at",
}


def test_migration_0015_widens_dispatches_and_adds_desktop_action_plans(migrated_database_url: str) -> None:
    url = migrated_database_url
    truncate_all(url)
    # `migrated_database_url` is at head, which S5 (0016) moved past 0015; this test is specifically
    # about 0015's own shape, so it stands on 0015 explicitly rather than assuming head == 0015.
    downgrade(url, "0015")
    try:
        # ---- clean at 0015 ----
        assert revision(url) == "0015"
        assert exists(url, "desktop_action_plans")
        assert columns(url, "desktop_action_plans") == PLAN_COLUMNS
        assert columns(url, "desktop_dispatches") == OLD_DISPATCH_COLUMNS | set(NEW_DISPATCH_COLUMNS)
        # No raw value, title, path or handle anywhere -- only opaque refs and a closed effect name.
        for forbidden in ("title", "path", "hwnd", "pid", "handle", "text", "name", "x", "y"):
            assert not any(forbidden in column.split("_") for column in columns(url, "desktop_dispatches")), forbidden
        # ("text" is not checked here: `text_bytes` is a legitimate byte COUNT, exactly like S2's
        # `desktop_disclosures.text_bytes`, never the text itself.)
        assert not any(
            forbidden in column.split("_") for forbidden in ("title", "path", "hwnd", "pid", "handle")
            for column in columns(url, "desktop_action_plans")
        )

        # ---- the widened grant kind ----
        task = insert_task(url)
        grant = insert_grant(url, task, "desktop_action_plan")
        assert run(url, "SELECT profile_id IS NULL FROM task_grants WHERE id = :id", id=grant) is True
        with pytest.raises(IntegrityError):  # still a closed set
            insert_grant(url, task, "desktop_control_plan")

        # ---- downgrade refuses to destroy a live plan grant ----
        with pytest.raises(Exception, match="refusing to downgrade"):
            downgrade(url, "0014")
        assert revision(url) == "0015" and exists(url, "desktop_action_plans")

        # ---- with no plan grant left, downgrade removes exactly what 0015 added ----
        # Standing at 0015 (S5's tables do not exist here), so they must be excluded from the shared
        # TRUNCATE_ALL list, exactly like S3/S4's own tables are excluded in `test_migration_0013.py`.
        truncate_all(url, without=("desktop_vision_disclosures", "desktop_captures", *M10_S1_TABLES, *M10_S2_TABLES, *M10_S3_TABLES))
        downgrade(url, "0014")
        assert revision(url) == "0014"
        assert not exists(url, "desktop_action_plans")
        assert columns(url, "desktop_dispatches") == OLD_DISPATCH_COLUMNS
        with pytest.raises(IntegrityError):  # the old kind set does not know the new kind
            insert_grant(url, insert_task(url), "desktop_action_plan")
        insert_grant(url, insert_task(url), "desktop_disclose")  # S2's kind still works

        # ---- re-upgrade restores ----
        run(url, "DELETE FROM task_grants")
        migrate(url, "0015")
        assert revision(url) == "0015"
        assert exists(url, "desktop_action_plans")
        assert columns(url, "desktop_dispatches") == OLD_DISPATCH_COLUMNS | set(NEW_DISPATCH_COLUMNS)
    finally:
        migrate(url)
        truncate_all(url)
    assert revision(url) == head_revision()
