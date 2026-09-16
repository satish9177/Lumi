"""Record which browser worker did what, and make one dispatch per attempt a fact.

`browser_worker_generations` is the worker's equivalent of `runtime_generations`:
one row per worker process, owned by the runtime generation that registered it.
It is how a result from a worker that no longer exists is recognised and refused.

`browser_dispatches` is the join between an execution attempt and the browser
work done for it. The unique constraint on `attempt_id` is the load-bearing part:
combined with Milestone 2's "one unfinished attempt per action" and "one approval
funds one attempt", it makes a second real browser submission for one approved
action impossible to write down, not merely unlikely.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies: a migration must not change when an application enum does.
_DISPATCH_STATUSES = (
    "DISPATCHED",
    "OK",
    "CHANGED_RESOURCE",
    "RESOURCE_UNAVAILABLE",
    "FAILED_BEFORE_EFFECT",
    "OUTCOME_UNKNOWN",
)


def upgrade() -> None:
    op.create_table(
        "browser_worker_generations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("runtime_generation", sa.Uuid(), nullable=False),
        sa.Column("worker_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_worker_generations")),
        sa.ForeignKeyConstraint(
            ["runtime_generation"],
            ["runtime_generations.id"],
            name=op.f(
                "fk_browser_worker_generations_runtime_generation_runtime_generations"
            ),
            ondelete="RESTRICT",
        ),
    )

    statuses = ", ".join(f"'{status}'" for status in _DISPATCH_STATUSES)
    op.create_table(
        "browser_dispatches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        # Null for a read-only reconciliation lookup, which is not an execution
        # attempt and must never be recorded as one.
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("worker_generation", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("site", sa.String(length=64), nullable=False),
        sa.Column("effect", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("submitted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("observation_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_dispatches")),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_browser_dispatches_action_id_actions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["action_attempts.id"],
            name=op.f("fk_browser_dispatches_attempt_id_action_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_generation"],
            ["browser_worker_generations.id"],
            name=op.f(
                "fk_browser_dispatches_worker_generation_browser_worker_generations"
            ),
            ondelete="RESTRICT",
        ),
        # One execution attempt dispatches browser work exactly once.
        sa.UniqueConstraint("attempt_id", name=op.f("uq_browser_dispatches_attempt_id")),
        sa.CheckConstraint(f"status IN ({statuses})", name=op.f("ck_browser_dispatches_status")),
        sa.CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'",
            name=op.f("ck_browser_dispatches_result_is_object"),
        ),
        sa.CheckConstraint(
            "(finished_at IS NULL) = (status = 'DISPATCHED')",
            name=op.f("ck_browser_dispatches_finished_with_status"),
        ),
        # A consequential dispatch is only ever made on behalf of an attempt.
        sa.CheckConstraint(
            "effect <> 'CONSEQUENTIAL' OR attempt_id IS NOT NULL",
            name=op.f("ck_browser_dispatches_consequential_needs_attempt"),
        ),
    )
    op.create_index(
        "ix_browser_dispatches_action_id_started_at",
        "browser_dispatches",
        ["action_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_browser_dispatches_action_id_started_at", table_name="browser_dispatches"
    )
    op.drop_table("browser_dispatches")
    op.drop_table("browser_worker_generations")
