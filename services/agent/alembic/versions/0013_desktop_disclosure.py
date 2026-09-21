"""Desktop disclosure, Milestone 9 S2: `desktop_disclosures`, `desktop_answers`, and the grant kind.

S2 changes *who may see* an already-captured desktop snapshot, so it adds only the durable state that
proves a one-shot disclosure and retains its private answer:

* `task_grants.kind` gains `desktop_disclose` (no second authorization framework). The grant binds one
  task to one exact observation id and snapshot digest, one recipient and one expiry, and is consumed
  (`ACTIVE` -> `COMPLETED`) by the same transaction that inserts its disclosure row.
* `desktop_disclosures`: one row per disclosure. `grant_id` and `task_id` are UNIQUE, so one approval
  can never fund two provider calls. `STARTED` -> `SUCCEEDED` | `FAILED` | `OUTCOME_UNKNOWN`.
* `desktop_answers`: the private, grounded answer, `desktop_private`, one per disclosure.

No column stores a window handle, process, coordinate or snapshot, and the disclosure audit row holds
no desktop text. Desktop-derived text that S2 does keep, plainly: the user's typed question (tasks.request),
the window's display label and title inside the grant scope (card only), and the redacted answer with its
redacted evidence quotes (desktop_answers). None of those expires with the raw observation's 24-hour
retention. Nothing here can perform an action on an application.

Downgrade drops both tables and restores the previous grant kinds (it refuses if a `desktop_disclose`
grant exists, rather than deleting an audit record).

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_CK = "kind"
_OLD = "kind IN ('public_research', 'authenticated_read', 'form_prepare')"
_NEW = "kind IN ('public_research', 'authenticated_read', 'form_prepare', 'desktop_disclose')"


def upgrade() -> None:
    op.drop_constraint(_KIND_CK, "task_grants", type_="check")
    op.create_check_constraint(_KIND_CK, "task_grants", _NEW)

    op.create_table(
        "desktop_disclosures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("recipient", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("projection_digest", sa.String(length=64), nullable=False),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("text_bytes", sa.Integer(), nullable=False),
        sa.Column("redaction_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_desktop_disclosures")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_desktop_disclosures_task_id_tasks"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["task_grants.id"],
            name=op.f("fk_desktop_disclosures_grant_id_task_grants"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("grant_id", name="uq_desktop_disclosures_grant_id"),
        sa.UniqueConstraint("task_id", name="uq_desktop_disclosures_task_id"),
        sa.CheckConstraint(
            "status IN ('STARTED', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN')",
            name=op.f("ck_desktop_disclosures_status"),
        ),
        sa.CheckConstraint(
            "snapshot_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_desktop_disclosures_snapshot_digest_format")
        ),
        sa.CheckConstraint(
            "projection_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_desktop_disclosures_projection_digest_format")
        ),
        sa.CheckConstraint(
            "node_count >= 0 AND text_bytes >= 0 AND redaction_count >= 0",
            name=op.f("ck_desktop_disclosures_counts_non_negative"),
        ),
        sa.CheckConstraint(
            "(status = 'STARTED') = (finished_at IS NULL)",
            name=op.f("ck_desktop_disclosures_finished_when_not_started"),
        ),
        sa.CheckConstraint(
            "(status IN ('FAILED', 'OUTCOME_UNKNOWN')) = (error_code IS NOT NULL)",
            name=op.f("ck_desktop_disclosures_error_code_when_not_ok"),
        ),
    )
    op.create_table(
        "desktop_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("disclosure_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("recipient", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("answer", sa.String(length=1200), nullable=True),
        sa.Column("reason", sa.String(length=24), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_desktop_answers")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_desktop_answers_task_id_tasks"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["disclosure_id"],
            ["desktop_disclosures.id"],
            name=op.f("fk_desktop_answers_disclosure_id_desktop_disclosures"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("disclosure_id", name="uq_desktop_answers_disclosure_id"),
        sa.CheckConstraint(
            "classification = 'desktop_private'", name=op.f("ck_desktop_answers_classification")
        ),
        sa.CheckConstraint("kind IN ('answer', 'cannot_answer')", name=op.f("ck_desktop_answers_kind")),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'array'", name=op.f("ck_desktop_answers_evidence_is_array")
        ),
        sa.CheckConstraint(
            "(kind = 'answer') = (answer IS NOT NULL) AND (kind = 'cannot_answer') = (reason IS NOT NULL)",
            name=op.f("ck_desktop_answers_shape_matches_kind"),
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    held = connection.execute(sa.text("SELECT count(*) FROM task_grants WHERE kind = 'desktop_disclose'")).scalar()
    if held:
        raise RuntimeError("refusing to downgrade: desktop disclosure grants exist and are an audit record")
    op.drop_table("desktop_answers")
    op.drop_table("desktop_disclosures")
    op.drop_constraint(_KIND_CK, "task_grants", type_="check")
    op.create_check_constraint(_KIND_CK, "task_grants", _OLD)
