"""Milestone 9 S3: durable desktop dispatches for focus, semantic scroll and registered-app launch.

`desktop_dispatches` is the desktop counterpart of `browser_dispatches`: the durable record that Lumi
was about to ask the desktop worker for ONE effect, written (and committed) before the worker is
called. `attempt_id` is UNIQUE, so a second dispatch for one execution attempt cannot be written down.
It stores identifiers, opaque refs, digests and closed codes only: no window title, no control text, no
process path, no HWND/PID, no coordinate and never a typed value.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPERATIONS = ("focus_surface", "scroll_control", "launch_app")
_STATUSES = ("DISPATCHED", "OK", "FAILED_BEFORE_EFFECT", "OUTCOME_UNKNOWN")


def upgrade() -> None:
    operations = ", ".join(f"'{value}'" for value in _OPERATIONS)
    statuses = ", ".join(f"'{value}'" for value in _STATUSES)
    op.create_table(
        "desktop_dispatches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("worker_generation", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=24), nullable=False),
        sa.Column("surface_ref", sa.String(length=4), nullable=True),
        sa.Column("surface_epoch", sa.Integer(), nullable=True),
        sa.Column("observation_id", sa.Uuid(), nullable=True),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=True),
        sa.Column("control_ref", sa.String(length=4), nullable=True),
        sa.Column("app_id", sa.String(length=32), nullable=True),
        sa.Column("input_tick", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_desktop_dispatches")),
        sa.ForeignKeyConstraint(
            ["action_id"], ["actions.id"], name=op.f("fk_desktop_dispatches_action_id_actions"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["action_attempts.id"],
            name=op.f("fk_desktop_dispatches_attempt_id_action_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_generation"],
            ["desktop_worker_generations.id"],
            name=op.f("fk_desktop_dispatches_worker_generation_desktop_worker_generations"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("attempt_id", name=op.f("uq_desktop_dispatches_attempt_id")),
        sa.CheckConstraint(f"operation IN ({operations})", name=op.f("ck_desktop_dispatches_operation")),
        sa.CheckConstraint(f"status IN ({statuses})", name=op.f("ck_desktop_dispatches_status")),
        sa.CheckConstraint(
            "(finished_at IS NULL) = (status = 'DISPATCHED')", name=op.f("ck_desktop_dispatches_finished_matches_status")
        ),
        sa.CheckConstraint(
            "surface_ref IS NULL OR surface_ref ~ '^s([1-9]|1[0-6])$'", name=op.f("ck_desktop_dispatches_surface_ref_shape")
        ),
        sa.CheckConstraint(
            "control_ref IS NULL OR control_ref ~ '^u([1-9][0-9]?|1[0-9][0-9]|200)$'",
            name=op.f("ck_desktop_dispatches_control_ref_shape"),
        ),
        sa.CheckConstraint(
            "app_id IS NULL OR app_id ~ '^[a-z][a-z0-9_-]{0,31}$'", name=op.f("ck_desktop_dispatches_app_id_shape")
        ),
        sa.CheckConstraint(
            "snapshot_digest IS NULL OR snapshot_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_desktop_dispatches_digest_format"),
        ),
        sa.CheckConstraint(
            "input_tick >= 0 AND input_tick <= 4294967295", name=op.f("ck_desktop_dispatches_input_tick_range")
        ),
        sa.CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'", name=op.f("ck_desktop_dispatches_result_object")
        ),
        # Each operation carries exactly the identity it needs and nothing it does not.
        sa.CheckConstraint(
            "(operation = 'focus_surface' AND surface_ref IS NOT NULL AND surface_epoch IS NOT NULL "
            "AND control_ref IS NULL AND app_id IS NULL) "
            "OR (operation = 'scroll_control' AND surface_ref IS NOT NULL AND surface_epoch IS NOT NULL "
            "AND observation_id IS NOT NULL AND snapshot_digest IS NOT NULL AND control_ref IS NOT NULL "
            "AND app_id IS NULL) "
            "OR (operation = 'launch_app' AND app_id IS NOT NULL AND surface_ref IS NULL "
            "AND control_ref IS NULL AND observation_id IS NULL)",
            name=op.f("ck_desktop_dispatches_operation_identity"),
        ),
    )
    op.create_index("ix_desktop_dispatches_action_id", "desktop_dispatches", ["action_id"])
    op.create_index(
        "ix_desktop_dispatches_in_flight",
        "desktop_dispatches",
        ["status"],
        postgresql_where=sa.text("status = 'DISPATCHED'"),
    )


def downgrade() -> None:
    op.drop_index("ix_desktop_dispatches_in_flight", table_name="desktop_dispatches")
    op.drop_index("ix_desktop_dispatches_action_id", table_name="desktop_dispatches")
    op.drop_table("desktop_dispatches")
