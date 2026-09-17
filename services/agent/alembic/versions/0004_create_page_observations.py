"""Durable page observations for Milestone 7a public page inspection.

One row per successful `inspect_public_page` execution attempt: the bounded,
hashed projection the worker returned, and later the grounded answer Electron
main produced from it. `attempt_id` and `dispatch_id` are UNIQUE, so one
approval funds at most one stored observation.

The observation columns are immutable once written; only the answer columns may
be filled, exactly once. A trigger enforces both, so a later write cannot
quietly change the evidence an answer was grounded in.

No cookie, header, storage state, DOM, screenshot or credential column exists.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ANSWER_STATUSES = ("answered", "not_found", "ambiguous", "not_verified")

_IMMUTABILITY_FUNCTION = """
CREATE FUNCTION page_observations_enforce_immutable_evidence() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.task_id IS DISTINCT FROM OLD.task_id
        OR NEW.action_id IS DISTINCT FROM OLD.action_id
        OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
        OR NEW.dispatch_id IS DISTINCT FROM OLD.dispatch_id
        OR NEW.worker_generation IS DISTINCT FROM OLD.worker_generation
        OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
        OR NEW.provenance IS DISTINCT FROM OLD.provenance
        OR NEW.requested_url IS DISTINCT FROM OLD.requested_url
        OR NEW.final_url IS DISTINCT FROM OLD.final_url
        OR NEW.title IS DISTINCT FROM OLD.title
        OR NEW.document_epoch IS DISTINCT FROM OLD.document_epoch
        OR NEW.settled IS DISTINCT FROM OLD.settled
        OR NEW.truncated IS DISTINCT FROM OLD.truncated
        OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
        OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
        OR NEW.projection IS DISTINCT FROM OLD.projection
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'page observation % is immutable evidence', OLD.id
            USING ERRCODE = '23514';
    END IF;
    IF OLD.answered_at IS NOT NULL THEN
        RAISE EXCEPTION
            'page observation % already has a recorded answer', OLD.id
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_IMMUTABILITY_TRIGGER = """
CREATE TRIGGER page_observations_immutable_evidence
    BEFORE UPDATE ON page_observations
    FOR EACH ROW EXECUTE FUNCTION page_observations_enforce_immutable_evidence()
"""


def upgrade() -> None:
    statuses = ", ".join(f"'{status}'" for status in _ANSWER_STATUSES)
    op.create_table(
        "page_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("dispatch_id", sa.Uuid(), nullable=False),
        sa.Column("worker_generation", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column("requested_url", sa.String(length=2048), nullable=False),
        sa.Column("final_url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("document_epoch", sa.Integer(), nullable=False),
        sa.Column("settled", sa.Boolean(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("projection", postgresql.JSONB(), nullable=False),
        sa.Column("answer_status", sa.String(length=16), nullable=True),
        sa.Column("answer", postgresql.JSONB(), nullable=True),
        sa.Column("answer_provider", sa.String(length=16), nullable=True),
        sa.Column("answer_model", sa.String(length=64), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_observations")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_page_observations_task_id_tasks"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_id"], ["actions.id"],
            name=op.f("fk_page_observations_action_id_actions"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["action_attempts.id"],
            name=op.f("fk_page_observations_attempt_id_action_attempts"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"], ["browser_dispatches.id"],
            name=op.f("fk_page_observations_dispatch_id_browser_dispatches"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_generation"], ["browser_worker_generations.id"],
            name=op.f("fk_page_observations_worker_generation_browser_worker_generations"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("attempt_id", name=op.f("uq_page_observations_attempt_id")),
        sa.UniqueConstraint("dispatch_id", name=op.f("uq_page_observations_dispatch_id")),
        sa.CheckConstraint("schema_version = 1", name=op.f("ck_page_observations_schema_version")),
        sa.CheckConstraint(
            "provenance = 'untrusted_environment'", name=op.f("ck_page_observations_provenance")
        ),
        sa.CheckConstraint("document_epoch >= 1", name=op.f("ck_page_observations_document_epoch_positive")),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_page_observations_content_hash_format")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(projection) = 'object'", name=op.f("ck_page_observations_projection_is_object")
        ),
        sa.CheckConstraint(
            f"answer_status IS NULL OR answer_status IN ({statuses})",
            name=op.f("ck_page_observations_answer_status"),
        ),
        sa.CheckConstraint(
            "(answered_at IS NULL) = (answer IS NULL) AND (answered_at IS NULL) = (answer_status IS NULL)",
            name=op.f("ck_page_observations_answer_complete"),
        ),
        sa.CheckConstraint(
            "answer IS NULL OR jsonb_typeof(answer) = 'object'",
            name=op.f("ck_page_observations_answer_is_object"),
        ),
    )
    op.create_index(
        "ix_page_observations_task_id_created_at", "page_observations", ["task_id", "created_at"]
    )
    op.create_index("ix_page_observations_action_id", "page_observations", ["action_id"])
    op.execute(_IMMUTABILITY_FUNCTION)
    op.execute(_IMMUTABILITY_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER page_observations_immutable_evidence ON page_observations")
    op.execute("DROP FUNCTION page_observations_enforce_immutable_evidence()")
    op.drop_index("ix_page_observations_action_id", table_name="page_observations")
    op.drop_index("ix_page_observations_task_id_created_at", table_name="page_observations")
    op.drop_table("page_observations")
