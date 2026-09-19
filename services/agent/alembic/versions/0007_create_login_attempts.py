"""Manual login and human takeover for Milestone 8a S2.

Forward-only, additive. One new table:

* `login_attempts` -- a bounded interval in which the human, not Lumi, owned
  the browser. It is not an authorization: there is no `kind`, no `scope`,
  nothing a step could consume against it. It records only that the interval
  existed and how it ended (`OPEN`, `UNCONFIRMED`, `COMPLETED`, `CANCELLED`,
  `EXPIRED`, `INTERRUPTED`), so a crash mid-login can never be read back as a
  successful sign-in -- the profile's `status` column, unchanged by this
  migration, is the only place an authentication verdict lives.

Nothing existing changes. `browser_profiles`, `task_grants`,
`step_authorizations`, `action_attempts` and `browser_dispatches` keep their
Milestone 8a S1 shape exactly. A takeover existing does not widen any
authority that existed before it.

What this migration deliberately does **not** add: any column for page text,
a URL, a credential signal or a raw account identity. Those never become
durable state.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOGIN_ATTEMPT_STATUSES = ("OPEN", "UNCONFIRMED", "COMPLETED", "CANCELLED", "EXPIRED", "INTERRUPTED")
_OPEN_LOGIN_ATTEMPT_STATUSES = ("OPEN", "UNCONFIRMED")


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_generation", sa.Uuid(), nullable=False),
        sa.Column("worker_generation", sa.Uuid(), nullable=True),
        sa.Column("profile_revision", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_attempts")),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["browser_profiles.id"],
            name=op.f("fk_login_attempts_profile_id_browser_profiles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_generation"],
            ["runtime_generations.id"],
            name=op.f("fk_login_attempts_runtime_generation_runtime_generations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_generation"],
            ["browser_worker_generations.id"],
            name=op.f("fk_login_attempts_worker_generation_browser_worker_generations"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"status IN ({_quoted(_LOGIN_ATTEMPT_STATUSES)})", name=op.f("ck_login_attempts_status")
        ),
        sa.CheckConstraint(
            "profile_revision >= 1", name=op.f("ck_login_attempts_profile_revision_positive")
        ),
        sa.CheckConstraint(
            "expires_at > started_at", name=op.f("ck_login_attempts_expires_after_start")
        ),
        sa.CheckConstraint(
            "(completed_at IS NOT NULL) = (status = 'COMPLETED')",
            name=op.f("ck_login_attempts_completed_at_set"),
        ),
        sa.CheckConstraint(
            "(cancelled_at IS NOT NULL) = (status = 'CANCELLED')",
            name=op.f("ck_login_attempts_cancelled_at_set"),
        ),
    )
    op.create_index(
        "uq_login_attempts_profile_id_open",
        "login_attempts",
        ["profile_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({_quoted(_OPEN_LOGIN_ATTEMPT_STATUSES)})"),
    )
    op.create_index(
        "ix_login_attempts_profile_id_started_at",
        "login_attempts",
        ["profile_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_login_attempts_profile_id_started_at", table_name="login_attempts")
    op.drop_index("uq_login_attempts_profile_id_open", table_name="login_attempts")
    op.drop_table("login_attempts")
