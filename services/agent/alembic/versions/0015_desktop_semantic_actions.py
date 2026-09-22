"""Bounded semantic desktop actions, Milestone 9 S4: value/select/invoke dispatches and the planning grant.

S4 adds exactly two things, both durable state and neither a new authorization framework:

* `desktop_dispatches` (from S3) gains three more `operation` values -- `set_control_value`,
  `select_control`, `invoke_control` -- and the two opaque identity columns they need
  (`value_ref`, `invoke_effect`; `select_control` reuses `control_ref` for the chosen option and adds
  `option_container_ref` for the list/combo it must belong to). The operation-identity constraint is
  widened so each new operation still carries EXACTLY the identity it needs: never a raw value, never a
  coordinate, never a native id.
* `task_grants.kind` gains `desktop_action_plan` (no second authorization framework, exactly as S2's
  `desktop_disclose` was added) and `desktop_action_plans` records one planning-disclosure attempt: one
  approved, redacted, bounded snapshot plus the trusted local value descriptors shown to ONE provider,
  and the ONE closed action it proposed back (opaque refs only -- never a raw value, never free text).
  `grant_id` and `task_id` are UNIQUE, exactly like `desktop_disclosures`: one planning approval can
  never fund two provider calls. A planning disclosure is not execution authority: the action it
  proposes still needs its own separate, exact `DESKTOP_SET_VALUE` / `DESKTOP_SELECT` / `DESKTOP_INVOKE`
  approval on the ordinary action ledger before any effect exists, exactly as S3 already requires for
  focus/scroll/launch.

No column here stores a window handle, process, coordinate, native id or raw typed value. The one place
S4 keeps a raw value at rest is `task_grants.scope` for a `desktop_action_plan` grant -- the same
column S2 already used to hold the window title for card display, immutable after insert by the
trigger `0005` added -- because that value came from the person, not from the desktop, and the trusted
card must still be able to show it back. It is never copied into `desktop_action_plans`,
`desktop_dispatches`, an event or a log.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_CK = "kind"
_OLD_KIND = "kind IN ('public_research', 'authenticated_read', 'form_prepare', 'desktop_disclose')"
_NEW_KIND = (
    "kind IN ('public_research', 'authenticated_read', 'form_prepare', 'desktop_disclose', "
    "'desktop_action_plan')"
)

_OLD_OPERATIONS = ("focus_surface", "scroll_control", "launch_app")
_NEW_OPERATIONS = (
    "focus_surface", "scroll_control", "launch_app",
    "set_control_value", "select_control", "invoke_control",
)
#: Short names: `create_check_constraint`/`drop_constraint` apply the naming convention themselves
#: (unlike `sa.CheckConstraint(..., name=op.f(...))` inside `create_table`, which is already the fully
#: qualified name). These two match the short names `desktop_dispatches` was created with in `0014`.
_DISPATCH_OP_CK = "operation"
_DISPATCH_IDENTITY_CK = "operation_identity"

_OLD_IDENTITY = (
    "(operation = 'focus_surface' AND surface_ref IS NOT NULL AND surface_epoch IS NOT NULL "
    "AND control_ref IS NULL AND app_id IS NULL) "
    "OR (operation = 'scroll_control' AND surface_ref IS NOT NULL AND surface_epoch IS NOT NULL "
    "AND observation_id IS NOT NULL AND snapshot_digest IS NOT NULL AND control_ref IS NOT NULL "
    "AND app_id IS NULL) "
    "OR (operation = 'launch_app' AND app_id IS NOT NULL AND surface_ref IS NULL "
    "AND control_ref IS NULL AND observation_id IS NULL)"
)
_NEW_IDENTITY = (
    "(operation = 'focus_surface' AND surface_ref IS NOT NULL AND surface_epoch IS NOT NULL "
    "AND control_ref IS NULL AND app_id IS NULL AND value_ref IS NULL AND option_container_ref IS NULL "
    "AND invoke_effect IS NULL) "
    "OR (operation = 'scroll_control' AND surface_ref IS NOT NULL AND surface_epoch IS NOT NULL "
    "AND observation_id IS NOT NULL AND snapshot_digest IS NOT NULL AND control_ref IS NOT NULL "
    "AND app_id IS NULL AND value_ref IS NULL AND option_container_ref IS NULL AND invoke_effect IS NULL) "
    "OR (operation = 'launch_app' AND app_id IS NOT NULL AND surface_ref IS NULL "
    "AND control_ref IS NULL AND observation_id IS NULL AND value_ref IS NULL "
    "AND option_container_ref IS NULL AND invoke_effect IS NULL) "
    "OR (operation = 'set_control_value' AND surface_ref IS NOT NULL AND surface_epoch IS NOT NULL "
    "AND observation_id IS NOT NULL AND snapshot_digest IS NOT NULL AND control_ref IS NOT NULL "
    "AND value_ref IS NOT NULL AND app_id IS NULL AND option_container_ref IS NULL "
    "AND invoke_effect IS NULL) "
    "OR (operation = 'select_control' AND surface_ref IS NOT NULL AND surface_epoch IS NOT NULL "
    "AND observation_id IS NOT NULL AND snapshot_digest IS NOT NULL AND control_ref IS NOT NULL "
    "AND option_container_ref IS NOT NULL AND app_id IS NULL AND value_ref IS NULL "
    "AND invoke_effect IS NULL) "
    "OR (operation = 'invoke_control' AND surface_ref IS NOT NULL AND surface_epoch IS NOT NULL "
    "AND observation_id IS NOT NULL AND snapshot_digest IS NOT NULL AND control_ref IS NOT NULL "
    "AND invoke_effect IS NOT NULL AND app_id IS NULL AND value_ref IS NULL "
    "AND option_container_ref IS NULL)"
)


def upgrade() -> None:
    op.drop_constraint(_KIND_CK, "task_grants", type_="check")
    op.create_check_constraint(_KIND_CK, "task_grants", _NEW_KIND)

    op.add_column("desktop_dispatches", sa.Column("value_ref", sa.String(length=4), nullable=True))
    op.add_column(
        "desktop_dispatches", sa.Column("option_container_ref", sa.String(length=4), nullable=True)
    )
    op.add_column("desktop_dispatches", sa.Column("invoke_effect", sa.String(length=32), nullable=True))
    op.drop_constraint(_DISPATCH_IDENTITY_CK, "desktop_dispatches", type_="check")
    op.drop_constraint(_DISPATCH_OP_CK, "desktop_dispatches", type_="check")
    op.create_check_constraint(
        _DISPATCH_OP_CK, "desktop_dispatches",
        "operation IN (" + ", ".join(f"'{value}'" for value in _NEW_OPERATIONS) + ")",
    )
    op.create_check_constraint(_DISPATCH_IDENTITY_CK, "desktop_dispatches", _NEW_IDENTITY)
    op.create_check_constraint(
        op.f("ck_desktop_dispatches_value_ref_shape"),
        "desktop_dispatches",
        "value_ref IS NULL OR value_ref ~ '^v([1-9]|10)$'",
    )
    op.create_check_constraint(
        op.f("ck_desktop_dispatches_option_container_ref_shape"),
        "desktop_dispatches",
        "option_container_ref IS NULL OR option_container_ref ~ '^u([1-9][0-9]?|1[0-9][0-9]|200)$'",
    )
    op.create_check_constraint(
        op.f("ck_desktop_dispatches_invoke_effect_shape"),
        "desktop_dispatches",
        "invoke_effect IS NULL OR invoke_effect IN ('name_toggle')",
    )

    op.create_table(
        "desktop_action_plans",
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
        # The ONE closed action the provider proposed: opaque refs and a discriminator only (never a
        # raw value or free text). NULL until a result is recorded.
        sa.Column("proposed_action", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_desktop_action_plans")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_desktop_action_plans_task_id_tasks"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["task_grants.id"],
            name=op.f("fk_desktop_action_plans_grant_id_task_grants"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("grant_id", name="uq_desktop_action_plans_grant_id"),
        sa.UniqueConstraint("task_id", name="uq_desktop_action_plans_task_id"),
        sa.CheckConstraint(
            "status IN ('STARTED', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN')",
            name=op.f("ck_desktop_action_plans_status"),
        ),
        sa.CheckConstraint(
            "snapshot_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_desktop_action_plans_snapshot_digest_format")
        ),
        sa.CheckConstraint(
            "projection_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_desktop_action_plans_projection_digest_format")
        ),
        sa.CheckConstraint(
            "node_count >= 0 AND text_bytes >= 0 AND redaction_count >= 0",
            name=op.f("ck_desktop_action_plans_counts_non_negative"),
        ),
        sa.CheckConstraint(
            "(status = 'STARTED') = (finished_at IS NULL)",
            name=op.f("ck_desktop_action_plans_finished_when_not_started"),
        ),
        sa.CheckConstraint(
            "(status IN ('FAILED', 'OUTCOME_UNKNOWN')) = (error_code IS NOT NULL)",
            name=op.f("ck_desktop_action_plans_error_code_when_not_ok"),
        ),
        sa.CheckConstraint(
            "(status = 'SUCCEEDED') = (proposed_action IS NOT NULL)",
            name=op.f("ck_desktop_action_plans_proposal_when_succeeded"),
        ),
        sa.CheckConstraint(
            "proposed_action IS NULL OR jsonb_typeof(proposed_action) = 'object'",
            name=op.f("ck_desktop_action_plans_proposal_is_object"),
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    held = connection.execute(
        sa.text("SELECT count(*) FROM task_grants WHERE kind = 'desktop_action_plan'")
    ).scalar()
    if held:
        raise RuntimeError("refusing to downgrade: desktop action-plan grants exist and are an audit record")
    dispatched = connection.execute(
        sa.text(
            "SELECT count(*) FROM desktop_dispatches WHERE operation IN "
            "('set_control_value', 'select_control', 'invoke_control')"
        )
    ).scalar()
    if dispatched:
        raise RuntimeError("refusing to downgrade: S4 desktop dispatches exist and are an audit record")

    op.drop_table("desktop_action_plans")

    op.drop_constraint(_DISPATCH_IDENTITY_CK, "desktop_dispatches", type_="check")
    op.drop_constraint(_DISPATCH_OP_CK, "desktop_dispatches", type_="check")
    op.drop_constraint(op.f("ck_desktop_dispatches_value_ref_shape"), "desktop_dispatches", type_="check")
    op.drop_constraint(
        op.f("ck_desktop_dispatches_option_container_ref_shape"), "desktop_dispatches", type_="check"
    )
    op.drop_constraint(op.f("ck_desktop_dispatches_invoke_effect_shape"), "desktop_dispatches", type_="check")
    op.create_check_constraint(
        _DISPATCH_OP_CK, "desktop_dispatches",
        "operation IN (" + ", ".join(f"'{value}'" for value in _OLD_OPERATIONS) + ")",
    )
    op.create_check_constraint(_DISPATCH_IDENTITY_CK, "desktop_dispatches", _OLD_IDENTITY)
    op.drop_column("desktop_dispatches", "invoke_effect")
    op.drop_column("desktop_dispatches", "option_container_ref")
    op.drop_column("desktop_dispatches", "value_ref")

    op.drop_constraint(_KIND_CK, "task_grants", type_="check")
    op.create_check_constraint(_KIND_CK, "task_grants", _OLD_KIND)
