"""Trusted orchestration resource-ref registry, Milestone 12 S1.

`orchestration_resources` is the durable, controller-owned registry the orchestration planner cites by an
opaque `ref` (`r1`, `r2`, ...) instead of ever being handed a path, URL, native handle or account identity. A
resource is bound to exactly one orchestration (`orchestration_id`, RESTRICT) and one closed `kind`; a lookup
is always scoped to `(orchestration_id, ref)`, so a ref from another orchestration cannot resolve here even by
accident. `producing_step_id` records which step minted it; `parent_resource_id` is self-referential lineage
for a resource derived from another (unused until a later slice's download -> place -> extract chain).

Milestone 12 S1 mints exactly two kinds (`research_result_ref` from `public_research`, `project_status_ref`
from `project_status`/`project_start`) and composes no capability that accepts one as input yet -- see
`app/domain/orchestration_resources.py`'s `CAPABILITY_RESOURCE_REQUIREMENTS`, which is empty for every
capability this runtime has composed so far. The remaining fourteen kinds are declared now because each
already names a real M1-M10 input/output class (`src/shared/agent-capabilities.ts`); a kind is not minted or
consumed until the slice that reviews the matching capability composition does so.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled identically to `app/domain/orchestration_resources.py`'s `RESOURCE_KINDS`, pinned by
#: `tests/test_orchestration_resources_domain.py` since Python and this migration cannot share a source file.
_RESOURCE_KINDS = (
    "public_url_ref", "research_result_ref",
    "account_context_ref", "account_result_ref",
    "document_ref", "document_result_ref", "transfer_ref",
    "desktop_target_ref", "desktop_snapshot_ref", "desktop_result_ref",
    "app_ref", "project_ref", "project_status_ref",
    "form_target_ref", "form_result_ref", "workflow_ref",
)
_RESOURCE_KIND_CK = "kind IN (" + ", ".join(f"'{item}'" for item in _RESOURCE_KINDS) + ")"
_PRIVACY_CLASS_CK = "privacy_class IN ('public', 'private', 'none')"


def upgrade() -> None:
    op.create_table(
        "orchestration_resources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("orchestration_id", sa.Uuid(), nullable=False),
        sa.Column("ref", sa.String(length=8), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("producing_step_id", sa.Uuid(), nullable=True),
        sa.Column("parent_resource_id", sa.Uuid(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("privacy_class", sa.String(length=16), nullable=False),
        sa.Column("safe_label", sa.String(length=200), nullable=False),
        sa.Column("single_use", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("binding_digest", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orchestration_resources")),
        sa.ForeignKeyConstraint(
            ["orchestration_id"], ["orchestrations.id"],
            name=op.f("fk_orchestration_resources_orchestration_id_orchestrations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["producing_step_id"], ["orchestration_steps.id"],
            name=op.f("fk_orchestration_resources_producing_step_id_orchestration_steps"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_resource_id"], ["orchestration_resources.id"],
            name=op.f("fk_orchestration_resources_parent_resource_id_orchestration_resources"), ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("orchestration_id", "ref", name="uq_orchestration_resources_orchestration_id_ref"),
        sa.CheckConstraint(_RESOURCE_KIND_CK, name=op.f("ck_orchestration_resources_kind")),
        sa.CheckConstraint(_PRIVACY_CLASS_CK, name=op.f("ck_orchestration_resources_privacy_class")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_orchestration_resources_revision_positive")),
        # A resource can be marked consumed only if it was ever eligible to be: single-use.
        sa.CheckConstraint(
            "consumed_at IS NULL OR single_use", name=op.f("ck_orchestration_resources_consumed_only_if_single_use")
        ),
        sa.CheckConstraint("ref ~ '^r[1-9][0-9]{0,5}$'", name=op.f("ck_orchestration_resources_ref_shape")),
    )
    op.create_index("ix_orchestration_resources_orchestration_id", "orchestration_resources", ["orchestration_id"])


def downgrade() -> None:
    op.drop_index("ix_orchestration_resources_orchestration_id", table_name="orchestration_resources")
    op.drop_table("orchestration_resources")
