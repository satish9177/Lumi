"""The network-frozen local form draft, Milestone 8b S6: `form_drafts` and `frozen_at`.

S6 fills an authenticated form in Lumi's own browser with the network frozen, verifies
the values are in the fields and hands the browser over. This migration adds exactly
the two things that needs to be *recorded* -- and nothing that could restore a draft:

* `browser_dispatches.frozen_at` (nullable timestamptz). The proof that the runtime
  received a successful worker freeze verification -- both layers frozen, no request in
  flight, no relay open -- **before the first field write**. It is written once, by a
  compare-and-set that only succeeds while the dispatch is still `DISPATCHED`, and it is
  never back-filled after a write. Recovery reads it: a crashed `prepare_form` with
  `frozen_at` set may say the remote effect was impossible under the verified freeze; one
  with it null may not. Every existing dispatch keeps `NULL`.
* `form_drafts`. What Lumi *prepared or attempted*, not a restorable draft: the browser
  page is local and is lost on any restart. Each row carries safe audit material only --
  refs, identity hashes, approved digests and verified-local-value hashes -- **never a raw
  value, a selector or a locator description**. At most one live (`PREPARED` or `STALE`)
  draft exists per profile and per task, enforced by partial unique indexes.

Nothing historical is rewritten: S5's `prepared_nothing` approvals stay exactly as they
were, and their `form-prepare-v1` manifests are not executable.

Forward-only in intent. Downgrade drops the table and the column.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = ("PREPARED", "STALE", "DISCARDED", "HANDED_OVER")
_QUOTED_STATUSES = ", ".join(f"'{status}'" for status in _STATUSES)


def upgrade() -> None:
    op.add_column(
        "browser_dispatches", sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "form_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("dispatch_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("draft_digest", sa.String(length=64), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("tab", sa.String(length=4), nullable=False),
        sa.Column("document_epoch", sa.Integer(), nullable=False),
        sa.Column("form_epoch", sa.Integer(), nullable=False),
        sa.Column("form_ref", sa.String(length=4), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_form_drafts")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_form_drafts_task_id_tasks"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["browser_profiles.id"],
            name=op.f("fk_form_drafts_profile_id_browser_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["action_id"], ["actions.id"], name=op.f("fk_form_drafts_action_id_actions"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["action_attempts.id"],
            name=op.f("fk_form_drafts_attempt_id_action_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["browser_dispatches.id"],
            name=op.f("fk_form_drafts_dispatch_id_browser_dispatches"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("attempt_id", name=op.f("uq_form_drafts_attempt_id")),
        sa.UniqueConstraint("dispatch_id", name=op.f("uq_form_drafts_dispatch_id")),
        sa.CheckConstraint(f"status IN ({_QUOTED_STATUSES})", name=op.f("ck_form_drafts_status")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_form_drafts_revision_positive")),
        sa.CheckConstraint(
            "manifest_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_form_drafts_manifest_digest_format")
        ),
        sa.CheckConstraint(
            "draft_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_form_drafts_draft_digest_format")
        ),
        sa.CheckConstraint("tab ~ '^t[1-3]$'", name=op.f("ck_form_drafts_tab_ref")),
        sa.CheckConstraint("form_ref ~ '^f[1-5]$'", name=op.f("ck_form_drafts_form_ref_shape")),
        sa.CheckConstraint(
            "document_epoch >= 1 AND form_epoch >= 1", name=op.f("ck_form_drafts_epochs_positive")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(fields) = 'array' AND jsonb_array_length(fields) <= 12",
            name=op.f("ck_form_drafts_fields_bounded"),
        ),
    )
    live = sa.text("status IN ('PREPARED', 'STALE')")
    op.create_index("uq_form_drafts_profile_id_live", "form_drafts", ["profile_id"], unique=True, postgresql_where=live)
    op.create_index("uq_form_drafts_task_id_live", "form_drafts", ["task_id"], unique=True, postgresql_where=live)


def downgrade() -> None:
    op.drop_index("uq_form_drafts_task_id_live", table_name="form_drafts")
    op.drop_index("uq_form_drafts_profile_id_live", table_name="form_drafts")
    op.drop_table("form_drafts")
    op.drop_column("browser_dispatches", "frozen_at")
