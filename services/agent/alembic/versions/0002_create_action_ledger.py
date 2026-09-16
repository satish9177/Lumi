"""Create the durable action ledger: actions, approvals and execution attempts.

Also widens the task status CHECK constraint with WAITING_APPROVAL,
OUTCOME_UNKNOWN and RECONCILING.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies: migrations must not change when the application enums do.
_OLD_TASK_STATUSES = (
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
_NEW_TASK_STATUSES = (
    "CREATED",
    "PLANNING",
    "READY",
    "WAITING_APPROVAL",
    "EXECUTING",
    "VERIFYING",
    "OUTCOME_UNKNOWN",
    "RECONCILING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "PAUSED",
)
_ACTION_STATUSES = (
    "PROPOSED",
    "WAITING_APPROVAL",
    "APPROVED",
    "REJECTED",
    "EXECUTING",
    "SUCCEEDED",
    "FAILED",
    "OUTCOME_UNKNOWN",
    "RECONCILING",
)
_APPROVAL_STATUSES = ("PENDING", "APPROVED", "REJECTED", "CONSUMED")
_ATTEMPT_OUTCOMES = ("SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN")
_RISK_TIERS = ("R0", "R1", "R2", "R3")

_HEX_DIGEST_FORMAT = "proposal_digest ~ '^[0-9a-f]{64}$'"

# An approved proposal must be the proposal that executes. Application code
# never updates these columns, and this makes that a database guarantee rather
# than a convention: a proposal cannot be swapped underneath a live approval.
_IMMUTABILITY_FUNCTION = """
CREATE FUNCTION actions_enforce_immutable_columns() RETURNS trigger AS $$
BEGIN
    IF NEW.task_id IS DISTINCT FROM OLD.task_id
        OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
        OR NEW.tool_name IS DISTINCT FROM OLD.tool_name
        OR NEW.risk_tier IS DISTINCT FROM OLD.risk_tier
        OR NEW.proposal IS DISTINCT FROM OLD.proposal
        OR NEW.proposal_digest IS DISTINCT FROM OLD.proposal_digest
    THEN
        RAISE EXCEPTION
            'action % has an immutable proposal; it cannot be changed after creation', OLD.id
            USING ERRCODE = '23514';
    END IF;
    IF NEW.revision <= OLD.revision THEN
        RAISE EXCEPTION
            'action % revision must increase (% -> %)', OLD.id, OLD.revision, NEW.revision
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_IMMUTABILITY_TRIGGER = """
CREATE TRIGGER actions_immutable_columns
    BEFORE UPDATE ON actions
    FOR EACH ROW EXECUTE FUNCTION actions_enforce_immutable_columns()
"""


def _replace_task_status_constraint(statuses: tuple[str, ...]) -> None:
    # Written out rather than via op.create_check_constraint so the constraint
    # name is exactly the one 0001 created, not one the naming convention
    # derives a second time from an already-conventional name.
    values = ", ".join(f"'{status}'" for status in statuses)
    op.execute("ALTER TABLE tasks DROP CONSTRAINT ck_tasks_status")
    op.execute(f"ALTER TABLE tasks ADD CONSTRAINT ck_tasks_status CHECK (status IN ({values}))")


