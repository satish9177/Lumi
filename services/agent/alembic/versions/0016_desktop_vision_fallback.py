"""Scoped desktop visual fallback, Milestone 9 S5: capture and vision-disclosure grants and their records.

S5 adds exactly two new grant kinds -- no second authorization framework, the same shape S2's
`desktop_disclose` and S4's `desktop_action_plan` already are -- and two new tables, neither of which
ever holds a raw pixel:

* `desktop_vision_capture`: consent to take ONE scoped screenshot of ONE already-approved window, for
  local use only (on-device display, local OCR). No provider is ever contacted for this grant kind.
  `desktop_captures` records one attempt: geometry/frame identity (digests only -- never a coordinate,
  rect or raw byte), pixel dimensions and DPI, never the image itself.
* `desktop_vision_disclose`: a SEPARATE, later approval to send ONE freshly-recaptured image (never the
  first capture's own bytes -- "a fresh image needs fresh approval") to ONE named provider and model for
  ONE stated purpose. Text disclosure authority from S2/S4 does not extend to this: it is its own grant
  kind, checked by the router the same structural way (one recipient, zero failover) plus the one new
  rule this slice adds -- an image may cross this boundary at all only for this one task class.
  `desktop_vision_disclosures` records one attempt and its candidates (evidence/regions only -- schema
  closed so a vision result can never itself express a click, a coordinate or an approval).

`grant_id` and `task_id` are UNIQUE on both new tables, exactly like `desktop_disclosures` and
`desktop_action_plans`: one approval funds exactly one attempt, ever.

`task_grants` also gains one new, nullable column: `approval_input_tick`. It holds the
`GetLastInputInfo` tick read at the trusted click that confirms a `desktop_vision_capture` or
`desktop_vision_disclose` grant, set atomically in that same PENDING -> ACTIVE compare-and-swap and
left NULL for every other grant kind. A later claim compares against this stored reading, never a
tick freshly read at claim time (which would trivially always match "now" and detect nothing) --
so a human-input takeover between approval and claim is still caught.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_CK = "kind"
_OLD_KIND = (
    "kind IN ('public_research', 'authenticated_read', 'form_prepare', 'desktop_disclose', "
    "'desktop_action_plan')"
)
_NEW_KIND = (
    "kind IN ('public_research', 'authenticated_read', 'form_prepare', 'desktop_disclose', "
    "'desktop_action_plan', 'desktop_vision_capture', 'desktop_vision_disclose')"
)

_CAPTURE_STATUSES = ("STARTED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN")


def upgrade() -> None:
    op.drop_constraint(_KIND_CK, "task_grants", type_="check")
    op.create_check_constraint(_KIND_CK, "task_grants", _NEW_KIND)

    # NULL for every grant kind except the two S5 kinds. Set once, atomically, in the same
    # compare-and-swap that moves a `desktop_vision_capture`/`desktop_vision_disclose` grant from
    # PENDING to ACTIVE (the trusted click) -- never touched again afterwards, and never on any
    # other grant kind. This is deliberately NOT part of the immutable `scope` JSON: it is not part
    # of what the person approved, only a fact recorded about the approval itself.
    op.add_column("task_grants", sa.Column("approval_input_tick", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "approval_input_tick_range",
        "task_grants",
        "approval_input_tick IS NULL OR (approval_input_tick >= 0 AND approval_input_tick <= 4294967295)",
    )

    op.create_table(
        "desktop_captures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("surface_ref", sa.String(length=4), nullable=False),
        sa.Column("surface_epoch", sa.Integer(), nullable=False),
        # An immutable audit copy of `task_grants.approval_input_tick` -- the `GetLastInputInfo` tick
        # read at CONFIRM time (the trusted click) and already what the claim itself compared against.
        # Never re-read fresh at claim time, which would trivially always match "now" and detect nothing.
        sa.Column("approval_input_tick", sa.BigInteger(), nullable=False),
        sa.Column("geometry_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("frame_digest", sa.String(length=64), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("dpi", sa.Integer(), nullable=True),
        sa.Column("monitor_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_desktop_captures")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_desktop_captures_task_id_tasks"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["task_grants.id"], name=op.f("fk_desktop_captures_grant_id_task_grants"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("grant_id", name="uq_desktop_captures_grant_id"),
        sa.UniqueConstraint("task_id", name="uq_desktop_captures_task_id"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in _CAPTURE_STATUSES) + ")",
            name=op.f("ck_desktop_captures_status"),
        ),
        sa.CheckConstraint("surface_ref ~ '^s([1-9]|1[0-6])$'", name=op.f("ck_desktop_captures_surface_ref_shape")),
        sa.CheckConstraint(
            "geometry_fingerprint IS NULL OR geometry_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_desktop_captures_geometry_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "frame_digest IS NULL OR frame_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_desktop_captures_frame_digest_format"),
        ),
        sa.CheckConstraint(
            "(status = 'STARTED') = (finished_at IS NULL)",
            name=op.f("ck_desktop_captures_finished_when_not_started"),
        ),
        sa.CheckConstraint(
            "(status IN ('FAILED', 'OUTCOME_UNKNOWN')) = (error_code IS NOT NULL)",
            name=op.f("ck_desktop_captures_error_code_when_not_ok"),
        ),
        sa.CheckConstraint(
            "(status = 'SUCCEEDED') = (geometry_fingerprint IS NOT NULL AND frame_digest IS NOT NULL "
            "AND width IS NOT NULL AND height IS NOT NULL AND dpi IS NOT NULL AND monitor_id IS NOT NULL)",
            name=op.f("ck_desktop_captures_frame_fields_when_succeeded"),
        ),
        sa.CheckConstraint(
            "width IS NULL OR width > 0", name=op.f("ck_desktop_captures_width_positive")
        ),
        sa.CheckConstraint(
            "height IS NULL OR height > 0", name=op.f("ck_desktop_captures_height_positive")
        ),
        sa.CheckConstraint("dpi IS NULL OR dpi > 0", name=op.f("ck_desktop_captures_dpi_positive")),
        sa.CheckConstraint(
            "approval_input_tick >= 0 AND approval_input_tick <= 4294967295",
            name=op.f("ck_desktop_captures_approval_input_tick_range"),
        ),
    )

    op.create_table(
        "desktop_vision_disclosures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        # The prior local-only capture that established fallback eligibility. An audit link only: the
        # image this disclosure actually sends is a FRESH capture taken at claim time, never these bytes
        # (which were never persisted anywhere) and never even this capture's own frame digest.
        sa.Column("capture_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=400), nullable=False),
        # Same reasoning as `desktop_captures.approval_input_tick`: an immutable audit copy of
        # `task_grants.approval_input_tick`.
        sa.Column("approval_input_tick", sa.BigInteger(), nullable=False),
        sa.Column("geometry_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("frame_digest", sa.String(length=64), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("dpi", sa.Integer(), nullable=True),
        sa.Column("monitor_id", sa.BigInteger(), nullable=True),
        # The closed candidate list the provider returned: evidence only, never authority. NULL until a
        # result is recorded.
        sa.Column("candidates", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_desktop_vision_disclosures")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_desktop_vision_disclosures_task_id_tasks"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["task_grants.id"],
            name=op.f("fk_desktop_vision_disclosures_grant_id_task_grants"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["capture_id"], ["desktop_captures.id"],
            name=op.f("fk_desktop_vision_disclosures_capture_id_desktop_captures"), ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("grant_id", name="uq_desktop_vision_disclosures_grant_id"),
        sa.UniqueConstraint("task_id", name="uq_desktop_vision_disclosures_task_id"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in _CAPTURE_STATUSES) + ")",
            name=op.f("ck_desktop_vision_disclosures_status"),
        ),
        sa.CheckConstraint(
            "geometry_fingerprint IS NULL OR geometry_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_desktop_vision_disclosures_geometry_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "frame_digest IS NULL OR frame_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_desktop_vision_disclosures_frame_digest_format"),
        ),
        sa.CheckConstraint(
            "(status = 'STARTED') = (finished_at IS NULL)",
            name=op.f("ck_desktop_vision_disclosures_finished_when_not_started"),
        ),
        sa.CheckConstraint(
            "(status IN ('FAILED', 'OUTCOME_UNKNOWN')) = (error_code IS NOT NULL)",
            name=op.f("ck_desktop_vision_disclosures_error_code_when_not_ok"),
        ),
        sa.CheckConstraint(
            "candidates IS NULL OR jsonb_typeof(candidates) = 'array'",
            name=op.f("ck_desktop_vision_disclosures_candidates_is_array"),
        ),
        sa.CheckConstraint(
            "(status = 'SUCCEEDED') = (candidates IS NOT NULL)",
            name=op.f("ck_desktop_vision_disclosures_candidates_when_succeeded"),
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    held = connection.execute(
        sa.text(
            "SELECT count(*) FROM task_grants WHERE kind IN "
            "('desktop_vision_capture', 'desktop_vision_disclose')"
        )
    ).scalar()
    if held:
        raise RuntimeError("refusing to downgrade: S5 vision grants exist and are an audit record")

    op.drop_table("desktop_vision_disclosures")
    op.drop_table("desktop_captures")

    op.drop_constraint("approval_input_tick_range", "task_grants", type_="check")
    op.drop_column("task_grants", "approval_input_tick")

    op.drop_constraint(_KIND_CK, "task_grants", type_="check")
    op.create_check_constraint(_KIND_CK, "task_grants", _OLD_KIND)
