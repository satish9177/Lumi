"""Windows desktop observation, Milestone 9 S1: `desktop_worker_generations` and `desktop_observations`.

S1 lets Lumi *read* a Windows application semantically and nothing more, so it records
exactly two things:

* `desktop_worker_generations`: the identity of one run of the isolated desktop worker,
  bound to the runtime generation that started it (the same idea as
  `browser_worker_generations`).
* `desktop_observations`: the safe, bounded projection of one observation. `snapshot` is
  the closed `DesktopObservation` schema. There is deliberately no column for a window
  handle, a process id or path, coordinates, an AutomationId or class name, or a
  password; the row is `desktop_private` and no provider-facing record refers to it.

Nothing that could perform an action is stored: no action, input or target-handle table.
Ownership is the worker generation, not a task, because no planner consumes desktop
observations in this slice.

Forward-only in intent. Downgrade drops both tables.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "desktop_worker_generations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("runtime_generation", sa.Uuid(), nullable=False),
        sa.Column("worker_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_desktop_worker_generations")),
        sa.ForeignKeyConstraint(
            ["runtime_generation"],
            ["runtime_generations.id"],
            name="fk_desktop_worker_generations_runtime_generation",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "desktop_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("worker_generation", sa.Uuid(), nullable=False),
        sa.Column("surface_ref", sa.String(length=4), nullable=False),
        sa.Column("surface_epoch", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_desktop_observations")),
        sa.ForeignKeyConstraint(
            ["worker_generation"],
            ["desktop_worker_generations.id"],
            name="fk_desktop_observations_worker_generation",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "classification = 'desktop_private'", name=op.f("ck_desktop_observations_classification")
        ),
        sa.CheckConstraint(
            "surface_ref ~ '^s([1-9]|1[0-6])$'", name=op.f("ck_desktop_observations_surface_ref_shape")
        ),
        sa.CheckConstraint("surface_epoch >= 1", name=op.f("ck_desktop_observations_epoch_positive")),
        sa.CheckConstraint(
            "schema_version >= 1", name=op.f("ck_desktop_observations_schema_version_positive")
        ),
        sa.CheckConstraint(
            "snapshot_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_desktop_observations_digest_format")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'", name=op.f("ck_desktop_observations_snapshot_object")
        ),
    )
    op.create_index("ix_desktop_observations_created_at", "desktop_observations", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_desktop_observations_created_at", table_name="desktop_observations")
    op.drop_table("desktop_observations")
    op.drop_table("desktop_worker_generations")
