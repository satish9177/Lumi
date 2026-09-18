"""Scoped research authorization for Milestone 7b public web research.

Forward-only, and additive to the Milestone 2 action ledger rather than beside
it. Five new tables plus one change to an existing one:

* `task_grants` -- one reusable, bounded scope per research task, created by a
  trusted renderer click. Its `scope` is immutable after insert.
* `step_authorizations` -- one single-use authorization per research step,
  minted by the runtime only after checking the step against its grant.
* `research_sessions` -- the task-owned public browser context, bound to the
  worker generation that created it.
* `research_observations` -- bounded, hashed, immutable observations.
* `research_answers` -- the one grounded answer a research task ends with.

`action_attempts.approval_id` becomes nullable, and a new
`step_authorization_id` joins it under a CHECK that exactly one of the two is
present. Existing rows all have an approval, so the constraint holds for them
unchanged and every Milestone 2-7a invariant (one unfinished attempt per
action, one approval per attempt) is preserved.

`actions.status` gains `AUTHORIZED`: a scoped authorization is deliberately not
spelled `APPROVED`, because the user confirmed a scope, not that exact step.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTION_STATUSES = (
    "PROPOSED",
    "WAITING_APPROVAL",
    "APPROVED",
    "AUTHORIZED",
    "REJECTED",
    "EXECUTING",
    "SUCCEEDED",
    "FAILED",
    "OUTCOME_UNKNOWN",
    "RECONCILING",
)
_ACTION_STATUSES_BEFORE = tuple(status for status in _ACTION_STATUSES if status != "AUTHORIZED")
_GRANT_STATUSES = ("PENDING", "ACTIVE", "REVOKED", "EXPIRED", "COMPLETED")
_SESSION_STATUSES = ("OPEN", "CLOSED", "STALE")
_ANSWER_STATUSES = ("answered", "partial", "not_found", "not_verified")
_STOP_REASONS = (
    "goal_reached",
    "no_evidence",
    "budget_exhausted",
    "blocked",
    "planner_failed",
    "user_stopped",
    "outside_scope",
)

#: A confirmed scope is what the user read on the card. Nothing may edit it
#: afterwards -- a widened scope would authorise steps nobody saw.
_GRANT_IMMUTABILITY_FUNCTION = """
CREATE FUNCTION task_grants_enforce_immutable_scope() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.task_id IS DISTINCT FROM OLD.task_id
        OR NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
        OR NEW.scope IS DISTINCT FROM OLD.scope
        OR NEW.scope_digest IS DISTINCT FROM OLD.scope_digest
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'task grant % holds an immutable scope', OLD.id
            USING ERRCODE = '23514';
    END IF;
    IF OLD.status IN ('REVOKED', 'EXPIRED', 'COMPLETED')
        AND NEW.status IS DISTINCT FROM OLD.status
    THEN
        RAISE EXCEPTION
            'task grant % is closed and cannot be reactivated', OLD.id
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_GRANT_IMMUTABILITY_TRIGGER = """
CREATE TRIGGER task_grants_immutable_scope
    BEFORE UPDATE ON task_grants
    FOR EACH ROW EXECUTE FUNCTION task_grants_enforce_immutable_scope()
"""

#: Evidence, like a Milestone 7a page observation, is written once and never
#: edited: an answer's grounding must mean the same thing later.
_OBSERVATION_IMMUTABILITY_FUNCTION = """
CREATE FUNCTION research_observations_enforce_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'research observation % is immutable evidence', OLD.id
        USING ERRCODE = '23514';
END;
$$ LANGUAGE plpgsql
"""

_OBSERVATION_IMMUTABILITY_TRIGGER = """
CREATE TRIGGER research_observations_immutable
    BEFORE UPDATE ON research_observations
    FOR EACH ROW EXECUTE FUNCTION research_observations_enforce_immutable()
"""

