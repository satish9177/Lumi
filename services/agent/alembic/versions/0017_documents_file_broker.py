"""Approved documents and the file broker, Milestone 10 S1.

S1 adds bounded, read-only local document authority and nothing that writes a file:

* `file_roots`: an M10 root with explicit `can_read` / `can_create` / `can_modify` booleans (modify fixed
  false in M10), bound to its canonical path and directory identity. Legacy search roots are untouched.
* `file_refs`: task-owned file authority (a root plus relative name, or one dropped file), bound to the
  file's (volume, index, size, mtime) and SHA-256.
* `documents`: bounded extracted text, `document_private`, purged after `expires_at`.
* `document_disclosures` / `document_answers` and the grant kind `document_disclose`: ONE exact,
  single-use provider disclosure of redacted excerpts, and its grounded result.

Downgrade refuses while any `document_disclose` grant or any file root exists (they are an audit record).

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_CK = "kind"
_OLD_KIND = (
    "kind IN ('public_research', 'authenticated_read', 'form_prepare', 'desktop_disclose', "
    "'desktop_action_plan', 'desktop_vision_capture', 'desktop_vision_disclose')"
)
_NEW_KIND = (
    "kind IN ('public_research', 'authenticated_read', 'form_prepare', 'desktop_disclose', "
    "'desktop_action_plan', 'desktop_vision_capture', 'desktop_vision_disclose', 'document_disclose')"
)


def upgrade() -> None:
    op.drop_constraint(_KIND_CK, "task_grants", type_="check")
    op.create_check_constraint(_KIND_CK, "task_grants", _NEW_KIND)

    op.create_table(
        "file_roots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("canonical_path", sa.Text(), nullable=False),
        sa.Column("path_key", sa.Text(), nullable=False),
        sa.Column("volume_serial", sa.BigInteger(), nullable=False),
        sa.Column("dir_index", sa.String(length=20), nullable=False),
        sa.Column("can_read", sa.Boolean(), nullable=False),
        sa.Column("can_create", sa.Boolean(), nullable=False),
        sa.Column("can_modify", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_roots")),
        sa.CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name=op.f("ck_file_roots_status")),
        sa.CheckConstraint("(revoked_at IS NOT NULL) = (status = 'REVOKED')", name=op.f("ck_file_roots_revoked_at_set")),
        sa.CheckConstraint("can_modify = false", name=op.f("ck_file_roots_no_modify_in_m10")),
        sa.CheckConstraint("can_read OR can_create", name=op.f("ck_file_roots_some_permission")),
        sa.CheckConstraint("dir_index ~ '^[0-9]{1,20}$'", name=op.f("ck_file_roots_dir_index_format")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_file_roots_revision_positive")),
        sa.CheckConstraint("length(label) BETWEEN 1 AND 64", name=op.f("ck_file_roots_label_present")),
    )
    op.create_index(
        "uq_file_roots_path_key_active",
        "file_roots",
        ["path_key"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "file_refs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("root_id", sa.Uuid(), nullable=True),
        sa.Column("relative_path", sa.Text(), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("volume_serial", sa.BigInteger(), nullable=False),
        sa.Column("file_index", sa.String(length=20), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_refs")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_file_refs_task_id_tasks"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["root_id"], ["file_roots.id"], name=op.f("fk_file_refs_root_id_file_roots"), ondelete="RESTRICT"
        ),
        sa.CheckConstraint("source IN ('ROOT_FILE', 'DROPPED_FILE')", name=op.f("ck_file_refs_source")),
        sa.CheckConstraint(
            "(source = 'ROOT_FILE' AND root_id IS NOT NULL AND relative_path IS NOT NULL AND local_path IS NULL) "
            "OR (source = 'DROPPED_FILE' AND root_id IS NULL AND relative_path IS NULL AND local_path IS NOT NULL)",
            name=op.f("ck_file_refs_source_shape"),
        ),
        sa.CheckConstraint("format IN ('pdf', 'docx', 'txt')", name=op.f("ck_file_refs_format")),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_file_refs_sha256_format")),
        sa.CheckConstraint("file_index ~ '^[0-9]{1,20}$'", name=op.f("ck_file_refs_file_index_format")),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_file_refs_size_non_negative")),
    )
    op.create_index("ix_file_refs_task_id", "file_refs", ["task_id"])

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("file_ref_id", sa.Uuid(), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("text_sha256", sa.String(length=64), nullable=False),
        sa.Column("text_chars", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("flags", postgresql.JSONB(), nullable=False),
        sa.Column("classification", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_documents_task_id_tasks"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["file_ref_id"], ["file_refs.id"], name=op.f("fk_documents_file_ref_id_file_refs"), ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("file_ref_id", name="uq_documents_file_ref_id"),
        sa.CheckConstraint("classification = 'document_private'", name=op.f("ck_documents_classification")),
        sa.CheckConstraint("format IN ('pdf', 'docx', 'txt')", name=op.f("ck_documents_format")),
        sa.CheckConstraint("text_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_documents_text_sha256_format")),
        sa.CheckConstraint("(purged_at IS NULL) = (text IS NOT NULL)", name=op.f("ck_documents_purged_means_no_text")),
        sa.CheckConstraint("text IS NULL OR length(text) <= 120000", name=op.f("ck_documents_text_bounded")),
        sa.CheckConstraint("jsonb_typeof(flags) = 'object'", name=op.f("ck_documents_flags_is_object")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_documents_expires_after_creation")),
    )
    op.create_index("ix_documents_task_id", "documents", ["task_id"])

    op.create_table(
        "document_disclosures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("recipient", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("projection_digest", sa.String(length=64), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("text_bytes", sa.Integer(), nullable=False),
        sa.Column("redaction_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_disclosures")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_document_disclosures_task_id_tasks"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["task_grants.id"], name=op.f("fk_document_disclosures_grant_id_task_grants"), ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("grant_id", name="uq_document_disclosures_grant_id"),
        sa.UniqueConstraint("task_id", name="uq_document_disclosures_task_id"),
        sa.CheckConstraint(
            "status IN ('STARTED', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN')", name=op.f("ck_document_disclosures_status")
        ),
        sa.CheckConstraint(
            "projection_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_document_disclosures_projection_digest_format")
        ),
        sa.CheckConstraint(
            "document_count BETWEEN 1 AND 2 AND text_bytes >= 0 AND redaction_count >= 0",
            name=op.f("ck_document_disclosures_counts_bounded"),
        ),
        sa.CheckConstraint(
            "(status = 'STARTED') = (finished_at IS NULL)", name=op.f("ck_document_disclosures_finished_when_not_started")
        ),
        sa.CheckConstraint(
            "(status IN ('FAILED', 'OUTCOME_UNKNOWN')) = (error_code IS NOT NULL)",
            name=op.f("ck_document_disclosures_error_code_when_not_ok"),
        ),
    )

    op.create_table(
        "document_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("disclosure_id", sa.Uuid(), nullable=False),
        sa.Column("classification", sa.String(length=24), nullable=False),
        sa.Column("recipient", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.String(length=800), nullable=True),
        sa.Column("reason", sa.String(length=24), nullable=True),
        sa.Column("findings", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_answers")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_document_answers_task_id_tasks"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["disclosure_id"],
            ["document_disclosures.id"],
            name=op.f("fk_document_answers_disclosure_id_document_disclosures"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("disclosure_id", name="uq_document_answers_disclosure_id"),
        sa.CheckConstraint("classification = 'document_private'", name=op.f("ck_document_answers_classification")),
        sa.CheckConstraint("kind IN ('comparison', 'cannot_compare')", name=op.f("ck_document_answers_kind")),
        sa.CheckConstraint("jsonb_typeof(findings) = 'array'", name=op.f("ck_document_answers_findings_is_array")),
        sa.CheckConstraint(
            "(kind = 'comparison') = (summary IS NOT NULL) AND (kind = 'cannot_compare') = (reason IS NOT NULL)",
            name=op.f("ck_document_answers_shape_matches_kind"),
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    held = connection.execute(sa.text("SELECT count(*) FROM task_grants WHERE kind = 'document_disclose'")).scalar()
    roots = connection.execute(sa.text("SELECT count(*) FROM file_roots")).scalar()
    if held or roots:
        raise RuntimeError("refusing to downgrade: document grants or file roots exist and are an audit record")
    op.drop_table("document_answers")
    op.drop_table("document_disclosures")
    op.drop_index("ix_documents_task_id", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_file_refs_task_id", table_name="file_refs")
    op.drop_table("file_refs")
    op.drop_index("uq_file_roots_path_key_active", table_name="file_roots")
    op.drop_table("file_roots")
    op.drop_constraint(_KIND_CK, "task_grants", type_="check")
    op.create_check_constraint(_KIND_CK, "task_grants", _OLD_KIND)
