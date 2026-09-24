"""Milestone 10 S2, migration 0018: controlled downloads, file placement and the cross-executor effect keys.

Stands on 0018 explicitly (later slices sit on top), checks the database-level invariants, refuses to
downgrade over an audit record, and always leaves the database at head.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import M10_S3_TABLES, M10_S4_TABLES, M11_S2_TABLES, downgrade, head_revision, migrate, truncate_all
from tests.test_migration_0013 import exists, insert_grant, insert_task, revision, run

S2_TABLES = ("file_transfers", "action_effect_keys")
DIGEST = "a" * 64


def _root(url: str) -> uuid.UUID:
    root = uuid.uuid4()
    run(
        url,
        "INSERT INTO file_roots (id, label, canonical_path, path_key, volume_serial, dir_index, can_read, can_create, "
        "can_modify, status, revision) VALUES (:id, 'Downloads', 'C:\\Dl', :key, 1, '42', true, true, false, 'ACTIVE', 1)",
        id=root,
        key=f"c:\\dl-{root}",
    )
    return root


def _transfer(url: str, root: uuid.UUID, *, status: str = "PENDING", kind: str | None = None, max_bytes: int = 1024,
              sha256: str | None = None, length: int | None = None) -> None:
    task = insert_task(url)
    grant = insert_grant(url, task, "file_transfer")
    run(
        url,
        "INSERT INTO file_transfers (id, task_id, grant_id, source_url, source_digest, source_host, dest_root_id, "
        "dest_name, max_bytes, status, kind, sha256, length) VALUES (:id, :task, :grant, 'http://127.0.0.1/x.pdf', "
        ":digest, '127.0.0.1', :root, 'x.pdf', :max, :status, :kind, :sha, :length)",
        id=uuid.uuid4(), task=task, grant=grant, digest=DIGEST, root=root, max=max_bytes, status=status, kind=kind,
        sha=sha256, length=length,
    )


def test_migration_0018_adds_transfers_and_effect_keys_and_downgrades_only_without_audit_rows(migrated_database_url: str) -> None:
    url = migrated_database_url
    truncate_all(url)
    downgrade(url, "0018")
    try:
        assert revision(url) == "0018"
        for table in S2_TABLES:
            assert exists(url, table), table
        root = _root(url)

        # Closed type set, bounded size, and nothing reaches QUARANTINED/PLACED without a verified hash.
        for bad in ({"kind": "exe"}, {"max_bytes": 10485761}, {"status": "QUARANTINED"}, {"status": "DELETED"}):
            with pytest.raises(IntegrityError):
                _transfer(url, root, **bad)
        _transfer(url, root, status="QUARANTINED", kind="pdf", sha256=DIGEST, length=10)

        # Effect keys are a closed kind set with a fixed shape (no paths, no spaces, no separators).
        with pytest.raises(IntegrityError):
            run(url, "INSERT INTO action_effect_keys (action_id, effect_key, effect_kind) VALUES (:a, 'download:url:x', 'shell')", a=uuid.uuid4())
        with pytest.raises(IntegrityError):
            run(url, "INSERT INTO action_effect_keys (action_id, effect_key, effect_kind) VALUES (:a, 'C:\\evil path', 'download')", a=uuid.uuid4())

        with pytest.raises(Exception, match="refusing to downgrade"):
            downgrade(url, "0017")
        assert revision(url) == "0018"

        truncate_all(url, without=(*M10_S3_TABLES, *M10_S4_TABLES, *M11_S2_TABLES))
        downgrade(url, "0017")
        assert revision(url) == "0017"
        for table in S2_TABLES:
            assert not exists(url, table), table
        with pytest.raises(IntegrityError):
            insert_grant(url, insert_task(url), "file_transfer")
        run(url, "DELETE FROM task_grants")
        run(url, "DELETE FROM task_events")
        run(url, "DELETE FROM tasks")
    finally:
        migrate(url)
    assert revision(url) == head_revision()
