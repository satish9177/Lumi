"""Durable read-only task orchestration, Milestone 11 S2.

* `orchestrations`: one durable graph per general request. It holds no authority of its own -- creating one
  grants nothing, and it never becomes a second action ledger.
* `orchestration_steps`: one row per capability choice, in sequence. A step either links an EXISTING child
  task another service already owns (`public_research` today; more land as later slices compose them), or
  is resolved synchronously by the caller with no new task at all (`project_status`, a pure read). There is
  no path, URL, command or capability-invented ref column on either table: `capability_id` is drawn from the
  closed catalog `src/shared/agent-capabilities.ts` already defines (Milestone 11 S1), and `child_task_id`
  only ever names a task this runtime already created through that capability's own normal boundary.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The full closed capability catalog, spelled identically to
#: `src/shared/agent-capabilities.ts`'s `AGENT_CAPABILITY_IDS` (pinned by a Python-side test, since the two
#: languages cannot share a source file). A step may only ever be tagged with a real catalog id, even before
#: a later slice composes it -- see `app/domain/orchestration.py`'s `COMPOSED_CAPABILITY_IDS` for which of
#: these this runtime can actually execute today.
_CAPABILITY_IDS = (
    "public_research", "inspect_public_page", "account_read", "document_read", "document_compare",
    "download_document", "place_downloaded_file", "desktop_observe", "desktop_reason", "desktop_safe_action",
    "launch_registered_app", "project_status", "project_start", "project_stop", "form_prepare",
    "workflow_prepare",
)
_CAPABILITY_CK = "capability_id IN (" + ", ".join(f"'{item}'" for item in _CAPABILITY_IDS) + ")"


def upgrade() -> None:
    op.create_table(
        "orchestrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("objective", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("pause_reason", sa.String(length=32), nullable=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("step_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("child_task_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("planner_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orchestrations")),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'PAUSED', 'SUCCEEDED', 'FAILED', 'STOPPED')", name=op.f("ck_orchestrations_status")
        ),
        sa.CheckConstraint("(status = 'PAUSED') = (pause_reason IS NOT NULL)", name=op.f("ck_orchestrations_pause_reason_set")),
        sa.CheckConstraint(
            "pause_reason IS NULL OR pause_reason IN "
            "('approval_required', 'budget_exhausted', 'loop_detected', 'capability_unavailable')",
            name=op.f("ck_orchestrations_pause_reason_closed"),
        ),
        sa.CheckConstraint(
            "(status = 'STOPPED') = (stopped_at IS NOT NULL)", name=op.f("ck_orchestrations_stopped_at_set")
        ),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_orchestrations_expires_after_creation")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_orchestrations_revision_positive")),
        sa.CheckConstraint("step_count >= 0 AND child_task_count >= 0 AND planner_calls >= 0", name=op.f("ck_orchestrations_counts_nonnegative")),
    )

    op.create_table(
        "orchestration_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("orchestration_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("capability_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        #: Set only for a task-backed capability; NULL for a synchronous one (a pure read with no new task).
        sa.Column("child_task_id", sa.Uuid(), nullable=True),
        sa.Column("result_handle", sa.String(length=40), nullable=True),
        sa.Column("result_summary", sa.String(length=600), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orchestration_steps")),
        sa.ForeignKeyConstraint(
            ["orchestration_id"], ["orchestrations.id"], name=op.f("fk_orchestration_steps_orchestration_id_orchestrations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["child_task_id"], ["tasks.id"], name=op.f("fk_orchestration_steps_child_task_id_tasks"), ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("orchestration_id", "sequence", name="uq_orchestration_steps_sequence"),
        #: A task belongs to at most one orchestration step, exactly like `workflow_steps`.
        sa.UniqueConstraint("child_task_id", name="uq_orchestration_steps_child_task_id"),
        sa.CheckConstraint(_CAPABILITY_CK, name=op.f("ck_orchestration_steps_capability_id")),
        sa.CheckConstraint(
            "status IN ('PENDING', 'AWAITING_APPROVAL', 'SUCCEEDED', 'FAILED')", name=op.f("ck_orchestration_steps_status")
        ),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_orchestration_steps_sequence_positive")),
        #: A result handle -- a citable reference a later planner call may see -- exists exactly when the
        #: step actually produced a result. A failed step has nothing to cite.
        sa.CheckConstraint(
            "(status = 'SUCCEEDED') = (result_handle IS NOT NULL)", name=op.f("ck_orchestration_steps_handle_iff_succeeded")
        ),
        #: A summary (a handle's text, or a failure explanation) exists only once the step is resolved.
        sa.CheckConstraint(
            "result_summary IS NULL OR status IN ('SUCCEEDED', 'FAILED')",
            name=op.f("ck_orchestration_steps_summary_only_when_resolved"),
        ),
    )
    op.create_index("ix_orchestration_steps_orchestration_id", "orchestration_steps", ["orchestration_id"])


def downgrade() -> None:
    op.drop_index("ix_orchestration_steps_orchestration_id", table_name="orchestration_steps")
    op.drop_table("orchestration_steps")
    op.drop_table("orchestrations")