def upgrade() -> None:
    # Widening a CHECK constraint validates every existing row, all of which
    # already hold one of the old values, so this stays inside one transaction.
    _replace_task_status_constraint(_NEW_TASK_STATUSES)

    op.create_table(
        "runtime_generations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_generations")),
    )

    action_statuses = ", ".join(f"'{status}'" for status in _ACTION_STATUSES)
    risk_tiers = ", ".join(f"'{tier}'" for tier in _RISK_TIERS)
    op.create_table(
        "actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("risk_tier", sa.String(length=8), nullable=False),
        sa.Column("proposal", postgresql.JSONB(), nullable=False),
        sa.Column("proposal_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_actions")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_actions_task_id_tasks"), ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "task_id", "idempotency_key", name=op.f("uq_actions_task_id_idempotency_key")
        ),
        sa.CheckConstraint(f"status IN ({action_statuses})", name=op.f("ck_actions_status")),
        sa.CheckConstraint(f"risk_tier IN ({risk_tiers})", name=op.f("ck_actions_risk_tier")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_actions_revision_positive")),
        sa.CheckConstraint(
            "jsonb_typeof(proposal) = 'object'", name=op.f("ck_actions_proposal_is_object")
        ),
        sa.CheckConstraint(_HEX_DIGEST_FORMAT, name=op.f("ck_actions_proposal_digest_format")),
        sa.CheckConstraint(
            "length(idempotency_key) >= 1", name=op.f("ck_actions_idempotency_key_present")
        ),
    )
    op.create_index("ix_actions_task_id_created_at", "actions", ["task_id", "created_at"])
    # asyncpg prepares each statement, so the function and trigger go separately.
    op.execute(_IMMUTABILITY_FUNCTION)
    op.execute(_IMMUTABILITY_TRIGGER)

    approval_statuses = ", ".join(f"'{status}'" for status in _APPROVAL_STATUSES)
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("action_revision", sa.BigInteger(), nullable=False),
        sa.Column("proposal_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_approvals_action_id_actions"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(f"status IN ({approval_statuses})", name=op.f("ck_approvals_status")),
        sa.CheckConstraint(_HEX_DIGEST_FORMAT, name=op.f("ck_approvals_proposal_digest_format")),
        sa.CheckConstraint(
            "action_revision >= 1", name=op.f("ck_approvals_action_revision_positive")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_approvals_expires_after_creation")
        ),
        sa.CheckConstraint(
            "status NOT IN ('APPROVED', 'CONSUMED') OR approved_at IS NOT NULL",
            name=op.f("ck_approvals_approved_at_set"),
        ),
        sa.CheckConstraint(
            "(rejected_at IS NOT NULL) = (status = 'REJECTED')",
            name=op.f("ck_approvals_rejected_at_set"),
        ),
        sa.CheckConstraint(
            "(consumed_at IS NOT NULL) = (status = 'CONSUMED')",
            name=op.f("ck_approvals_consumed_at_set"),
        ),
    )
    # At most one live approval per action.
    op.create_index(
        "uq_approvals_action_id_open",
        "approvals",
        ["action_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('APPROVED', 'PENDING')"),
    )

    outcomes = ", ".join(f"'{outcome}'" for outcome in _ATTEMPT_OUTCOMES)
    op.create_table(
        "action_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_generation", sa.Uuid(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_attempts")),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_action_attempts_action_id_actions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approvals.id"],
            name=op.f("fk_action_attempts_approval_id_approvals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_generation"],
            ["runtime_generations.id"],
            name=op.f("fk_action_attempts_runtime_generation_runtime_generations"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "action_id", "attempt_number", name=op.f("uq_action_attempts_action_id_attempt_number")
        ),
        # One approval funds at most one attempt.
        sa.UniqueConstraint("approval_id", name=op.f("uq_action_attempts_approval_id")),
        sa.CheckConstraint(
            "attempt_number >= 1", name=op.f("ck_action_attempts_attempt_number_positive")
        ),
        sa.CheckConstraint(
            f"outcome IS NULL OR outcome IN ({outcomes})", name=op.f("ck_action_attempts_outcome")
        ),
        sa.CheckConstraint(
            "(finished_at IS NULL) = (outcome IS NULL)",
            name=op.f("ck_action_attempts_finished_with_outcome"),
        ),
        sa.CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'",
            name=op.f("ck_action_attempts_result_is_object"),
        ),
    )
    # At most one unfinished attempt per action.
    op.create_index(
        "uq_action_attempts_action_id_unfinished",
        "action_attempts",
        ["action_id"],
        unique=True,
        postgresql_where=sa.text("finished_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_action_attempts_action_id_unfinished", table_name="action_attempts")
    op.drop_table("action_attempts")
    op.drop_index("uq_approvals_action_id_open", table_name="approvals")
    op.drop_table("approvals")
    op.execute("DROP TRIGGER actions_immutable_columns ON actions")
    op.execute("DROP FUNCTION actions_enforce_immutable_columns()")
    op.drop_index("ix_actions_task_id_created_at", table_name="actions")
    op.drop_table("actions")
    op.drop_table("runtime_generations")
    # Tasks in a state 0001 did not know about cannot survive the narrower
    # constraint, so park them in PAUSED rather than failing the downgrade.
    op.execute(
        "UPDATE tasks SET status = 'PAUSED', revision = revision + 1, updated_at = now() "
        "WHERE status IN ('WAITING_APPROVAL', 'OUTCOME_UNKNOWN', 'RECONCILING')"
    )
    _replace_task_status_constraint(_OLD_TASK_STATUSES)
