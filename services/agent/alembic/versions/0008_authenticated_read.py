"""Authenticated account reading for Milestone 8a S3.

Forward-only, and additive to what S1 and S2 built. It adds exactly what S3
needs and nothing for S4-S6 (no element inventory, form draft, protected value
or freeze column):

* `task_grants` learns one new kind, `authenticated_read`, and two nullable
  bindings: `profile_id` (an FK to `browser_profiles`) and
  `profile_revoke_epoch`, the value the profile's `revoke_epoch` had when the
  scope was shown. Both are null for public research and both are required for
  an authenticated read, enforced by a CHECK, so a grant can never be half
  bound. The immutability trigger is replaced so neither binding can be edited
  after insert, exactly like `scope` and `scope_digest`.
* `authenticated_observations` -- bounded, hashed, **redacted** evidence from
  an account page, written once and never edited. It is a separate table from
  `research_observations` on purpose, and `classification` is a CHECK that can
  only ever say `account_private`, so no query, join or export of public
  research evidence can mix the two by accident. There is no URL column: an
  authenticated link can itself be a capability (`?invite=`, a signed URL), so
  the address a ref means is never persisted.
* `authenticated_answers` -- the one grounded answer an authenticated task ends
  with. Never a `research_answers` row. It carries its `classification` and its
  `profile_id`, and it is excluded from public context and episodic memory by
  the code that reads it, not by a naming convention.

Deleting evidence: both evidence tables reference `browser_profiles` and
`tasks` with `ON DELETE CASCADE`, so they cannot outlive either parent. A
profile is soft-deleted (its row stays, as S1 decided), so the deletion path
also removes its evidence explicitly; see `BrowserProfileService.delete_profile`.

What this migration deliberately does **not** add: a column for a URL, a
cookie, a token, page HTML, a screenshot, or a raw account identity.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


#: `scope`, `scope_digest`, `kind`, `policy_version` and now the two profile
#: bindings are what the user confirmed. Nothing may edit them afterwards.
_GRANT_IMMUTABILITY_FUNCTION = """
CREATE OR REPLACE FUNCTION task_grants_enforce_immutable_scope() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.task_id IS DISTINCT FROM OLD.task_id
        OR NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
        OR NEW.scope IS DISTINCT FROM OLD.scope
        OR NEW.scope_digest IS DISTINCT FROM OLD.scope_digest
        OR NEW.profile_id IS DISTINCT FROM OLD.profile_id
        OR NEW.profile_revoke_epoch IS DISTINCT FROM OLD.profile_revoke_epoch
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

_GRANT_IMMUTABILITY_FUNCTION_S2 = """
CREATE OR REPLACE FUNCTION task_grants_enforce_immutable_scope() RETURNS trigger AS $$
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

#: Evidence is written once. An answer's grounding must mean the same later.
_OBSERVATION_IMMUTABILITY_FUNCTION = """
CREATE FUNCTION authenticated_observations_enforce_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'authenticated observation % is immutable evidence', OLD.id
        USING ERRCODE = '23514';
END;
$$ LANGUAGE plpgsql
"""

_OBSERVATION_IMMUTABILITY_TRIGGER = """
CREATE TRIGGER authenticated_observations_immutable
    BEFORE UPDATE ON authenticated_observations
    FOR EACH ROW EXECUTE FUNCTION authenticated_observations_enforce_immutable()
