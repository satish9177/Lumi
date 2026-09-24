"""Widen the orchestrations pause-reason vocabulary, Milestone 11 S4.

Adds `manual_handoff_required` (a human must act outside Lumi -- a login, a CAPTCHA, an unsupported
control -- before the orchestration can continue) and `outcome_unknown` (a linked capability's own effect
is unresolved, e.g. a research step interrupted mid-flight or a project run whose own `RunView.phase` is
itself `outcome_unknown`) to the closed set `orchestrations.pause_reason` may hold. Widening a CHECK
constraint validates every existing row, all of which already hold one of the old values, so this stays
inside one transaction.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-24
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_REASONS = ("approval_required", "budget_exhausted", "loop_detected", "capability_unavailable")
_NEW_REASONS = (*_OLD_REASONS, "manual_handoff_required", "outcome_unknown")


def _replace(reasons: tuple[str, ...]) -> None:
    # Written out rather than via op.create_check_constraint so the constraint name is exactly the one
    # 0021 created, not one the naming convention derives a second time from an already-conventional name.
    values = ", ".join(f"'{reason}'" for reason in reasons)
    op.execute("ALTER TABLE orchestrations DROP CONSTRAINT ck_orchestrations_pause_reason_closed")
    op.execute(
        f"ALTER TABLE orchestrations ADD CONSTRAINT ck_orchestrations_pause_reason_closed "
        f"CHECK (pause_reason IS NULL OR pause_reason IN ({values}))"
    )


def upgrade() -> None:
    _replace(_NEW_REASONS)


def downgrade() -> None:
    connection = op.get_bind()
    stuck = connection.execute(
        text("SELECT count(*) FROM orchestrations WHERE pause_reason IN ('manual_handoff_required', 'outcome_unknown')")
    ).scalar()
    if stuck:
        raise RuntimeError(
            "refusing to downgrade: an orchestration is paused with a reason 0021's narrower constraint "
            "cannot hold; resolve or stop it first"
        )
    _replace(_OLD_REASONS)
