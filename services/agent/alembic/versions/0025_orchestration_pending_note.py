"""Milestone 12 S3: a controller-authored note for a still-PENDING/AWAITING_APPROVAL step.

`orchestration_steps.result_summary` is, by 0021's own `summary_only_when_resolved` CHECK, a property of a
*resolved* step (`SUCCEEDED`/`FAILED`) only -- correct for every capability composed before this slice, none
of which had anything honest to say about a step that was still unresolved. `manual_handoff_required`
(`account_read`'s own linked task pausing for `login_required`/`account_changed`/`account_identity_unknown`/
`left_site_scope`) is the first case where the orchestration DOES have something bounded and controller-
authored to say -- a safe instruction ("sign in ... then choose Continue") -- while the step is still
unresolved, and that content must never be confused with a step's own *result*.

`pending_note` is the symmetric field: nullable, bounded (matching `result_summary`'s own 600-char limit),
and only ever set while `status` is `PENDING` or `AWAITING_APPROVAL`. A resolved step never carries a
`pending_note` (cleared the moment it settles, mirroring `result_summary`'s own inverse rule) -- the two
columns are mutually exclusive by construction, never just by convention.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orchestration_steps", sa.Column("pending_note", sa.String(length=600), nullable=True))
    op.create_check_constraint(
        op.f("ck_orchestration_steps_note_only_when_pending"),
        "orchestration_steps",
        "pending_note IS NULL OR status IN ('PENDING', 'AWAITING_APPROVAL')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_orchestration_steps_note_only_when_pending"), "orchestration_steps", type_="check")
    op.drop_column("orchestration_steps", "pending_note")
