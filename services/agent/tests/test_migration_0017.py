"""Milestone 10 S1, migration 0017: M10 file roots, file refs, documents and the document-disclosure grant.

Stands on 0017 explicitly (later slices sit on top), checks the shape and the database-level invariants,
refuses to downgrade over an audit record, and always leaves the database at head.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import head_revision, downgrade, migrate, truncate_all
from tests.test_migration_0013 import exists, insert_grant, insert_task, revision, run

S1_TABLES = ("file_roots", "file_refs", "documents", "document_disclosures", "document_answers")


def _root(url: str, *, can_modify: bool = False, path_key: str = "c:\\docs") -> None:
    run(
        url,
        "INSERT INTO file_roots (id, label, canonical_path, path_key, volume_serial, dir_index, can_read, can_create, "
        "can_modify, status, revision) VALUES (:id, 'Docs', 'C:\\Docs', :key, 1, '42', true, false, :modify, 'ACTIVE', 1)",
        id=uuid.uuid4(),
        key=path_key,
        modify=can_modify,
    )


def test_migration_0017_adds_the_document_tables_and_downgrades_only_without_audit_rows(migrated_database_url: str) -> None:
    url = migrated_database_url
    truncate_all(url)
    downgrade(url, "0017")
    try:
        assert revision(url) == "0017"
        for table in S1_TABLES:
            assert exists(url, table), table

        # can_modify is represented and cannot be true in M10; one live root per folder.
        with pytest.raises(IntegrityError):
            _root(url, can_modify=True)
        _root(url)
        with pytest.raises(IntegrityError):
            _root(url)
        _root(url, path_key="c:\\other")

        # the widened grant kind, still a closed set
        task = insert_task(url)
        insert_grant(url, task, "document_disclose")
        with pytest.raises(IntegrityError):
            insert_grant(url, task, "document_write")

        with pytest.raises(Exception, match="refusing to downgrade"):
            downgrade(url, "0016")
        assert revision(url) == "0017"

        truncate_all(url)
        downgrade(url, "0016")
        assert revision(url) == "0016"
        for table in S1_TABLES:
            assert not exists(url, table), table
        with pytest.raises(IntegrityError):
            insert_grant(url, insert_task(url), "document_disclose")
        run(url, "DELETE FROM task_grants")
        run(url, "DELETE FROM task_events")
        run(url, "DELETE FROM tasks")
    finally:
        migrate(url)
    assert revision(url) == head_revision()