#: A consumed step authorization never returns. Being single-use is what stops
#: a duplicated or replayed planner request from executing a step twice.
_AUTHORIZATION_FUNCTION = """
CREATE FUNCTION step_authorizations_enforce_single_use() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.grant_id IS DISTINCT FROM OLD.grant_id
        OR NEW.grant_revision IS DISTINCT FROM OLD.grant_revision
        OR NEW.task_id IS DISTINCT FROM OLD.task_id
        OR NEW.action_id IS DISTINCT FROM OLD.action_id
        OR NEW.action_revision IS DISTINCT FROM OLD.action_revision
        OR NEW.proposal_digest IS DISTINCT FROM OLD.proposal_digest
        OR NEW.scope_digest IS DISTINCT FROM OLD.scope_digest
        OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
        OR NEW.runtime_generation IS DISTINCT FROM OLD.runtime_generation
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
    THEN
        RAISE EXCEPTION
            'step authorization % is immutable', OLD.id
            USING ERRCODE = '23514';
    END IF;
    IF OLD.consumed_at IS NOT NULL THEN
        RAISE EXCEPTION
            'step authorization % was already consumed', OLD.id
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_AUTHORIZATION_TRIGGER = """
CREATE TRIGGER step_authorizations_single_use
    BEFORE UPDATE ON step_authorizations
    FOR EACH ROW EXECUTE FUNCTION step_authorizations_enforce_single_use()
