"""Controlled downloads, file placement and the cross-executor effect lock, Milestone 10 S2.

* `action_effect_keys`: the effect keys of an effect-bearing action, written with the action. The ledger
  refuses a new attempt whose keys collide with an in-flight or unresolved action in any task.
* `file_transfers`: the durable transfer manifest (safe metadata only; no quarantine path is stored).
* `task_grants.kind` gains `file_transfer`: one confirmed scope funding exactly one download step and one
  placement step.

Downgrade refuses while any transfer or keyed action exists (an audit record).

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_CK = "kind"
_OLD_KIND = (
    "kind IN ('public_research', 'authenticated_read', 'form_prepare', 'desktop_disclose', "
    "'desktop_action_plan', 'desktop_vision_capture', 'desktop_vision_disclose', 'document_disclose')"
)
_NEW_KIND = (
    "kind IN ('public_research', 'authenticated_read', 'form_prepare', 'desktop_disclose', "
    "'desktop_action_plan', 'desktop_vision_capture', 'desktop_vision_disclose', 'document_disclose', "
    "'file_transfer')"
)


def upgrade() -> None:
    op.drop_constraint(_KIND_CK, "task_grants", type_="check")
    op.create_check_constraint(_KIND_CK, "task_grants", _NEW_KIND)

    op.create_table(
        "action_effect_keys",
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("effect_key", sa.String(length=160), nullable=False),
        sa.Column("effect_kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("action_id", "effect_key", name=op.f("pk_action_effect_keys")),
        sa.ForeignKeyConstraint(
            ["action_id"], ["actions.id"], name=op.f("fk_action_effect_keys_action_id_actions"), ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "effect_kind IN ('download', 'file_create', 'project_run', 'external_mutation', 'desktop_mutation')",
            name=op.f("ck_action_effect_keys_effect_kind"),
        ),
        sa.CheckConstraint(
            "effect_key ~ '^[a-z_]+:[a-z_]+:[A-Za-z0-9:._-]{1,140}$'", name=op.f("ck_action_effect_keys_effect_key_shape")
        ),
    )
    op.create_index("ix_action_effect_keys_effect_key", "action_effect_keys", ["effect_key"])

    op.create_table(
        "file_transfers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column("source_host", sa.String(length=255), nullable=False),
        sa.Column("dest_root_id", sa.Uuid(), nullable=False),
        sa.Column("dest_name", sa.String(length=255), nullable=False),
        sa.Column("max_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("length", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=8), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("quarantine_volume", sa.BigInteger(), nullable=True),
        sa.Column("quarantine_index", sa.String(length=20), nullable=True),
        sa.Column("placed_volume", sa.BigInteger(), nullable=True),
        sa.Column("placed_index", sa.String(length=20), nullable=True),
        sa.Column("download_action_id", sa.Uuid(), nullable=True),
        sa.Column("place_action_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_transfers")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_file_transfers_task_id_tasks"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["task_grants.id"], name=op.f("fk_file_transfers_grant_id_task_grants"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["dest_root_id"], ["file_roots.id"], name=op.f("fk_file_transfers_dest_root_id_file_roots"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["download_action_id"], ["actions.id"], name=op.f("fk_file_transfers_download_action_id_actions"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["place_action_id"], ["actions.id"], name=op.f("fk_file_transfers_place_action_id_actions"), ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("task_id", name="uq_file_transfers_task_id"),
        sa.UniqueConstraint("grant_id", name="uq_file_transfers_grant_id"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'DOWNLOADING', 'QUARANTINED', 'PLACING', 'PLACED', 'FAILED', 'OUTCOME_UNKNOWN', 'CANCELLED')",
            name=op.f("ck_file_transfers_status"),
        ),
        sa.CheckConstraint("source_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_file_transfers_source_digest_format")),
        sa.CheckConstraint("sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_file_transfers_sha256_format")),
        sa.CheckConstraint("kind IS NULL OR kind IN ('pdf', 'docx', 'txt')", name=op.f("ck_file_transfers_kind")),
        sa.CheckConstraint("max_bytes BETWEEN 1 AND 10485760", name=op.f("ck_file_transfers_max_bytes_bounded")),
        sa.CheckConstraint("(status = 'PLACED') = (placed_at IS NOT NULL)", name=op.f("ck_file_transfers_placed_at_set")),
        sa.CheckConstraint(
            "status NOT IN ('QUARANTINED', 'PLACING', 'PLACED') OR (sha256 IS NOT NULL AND length IS NOT NULL)",
            name=op.f("ck_file_transfers_verified_before_placement"),
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    transfers = connection.execute(sa.text("SELECT count(*) FROM file_transfers")).scalar()
    keys = connection.execute(sa.text("SELECT count(*) FROM action_effect_keys")).scalar()
    if transfers or keys:
        raise RuntimeError("refusing to downgrade: transfers or effect keys exist and are an audit record")
    op.drop_table("file_transfers")
    op.drop_index("ix_action_effect_keys_effect_key", table_name="action_effect_keys")
    op.drop_table("action_effect_keys")
    op.drop_constraint(_KIND_CK, "task_grants", type_="check")
    op.create_check_constraint(_KIND_CK, "task_grants", _OLD_KIND)
