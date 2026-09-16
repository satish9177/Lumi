"""Create tasks and task_events.

Revision ID: 0001
Revises:
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy: migrations must not change when the application enum does.
_STATUSES = (
    "CREATED",
    "PLANNING",
    "READY",
    "EXECUTING",
    "VERIFYING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "PAUSED",
)


def upgrade() -> None:
    statuses = ", ".join(f"'{status}'" for status in _STATUSES)
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("last_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.CheckConstraint(f"status IN ({statuses})", name=op.f("ck_tasks_status")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_tasks_revision_positive")),
        sa.CheckConstraint(
            "last_event_sequence >= 1", name=op.f("ck_tasks_last_event_sequence_positive")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(request) = 'object'", name=op.f("ck_tasks_request_is_object")
        ),
    )
    op.create_table(
        "task_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("task_revision", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_events")),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_task_events_task_id_tasks"),
            ondelete="RESTRICT",
        ),
        # Also the index that serves ordered per-task event reads.
        sa.UniqueConstraint("task_id", "sequence", name=op.f("uq_task_events_task_id_sequence")),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_task_events_sequence_positive")),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name=op.f("ck_task_events_payload_is_object")
        ),
    )


def downgrade() -> None:
    op.drop_table("task_events")
    op.drop_table("tasks")