"""


def upgrade() -> None:
    # --- task grants learn the authenticated kind and its profile binding ----
    op.drop_constraint("kind", "task_grants", type_="check")
    op.create_check_constraint(
        "kind", "task_grants", "kind IN ('public_research', 'authenticated_read')"
    )
    op.add_column("task_grants", sa.Column("profile_id", sa.Uuid(), nullable=True))
    op.add_column("task_grants", sa.Column("profile_revoke_epoch", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_task_grants_profile_id_browser_profiles"),
        "task_grants",
        "browser_profiles",
        ["profile_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "profile_binding",
        "task_grants",
        "((kind = 'authenticated_read') = (profile_id IS NOT NULL)) "
        "AND ((profile_id IS NULL) = (profile_revoke_epoch IS NULL)) "
        "AND (profile_revoke_epoch IS NULL OR profile_revoke_epoch >= 0)",
    )
    op.create_index("ix_task_grants_profile_id", "task_grants", ["profile_id"])
    op.execute(_GRANT_IMMUTABILITY_FUNCTION)

    # --- account-private evidence -------------------------------------------
    op.create_table(
        "authenticated_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("dispatch_id", sa.Uuid(), nullable=True),
        sa.Column("worker_generation", sa.Uuid(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("tab", sa.String(length=4), nullable=True),
        sa.Column("document_epoch", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("settled", sa.Boolean(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("projection", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_authenticated_observations")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_authenticated_observations_task_id_tasks"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["task_grants.id"],
            name=op.f("fk_authenticated_observations_grant_id_task_grants"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["browser_profiles.id"],
            name=op.f("fk_authenticated_observations_profile_id_browser_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["action_id"], ["actions.id"],
            name=op.f("fk_authenticated_observations_action_id_actions"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["action_attempts.id"],
            name=op.f("fk_authenticated_observations_attempt_id_action_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"], ["browser_dispatches.id"],
            name=op.f("fk_authenticated_observations_dispatch_id_browser_dispatches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_generation"], ["browser_worker_generations.id"],
            name=op.f(
                "fk_authenticated_observations_worker_generation_browser_worker_generations"
            ),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("attempt_id", name=op.f("uq_authenticated_observations_attempt_id")),
        sa.UniqueConstraint(
            "task_id", "sequence", name=op.f("uq_authenticated_observations_task_id_sequence")
        ),
        sa.CheckConstraint(
            "schema_version = 1", name=op.f("ck_authenticated_observations_schema_version")
        ),
        sa.CheckConstraint(
            "classification = 'account_private'",
            name=op.f("ck_authenticated_observations_classification"),
        ),
        sa.CheckConstraint(
            "provenance = 'untrusted_environment'",
            name=op.f("ck_authenticated_observations_provenance"),
        ),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_authenticated_observations_sequence_positive")),
        sa.CheckConstraint(
            "document_epoch >= 1", name=op.f("ck_authenticated_observations_document_epoch_positive")
        ),
        sa.CheckConstraint(
            "kind IN ('page', 'tab_state')", name=op.f("ck_authenticated_observations_kind")
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_authenticated_observations_content_hash_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(projection) = 'object'",
            name=op.f("ck_authenticated_observations_projection_is_object"),
        ),
    )
    op.create_index(
        "ix_authenticated_observations_task_id_sequence",
        "authenticated_observations",
        ["task_id", "sequence"],
    )
    op.create_index(
        "ix_authenticated_observations_profile_id", "authenticated_observations", ["profile_id"]
    )
    op.execute(_OBSERVATION_IMMUTABILITY_FUNCTION)
    op.execute(_OBSERVATION_IMMUTABILITY_TRIGGER)

    op.create_table(
        "authenticated_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_authenticated_answers")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_authenticated_answers_task_id_tasks"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["task_grants.id"],
            name=op.f("fk_authenticated_answers_grant_id_task_grants"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["browser_profiles.id"],
            name=op.f("fk_authenticated_answers_profile_id_browser_profiles"), ondelete="CASCADE",
        ),
        sa.UniqueConstraint("task_id", name=op.f("uq_authenticated_answers_task_id")),
        sa.CheckConstraint(
            "classification = 'account_private'",
            name=op.f("ck_authenticated_answers_classification"),
        ),
        sa.CheckConstraint(
            f"status IN ({_quoted(_ANSWER_STATUSES)})", name=op.f("ck_authenticated_answers_status")
        ),
        sa.CheckConstraint(
            f"stop_reason IN ({_quoted(_STOP_REASONS)})",
            name=op.f("ck_authenticated_answers_stop_reason"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(answer) = 'object'", name=op.f("ck_authenticated_answers_answer_is_object")
        ),
        sa.CheckConstraint(
            "steps_used >= 0 AND observations_used >= 0 AND planner_calls >= 0",
            name=op.f("ck_authenticated_answers_counters"),
        ),
    )
    op.create_index("ix_authenticated_answers_profile_id", "authenticated_answers", ["profile_id"])


def downgrade() -> None:
    op.drop_index("ix_authenticated_answers_profile_id", table_name="authenticated_answers")
    op.drop_table("authenticated_answers")
    op.execute(
        "DROP TRIGGER authenticated_observations_immutable ON authenticated_observations"
    )
    op.execute("DROP FUNCTION authenticated_observations_enforce_immutable()")
    op.drop_index("ix_authenticated_observations_profile_id", table_name="authenticated_observations")
    op.drop_index(
        "ix_authenticated_observations_task_id_sequence", table_name="authenticated_observations"
    )
    op.drop_table("authenticated_observations")
    # Grants of the new kind cannot survive a downgrade past the constraint
    # that admits them, so they are removed first: `step_authorizations`
    # reference a grant, and both are Milestone 8a S3 authority.
    op.execute(
        "DELETE FROM step_authorizations WHERE grant_id IN "
        "(SELECT id FROM task_grants WHERE kind = 'authenticated_read')"
    )
    op.execute("DELETE FROM task_grants WHERE kind = 'authenticated_read'")
    op.execute(_GRANT_IMMUTABILITY_FUNCTION_S2)
    op.drop_index("ix_task_grants_profile_id", table_name="task_grants")
    op.drop_constraint("profile_binding", "task_grants", type_="check")
    op.drop_constraint(
        op.f("fk_task_grants_profile_id_browser_profiles"), "task_grants", type_="foreignkey"
    )
    op.drop_column("task_grants", "profile_revoke_epoch")
    op.drop_column("task_grants", "profile_id")
    op.drop_constraint("kind", "task_grants", type_="check")
    op.create_check_constraint("kind", "task_grants", "kind = 'public_research'")