"""


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # --- the action ledger learns one new state -----------------------------
    op.drop_constraint("status", "actions", type_="check")
    op.create_check_constraint(
        "status", "actions", f"status IN ({_quoted(_ACTION_STATUSES)})"
    )

    # --- task grants --------------------------------------------------------
    op.create_table(
        "task_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.String(length=40), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("scope_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_step_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planner_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_grants")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_task_grants_task_id_tasks"), ondelete="RESTRICT",
        ),
        sa.CheckConstraint(f"status IN ({_quoted(_GRANT_STATUSES)})", name=op.f("ck_task_grants_status")),
        sa.CheckConstraint("kind = 'public_research'", name=op.f("ck_task_grants_kind")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_task_grants_revision_positive")),
        sa.CheckConstraint(
            "scope_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_task_grants_scope_digest_format")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(scope) = 'object'", name=op.f("ck_task_grants_scope_is_object")
        ),
        sa.CheckConstraint("planner_calls >= 0", name=op.f("ck_task_grants_planner_calls_positive")),
        sa.CheckConstraint(
            "(status <> 'PENDING') = (confirmed_at IS NOT NULL)",
            name=op.f("ck_task_grants_confirmed_when_not_pending"),
        ),
        sa.CheckConstraint(
            "status <> 'ACTIVE' OR (expires_at IS NOT NULL AND expires_at > confirmed_at)",
            name=op.f("ck_task_grants_active_has_window"),
        ),
        sa.CheckConstraint(
            "(revoked_at IS NOT NULL) = (status = 'REVOKED')", name=op.f("ck_task_grants_revoked_at_set")
        ),
        sa.CheckConstraint(
            "(completed_at IS NOT NULL) = (status = 'COMPLETED')",
            name=op.f("ck_task_grants_completed_at_set"),
        ),
    )
    op.create_index(
        "uq_task_grants_task_id_open",
        "task_grants",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'ACTIVE')"),
    )
    op.execute(_GRANT_IMMUTABILITY_FUNCTION)
    op.execute(_GRANT_IMMUTABILITY_TRIGGER)

    # --- step authorizations ------------------------------------------------
    op.create_table(
        "step_authorizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("grant_revision", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("action_revision", sa.BigInteger(), nullable=False),
        sa.Column("proposal_digest", sa.String(length=64), nullable=False),
        sa.Column("scope_digest", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=40), nullable=False),
        sa.Column("runtime_generation", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_step_authorizations")),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["task_grants.id"],
            name=op.f("fk_step_authorizations_grant_id_task_grants"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_step_authorizations_task_id_tasks"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_id"], ["actions.id"],
            name=op.f("fk_step_authorizations_action_id_actions"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_generation"], ["runtime_generations.id"],
            name=op.f("fk_step_authorizations_runtime_generation_runtime_generations"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("action_id", name=op.f("uq_step_authorizations_action_id")),
        sa.CheckConstraint(
            "grant_revision >= 1", name=op.f("ck_step_authorizations_grant_revision_positive")
        ),
        sa.CheckConstraint(
            "action_revision >= 1", name=op.f("ck_step_authorizations_action_revision_positive")
        ),
        sa.CheckConstraint(
            "proposal_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_step_authorizations_proposal_digest_format"),
        ),
        sa.CheckConstraint(
            "scope_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_step_authorizations_scope_digest_format"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_step_authorizations_expires_after_creation")
        ),
    )
    op.create_index("ix_step_authorizations_grant_id", "step_authorizations", ["grant_id"])
    op.execute(_AUTHORIZATION_FUNCTION)
    op.execute(_AUTHORIZATION_TRIGGER)

    # --- attempts may now be funded by either authority ---------------------
    op.alter_column("action_attempts", "approval_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("action_attempts", sa.Column("step_authorization_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_action_attempts_step_authorization_id_step_authorizations"),
        "action_attempts",
        "step_authorizations",
        ["step_authorization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        op.f("uq_action_attempts_step_authorization_id"),
        "action_attempts",
        ["step_authorization_id"],
    )
    op.create_check_constraint(
        "one_authorization",
        "action_attempts",
        "(approval_id IS NULL) <> (step_authorization_id IS NULL)",
    )

    # --- the task-owned browser session -------------------------------------
    op.create_table(
        "research_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("worker_generation", sa.Uuid(), nullable=False),
        sa.Column("runtime_generation", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_sessions")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_research_sessions_task_id_tasks"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["task_grants.id"],
            name=op.f("fk_research_sessions_grant_id_task_grants"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_generation"], ["browser_worker_generations.id"],
            name=op.f("fk_research_sessions_worker_generation_browser_worker_generations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_generation"], ["runtime_generations.id"],
            name=op.f("fk_research_sessions_runtime_generation_runtime_generations"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"status IN ({_quoted(_SESSION_STATUSES)})", name=op.f("ck_research_sessions_status")
        ),
        sa.CheckConstraint(
            "(closed_at IS NULL) = (status = 'OPEN')",
            name=op.f("ck_research_sessions_closed_with_status"),
        ),
    )
    op.create_index("ix_research_sessions_task_id", "research_sessions", ["task_id"])
    op.add_column("browser_dispatches", sa.Column("session_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_browser_dispatches_session_id_research_sessions"),
        "browser_dispatches",
        "research_sessions",
        ["session_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # --- observations and the final answer ----------------------------------
    op.create_table(
        "research_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("dispatch_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("worker_generation", sa.Uuid(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("tab", sa.String(length=4), nullable=True),
        sa.Column("document_epoch", sa.Integer(), nullable=False),
        sa.Column("query", sa.String(length=200), nullable=True),
        sa.Column("requested_url", sa.String(length=2048), nullable=True),
        sa.Column("final_url", sa.String(length=2048), nullable=True),
        sa.Column("final_host", sa.String(length=253), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("settled", sa.Boolean(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("projection", postgresql.JSONB(), nullable=False),
        sa.Column("targets", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_observations")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_research_observations_task_id_tasks"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["task_grants.id"],
            name=op.f("fk_research_observations_grant_id_task_grants"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_id"], ["actions.id"],
            name=op.f("fk_research_observations_action_id_actions"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["action_attempts.id"],
            name=op.f("fk_research_observations_attempt_id_action_attempts"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"], ["browser_dispatches.id"],
            name=op.f("fk_research_observations_dispatch_id_browser_dispatches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["research_sessions.id"],
            name=op.f("fk_research_observations_session_id_research_sessions"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_generation"], ["browser_worker_generations.id"],
            name=op.f("fk_research_observations_worker_generation_browser_worker_generations"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("attempt_id", name=op.f("uq_research_observations_attempt_id")),
        sa.UniqueConstraint(
            "task_id", "sequence", name=op.f("uq_research_observations_task_id_sequence")
        ),
        sa.CheckConstraint("schema_version = 1", name=op.f("ck_research_observations_schema_version")),
        sa.CheckConstraint(
            "provenance = 'untrusted_environment'", name=op.f("ck_research_observations_provenance")
        ),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_research_observations_sequence_positive")),
        sa.CheckConstraint(
            "document_epoch >= 1", name=op.f("ck_research_observations_document_epoch_positive")
        ),
        sa.CheckConstraint(
            "kind IN ('page', 'search_results', 'tab_state')",
            name=op.f("ck_research_observations_kind"),
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_research_observations_content_hash_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(projection) = 'object'",
            name=op.f("ck_research_observations_projection_is_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(targets) = 'object'",
            name=op.f("ck_research_observations_targets_is_object"),
        ),
    )
    op.create_index(
        "ix_research_observations_task_id_sequence",
        "research_observations",
        ["task_id", "sequence"],
    )
    op.execute(_OBSERVATION_IMMUTABILITY_FUNCTION)
    op.execute(_OBSERVATION_IMMUTABILITY_TRIGGER)

    op.create_table(
        "research_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stop_reason", sa.String(length=24), nullable=False),
        sa.Column("answer", postgresql.JSONB(), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("steps_used", sa.Integer(), nullable=False),
        sa.Column("observations_used", sa.Integer(), nullable=False),
        sa.Column("planner_calls", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_answers")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_research_answers_task_id_tasks"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["task_grants.id"],
            name=op.f("fk_research_answers_grant_id_task_grants"), ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("task_id", name=op.f("uq_research_answers_task_id")),
        sa.CheckConstraint(
            f"status IN ({_quoted(_ANSWER_STATUSES)})", name=op.f("ck_research_answers_status")
        ),
        sa.CheckConstraint(
            f"stop_reason IN ({_quoted(_STOP_REASONS)})", name=op.f("ck_research_answers_stop_reason")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(answer) = 'object'", name=op.f("ck_research_answers_answer_is_object")
        ),
        sa.CheckConstraint(
            "steps_used >= 0 AND observations_used >= 0 AND planner_calls >= 0",
            name=op.f("ck_research_answers_counters"),
        ),
    )


def downgrade() -> None:
    op.drop_table("research_answers")
    op.execute("DROP TRIGGER research_observations_immutable ON research_observations")
    op.execute("DROP FUNCTION research_observations_enforce_immutable()")
    op.drop_index("ix_research_observations_task_id_sequence", table_name="research_observations")
    op.drop_table("research_observations")
    op.drop_constraint(
        op.f("fk_browser_dispatches_session_id_research_sessions"),
        "browser_dispatches",
        type_="foreignkey",
    )
    op.drop_column("browser_dispatches", "session_id")
    op.drop_index("ix_research_sessions_task_id", table_name="research_sessions")
    op.drop_table("research_sessions")
    op.drop_constraint("one_authorization", "action_attempts", type_="check")
    op.drop_constraint(
        op.f("uq_action_attempts_step_authorization_id"), "action_attempts", type_="unique"
    )
    op.drop_constraint(
        op.f("fk_action_attempts_step_authorization_id_step_authorizations"),
        "action_attempts",
        type_="foreignkey",
    )
    op.drop_column("action_attempts", "step_authorization_id")
    op.execute("UPDATE action_attempts SET approval_id = approval_id WHERE approval_id IS NULL")
    op.alter_column("action_attempts", "approval_id", existing_type=sa.Uuid(), nullable=False)
    op.execute("DROP TRIGGER step_authorizations_single_use ON step_authorizations")
    op.execute("DROP FUNCTION step_authorizations_enforce_single_use()")
    op.drop_index("ix_step_authorizations_grant_id", table_name="step_authorizations")
    op.drop_table("step_authorizations")
    op.execute("DROP TRIGGER task_grants_immutable_scope ON task_grants")
    op.execute("DROP FUNCTION task_grants_enforce_immutable_scope()")
    op.drop_index("uq_task_grants_task_id_open", table_name="task_grants")
    op.drop_table("task_grants")
    op.drop_constraint("status", "actions", type_="check")
    op.create_check_constraint(
        "status", "actions", f"status IN ({_quoted(_ACTION_STATUSES_BEFORE)})"
    )
