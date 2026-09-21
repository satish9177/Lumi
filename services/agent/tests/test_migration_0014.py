"""Milestone 9 S3, migration 0014: `desktop_dispatches`.

The dispatch row holds identifiers, opaque refs, digests and closed codes and nothing else; the column set
is pinned so that adding a title, a path, a handle or a value fails here first.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import downgrade, migrate, truncate_all
from tests.test_migration_0013 import columns, exists, revision, run


def test_migration_0014_adds_exactly_the_dispatch_table_and_downgrades_cleanly(migrated_database_url: str) -> None:
    url = migrated_database_url
    truncate_all(url)
    try:
        assert revision(url) == "0014" and exists(url, "desktop_dispatches")
        assert columns(url, "desktop_dispatches") == {
            "id", "action_id", "attempt_id", "worker_generation", "operation", "surface_ref", "surface_epoch",
            "observation_id", "snapshot_digest", "control_ref", "app_id", "input_tick", "status", "error_code",
            "result", "started_at", "finished_at",
        }
        for forbidden in ("title", "path", "hwnd", "pid", "handle", "value", "text", "name", "x", "y"):
            assert not any(forbidden in column.split("_") for column in columns(url, "desktop_dispatches")), forbidden
        downgrade(url, "0013")
        assert revision(url) == "0013" and not exists(url, "desktop_dispatches")
        assert exists(url, "desktop_disclosures") and exists(url, "desktop_observations")
        migrate(url)
        assert revision(url) == "0014" and exists(url, "desktop_dispatches")
    finally:
        migrate(url)
        truncate_all(url)
