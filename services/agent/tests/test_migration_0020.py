"""Milestone 10 S4, migration 0020: workflows, steps, candidates and workflow-scoped values.

Stands on 0020 explicitly, checks the database-level lineage and provenance invariants, refuses to downgrade
over an audit record, and always leaves the database at head.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import M11_S2_TABLES, M12_S1_TABLES, downgrade, head_revision, migrate, truncate_all
from tests.test_migration_0013 import exists, insert_task, revision, run

S4_TABLES = ("workflows", "workflow_steps", "workflow_candidates", "workflow_values")


def _workflow(url: str) -> uuid.UUID:
    workflow = uuid.uuid4()
    run(
        url,
        "INSERT INTO workflows (id, kind, status, objective, revision, expires_at) "
        "VALUES (:id, 'cross_app_preparation', 'ACTIVE', 'Prepare', 1, now() + interval '1 day')",
        id=workflow,
    )
    return workflow


def _step(url: str, workflow: uuid.UUID, role: str, task: uuid.UUID | None = None) -> uuid.UUID:
    task = task or insert_task(url)
    run(
        url,
        "INSERT INTO workflow_steps (id, workflow_id, task_id, role) VALUES (gen_random_uuid(), :w, :t, :r)",
        w=workflow, t=task, r=role,
    )
    return task


def _document(url: str, task: uuid.UUID) -> uuid.UUID:
    root = uuid.uuid4()
    run(
        url,
        "INSERT INTO file_roots (id, label, canonical_path, path_key, volume_serial, dir_index, can_read, can_create, "
        "can_modify, status, revision) VALUES (:id, 'R', 'C:\\R', :key, 1, '1', true, false, false, 'ACTIVE', 1)",
        id=root, key=f"c:\\r-{root}",
    )
    ref = uuid.uuid4()
    run(
        url,
        "INSERT INTO file_refs (id, task_id, source, root_id, relative_path, display_name, format, volume_serial, "
        "file_index, size_bytes, mtime_ns, sha256) VALUES (:id, :t, 'ROOT_FILE', :r, 'a.txt', 'a.txt', 'txt', 1, "
        "'2', 3, 4, repeat('a', 64))",
        id=ref, t=task, r=root,
    )
    document = uuid.uuid4()
    run(
        url,
        "INSERT INTO documents (id, task_id, file_ref_id, format, text, text_sha256, text_chars, truncated, flags, "
        "classification, expires_at) VALUES (:id, :t, :ref, 'txt', 'City: Pune', repeat('b', 64), 10, false, "
        "CAST('{}' AS jsonb), 'document_private', now() + interval '1 day')",
        id=document, t=task, ref=ref,
    )
    return document


def _candidate(url: str, workflow: uuid.UUID, task: uuid.UUID, document: uuid.UUID, *, provenance: str = "document_extracted") -> uuid.UUID:
    candidate = uuid.uuid4()
    run(
        url,
        "INSERT INTO workflow_candidates (id, workflow_id, source_task_id, source_role, document_id, document_text_sha256, "
        "kind, provenance, value, value_digest, preview, span_start, span_end, status) VALUES (:id, :w, :t, 'documents', "
        ":d, repeat('b', 64), 'city', :p, 'Pune', encode(sha256(convert_to('Pune', 'UTF8')), 'hex'), 'saved city', 6, 10, "
        "'PROPOSED')",
        id=candidate, w=workflow, t=task, d=document, p=provenance,
    )
    return candidate


def test_migration_0020_enforces_lineage_and_provenance_and_downgrades_only_without_audit_rows(migrated_database_url: str) -> None:
    url = migrated_database_url
    truncate_all(url)
    downgrade(url, "0020")
    try:
        assert revision(url) == "0020"
        for table in S4_TABLES:
            assert exists(url, table), table
        workflow = _workflow(url)
        docs = _step(url, workflow, "documents")
        document = _document(url, docs)
        # A second task in the same role, or the same task in another workflow, is refused.
        with pytest.raises(IntegrityError):
            _step(url, workflow, "documents")
        with pytest.raises(IntegrityError):
            _step(url, _workflow(url), "form", task=docs)
        # A candidate must come from the workflow's documents step, and its document must be that task's.
        candidate = _candidate(url, workflow, docs, document)
        other_workflow = _workflow(url)
        with pytest.raises(IntegrityError):
            _candidate(url, other_workflow, docs, document)
        form = _step(url, workflow, "form")
        with pytest.raises(IntegrityError):
            _candidate(url, workflow, form, document)
        # A provider-derived candidate needs its disclosure, projection, doc ref and quote.
        with pytest.raises(IntegrityError):
            _candidate(url, workflow, docs, document, provenance="provider_derived")
        # A value copies its candidate's kind, provenance and digest; there is no user-typed provenance.
        action = uuid.uuid4()
        run(
            url,
            "INSERT INTO actions (id, task_id, idempotency_key, tool_name, risk_tier, status, proposal, proposal_digest, revision) "
            "VALUES (:id, :t, 'k', 'adopt_workflow_value', 'R2', 'SUCCEEDED', CAST('{}' AS jsonb), repeat('c', 64), 1)",
            id=action, t=docs,
        )
        for provenance, kind in (("provider_derived", "city"), ("document_extracted", "country"), ("user_typed", "city")):
            with pytest.raises(IntegrityError):
                run(
                    url,
                    "INSERT INTO workflow_values (id, workflow_id, candidate_id, adopt_action_id, kind, provenance, value, "
                    "value_digest, preview) VALUES (gen_random_uuid(), :w, :c, :a, :k, :p, 'Pune', "
                    "encode(sha256(convert_to('Pune', 'UTF8')), 'hex'), 'saved city')",
                    w=workflow, c=candidate, a=action, k=kind, p=provenance,
                )
        run(
            url,
            "INSERT INTO workflow_values (id, workflow_id, candidate_id, adopt_action_id, kind, provenance, value, "
            "value_digest, preview) VALUES (gen_random_uuid(), :w, :c, :a, 'city', 'document_extracted', 'Pune', "
            "encode(sha256(convert_to('Pune', 'UTF8')), 'hex'), 'saved city')",
            w=workflow, c=candidate, a=action,
        )

        with pytest.raises(Exception, match="refusing to downgrade"):
            downgrade(url, "0019")
        assert revision(url) == "0020"

        truncate_all(url, without=(*M11_S2_TABLES, *M12_S1_TABLES))
        downgrade(url, "0019")
        assert revision(url) == "0019"
        for table in S4_TABLES:
            assert not exists(url, table), table
        run(url, "DELETE FROM task_events")
        run(url, "DELETE FROM tasks")
    finally:
        migrate(url)
    assert revision(url) == head_revision()
