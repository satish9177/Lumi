"""Protected values and the form-planning grant for Milestone 8b S5.

S5 is an approval-only slice: it adds what is needed to *describe* a form fill
exactly and to obtain an exact approval of that description. It adds nothing
that can write to a page, so it deliberately does not add drafts, a freeze, a
written-value hash, handover state or a dispatch column (all S6).

* `protected_values` -- the user's saved details Lumi may later place into a form
  field. Exactly one row per closed kind (`legal_name`, `preferred_name`,
  `email`, `phone`, `city`, `country`, `linkedin_url`, `portfolio_url`). The
  value is plaintext task data in Lumi's local runtime database: this is **not**
  encryption at rest, and it is not a credential store. `value_digest` is the
  SHA-256 of the UTF-8 canonical value and the database itself refuses a row
  whose digest does not match its value. `preview` is the deterministic masked
  text a provider and the trusted card may see; the value never leaves the row.
* `task_grants` learns a third kind, `form_prepare`: the trusted permission to
  let one planner see a bounded form structure and masked previews of chosen
  saved details. It is bound to a profile exactly as `authenticated_read` is, so
  the `profile_binding` check now covers both kinds. The one-open-grant-per-task
  index now applies per kind, because a task holds an account-reading grant and
  a form-planning grant at the same time.

Forward-only in intent. Downgrade removes `form_prepare` grants (local
authority, never reusable), the table and the widened constraints. Nothing
historical is rewritten.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "legal_name",
    "preferred_name",
    "email",
    "phone",
    "city",
    "country",
    "linkedin_url",
    "portfolio_url",
)
_QUOTED_KINDS = ", ".join(f"'{kind}'" for kind in _KINDS)


def upgrade() -> None:
    op.create_table(
        "protected_values",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("value_digest", sa.String(length=64), nullable=False),
        sa.Column("preview", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_protected_values")),
        sa.UniqueConstraint("kind", name=op.f("uq_protected_values_kind")),
        sa.CheckConstraint(f"kind IN ({_QUOTED_KINDS})", name=op.f("ck_protected_values_kind")),
        sa.CheckConstraint(
            "value_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_protected_values_value_digest_format")
        ),
        sa.CheckConstraint(
            "value_digest = encode(sha256(convert_to(value, 'UTF8')), 'hex')",
            name=op.f("ck_protected_values_digest_matches_value"),
        ),
        sa.CheckConstraint(
            "length(value) BETWEEN 1 AND 300",
            name=op.f("ck_protected_values_value_bounded"),
        ),
        sa.CheckConstraint("length(preview) >= 1", name=op.f("ck_protected_values_preview_present")),
    )

    op.drop_constraint("kind", "task_grants", type_="check")
    op.create_check_constraint(
        "kind", "task_grants", "kind IN ('public_research', 'authenticated_read', 'form_prepare')"
    )
    op.drop_constraint("profile_binding", "task_grants", type_="check")
    op.create_check_constraint(
        "profile_binding",
        "task_grants",
        "((kind IN ('authenticated_read', 'form_prepare')) = (profile_id IS NOT NULL)) "
        "AND ((profile_id IS NULL) = (profile_revoke_epoch IS NULL)) "
        "AND (profile_revoke_epoch IS NULL OR profile_revoke_epoch >= 0)",
    )
    op.drop_index("uq_task_grants_task_id_open", table_name="task_grants")
    op.create_index(
        "uq_task_grants_task_id_kind_open",
        "task_grants",
        ["task_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status IN ('ACTIVE', 'PENDING')"),
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM step_authorizations WHERE grant_id IN "
        "(SELECT id FROM task_grants WHERE kind = 'form_prepare')"
    )
    op.execute("DELETE FROM task_grants WHERE kind = 'form_prepare'")
    op.drop_index("uq_task_grants_task_id_kind_open", table_name="task_grants")
    op.create_index(
        "uq_task_grants_task_id_open",
        "task_grants",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('ACTIVE', 'PENDING')"),
    )
    op.drop_constraint("profile_binding", "task_grants", type_="check")
    op.create_check_constraint(
        "profile_binding",
        "task_grants",
        "((kind = 'authenticated_read') = (profile_id IS NOT NULL)) "
        "AND ((profile_id IS NULL) = (profile_revoke_epoch IS NULL)) "
        "AND (profile_revoke_epoch IS NULL OR profile_revoke_epoch >= 0)",
    )
    op.drop_constraint("kind", "task_grants", type_="check")
    op.create_check_constraint(
        "kind", "task_grants", "kind IN ('public_research', 'authenticated_read')"
    )
    op.drop_table("protected_values")
