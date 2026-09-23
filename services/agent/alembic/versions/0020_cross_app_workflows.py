"""Cross-app preparation workflows, Milestone 10 S4.

* `workflows`: one deterministic controller over existing capabilities. It owns lineage only.
* `workflow_steps`: the controller-created child tasks, one per role (`download`, `documents`, `form`). A
  task belongs to at most one workflow.
* `workflow_candidates`: untrusted candidate fields, `document_extracted` (a local span) or
  `provider_derived` (a grounded quote). Composite foreign keys prove the document -- and the disclosure --
  belong to the workflow's own `documents` step.
* `workflow_values`: workflow-scoped protected values, each adopted by one exact approval from one
  candidate. A composite foreign key makes the value's kind, digest and provenance equal its candidate's;
  there is no user-typed provenance. M8's global `protected_values` is untouched.
* `documents` and `document_disclosures` gain `UNIQUE (id, task_id)` so those foreign keys can bind the task.

Downgrade refuses while any workflow exists (an audit record).

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = ("legal_name", "preferred_name", "email", "phone", "city", "country", "linkedin_url", "portfolio_url")
_KIND_CK = "kind IN (" + ", ".join(f"'{kind}'" for kind in _KINDS) + ")"
_DIGEST_MATCHES = "value IS NULL OR value_digest = encode(sha256(convert_to(value, 'UTF8')), 'hex')"


def upgrade() -> None:
    op.create_unique_constraint("uq_documents_id_task_id", "documents", ["id", "task_id"])
    op.create_unique_constraint("uq_document_disclosures_id_task_id", "document_disclosures", ["id", "task_id"])

    op.create_table(
        "workflows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("objective", sa.String(length=300), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.String(length=40), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflows")),
        sa.CheckConstraint("kind = 'cross_app_preparation'", name=op.f("ck_workflows_kind")),
        sa.CheckConstraint("status IN ('ACTIVE', 'STOPPED')", name=op.f("ck_workflows_status")),
        sa.CheckConstraint("(status = 'STOPPED') = (stopped_at IS NOT NULL)", name=op.f("ck_workflows_stopped_at_set")),
        sa.CheckConstraint("(stopped_at IS NULL) = (stop_reason IS NULL)", name=op.f("ck_workflows_stop_reason_set")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_workflows_expires_after_creation")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_workflows_revision_positive")),
    )

    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_steps")),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"], name=op.f("fk_workflow_steps_workflow_id_workflows"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_workflow_steps_task_id_tasks"), ondelete="RESTRICT"),
        sa.UniqueConstraint("task_id", name="uq_workflow_steps_task_id"),
        sa.UniqueConstraint("workflow_id", "role", name="uq_workflow_steps_workflow_id_role"),
        sa.UniqueConstraint("workflow_id", "task_id", "role", name="uq_workflow_steps_lineage"),
        sa.CheckConstraint("role IN ('download', 'documents', 'form')", name=op.f("ck_workflow_steps_role")),
    )

    op.create_table(
        "workflow_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("source_task_id", sa.Uuid(), nullable=False),
        sa.Column("source_role", sa.String(length=16), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("provenance", sa.String(length=24), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("value_digest", sa.String(length=64), nullable=False),
        sa.Column("preview", sa.String(length=120), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("disclosure_id", sa.Uuid(), nullable=True),
        sa.Column("projection_digest", sa.String(length=64), nullable=True),
        sa.Column("doc_ref", sa.String(length=2), nullable=True),
        sa.Column("quote", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_candidates")),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"], name=op.f("fk_workflow_candidates_workflow_id_workflows"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id", "source_task_id", "source_role"],
            ["workflow_steps.workflow_id", "workflow_steps.task_id", "workflow_steps.role"],
            name=op.f("fk_workflow_candidates_step"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "source_task_id"],
            ["documents.id", "documents.task_id"],
            name=op.f("fk_workflow_candidates_document"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["disclosure_id", "source_task_id"],
            ["document_disclosures.id", "document_disclosures.task_id"],
            name=op.f("fk_workflow_candidates_disclosure"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "workflow_id", "provenance", "kind", "value_digest", name="uq_workflow_candidates_lineage"),
        sa.UniqueConstraint(
            "workflow_id", "document_id", "provenance", "kind", "value_digest", name="uq_workflow_candidates_dedupe"
        ),
        sa.CheckConstraint("source_role = 'documents'", name=op.f("ck_workflow_candidates_source_role")),
        sa.CheckConstraint(_KIND_CK, name=op.f("ck_workflow_candidates_kind")),
        sa.CheckConstraint(
            "provenance IN ('document_extracted', 'provider_derived')", name=op.f("ck_workflow_candidates_provenance")
        ),
        sa.CheckConstraint("status IN ('PROPOSED', 'ADOPTED', 'DISMISSED')", name=op.f("ck_workflow_candidates_status")),
        sa.CheckConstraint("value_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_workflow_candidates_value_digest_format")),
        sa.CheckConstraint(
            "document_text_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_workflow_candidates_text_sha256_format")
        ),
        sa.CheckConstraint(_DIGEST_MATCHES, name=op.f("ck_workflow_candidates_digest_matches_value")),
        sa.CheckConstraint(
            "value IS NULL OR length(value) BETWEEN 1 AND 300", name=op.f("ck_workflow_candidates_value_bounded")
        ),
        sa.CheckConstraint(
            "(purged_at IS NULL) = (value IS NOT NULL)", name=op.f("ck_workflow_candidates_purged_means_no_value")
        ),
        sa.CheckConstraint("span_start >= 0 AND span_end > span_start", name=op.f("ck_workflow_candidates_span_ordered")),
        sa.CheckConstraint(
            "(provenance = 'document_extracted' AND disclosure_id IS NULL AND projection_digest IS NULL "
            "AND doc_ref IS NULL AND quote IS NULL) "
            "OR (provenance = 'provider_derived' AND disclosure_id IS NOT NULL AND projection_digest IS NOT NULL "
            "AND doc_ref IN ('d1', 'd2') AND (quote IS NOT NULL OR purged_at IS NOT NULL))",
            name=op.f("ck_workflow_candidates_provenance_shape"),
        ),
        sa.CheckConstraint(
            "projection_digest IS NULL OR projection_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_workflow_candidates_projection_digest_format"),
        ),
    )
    op.create_index("ix_workflow_candidates_workflow_id", "workflow_candidates", ["workflow_id"])

    op.create_table(
        "workflow_values",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("adopt_action_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("provenance", sa.String(length=24), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("value_digest", sa.String(length=64), nullable=False),
        sa.Column("preview", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_values")),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"], name=op.f("fk_workflow_values_workflow_id_workflows"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["adopt_action_id"], ["actions.id"], name=op.f("fk_workflow_values_adopt_action_id_actions"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "workflow_id", "provenance", "kind", "value_digest"],
            [
                "workflow_candidates.id",
                "workflow_candidates.workflow_id",
                "workflow_candidates.provenance",
                "workflow_candidates.kind",
                "workflow_candidates.value_digest",
            ],
            name=op.f("fk_workflow_values_candidate"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("candidate_id", name="uq_workflow_values_candidate_id"),
        sa.UniqueConstraint("adopt_action_id", name="uq_workflow_values_adopt_action_id"),
        sa.UniqueConstraint("workflow_id", "kind", name="uq_workflow_values_workflow_id_kind"),
        sa.CheckConstraint(_KIND_CK, name=op.f("ck_workflow_values_kind")),
        sa.CheckConstraint(
            "provenance IN ('document_extracted', 'provider_derived')", name=op.f("ck_workflow_values_provenance")
        ),
        sa.CheckConstraint("value_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_workflow_values_value_digest_format")),
        sa.CheckConstraint(_DIGEST_MATCHES, name=op.f("ck_workflow_values_digest_matches_value")),
        sa.CheckConstraint("value IS NULL OR length(value) BETWEEN 1 AND 300", name=op.f("ck_workflow_values_value_bounded")),
        sa.CheckConstraint(
            "(purged_at IS NULL) = (value IS NOT NULL)", name=op.f("ck_workflow_values_purged_means_no_value")
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT count(*) FROM workflows")).scalar():
        raise RuntimeError("refusing to downgrade: workflows exist and are an audit record")
    op.drop_table("workflow_values")
    op.drop_index("ix_workflow_candidates_workflow_id", table_name="workflow_candidates")
    op.drop_table("workflow_candidates")
    op.drop_table("workflow_steps")
    op.drop_table("workflows")
    op.drop_constraint("uq_document_disclosures_id_task_id", "document_disclosures", type_="unique")
    op.drop_constraint("uq_documents_id_task_id", "documents", type_="unique")
