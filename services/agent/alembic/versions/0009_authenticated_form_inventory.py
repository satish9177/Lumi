"""Authenticated form element observation for Milestone 8b S4.

Forward-only in intent, additive in effect. `authenticated_observations`
(S3, account-private, immutable evidence) gains exactly two columns:

* `form_epoch` -- the tab's monotonic form-inventory counter at the moment of the
  observation. A page can replace its whole form without navigating, so the
  document epoch alone never proves that an element ref is still valid.
* `element_inventory` -- the bounded, **value-free** form/element inventory as
  JSON: opaque refs, roles, control types, redacted bounded names, `valueState`
  (`empty | filled | unknown`), flags and redacted option labels. Never a value,
  an option value, an id, a name, a class, a selector, a frame URL or a form
  action.

Schema versioning, explicitly:

* `schema_version = 1` -- an S3 text/link observation. It has **no** inventory.
  Existing rows are backfilled by the column defaults (`form_epoch = 0`,
  `element_inventory = '{}'`) and stay semantically version 1. No historical
  evidence is rewritten: `ADD COLUMN` with a constant default touches no row, and
  the table's immutability trigger (which forbids `UPDATE`) is never involved.
  A CHECK makes "version 1 with an inventory" unrepresentable.
* `schema_version = 2` -- an S4 observation. Every new observation is written as 2.

What this migration deliberately does **not** add: protected values, drafts, a
disclosure manifest or digest, field approval bindings, `frozen_at`, a written
value hash, or anything else that belongs to S5/S6.

Downgrade removes the S4 shape. Version-2 rows cannot exist under the version-1
constraint, and evidence rows cannot be rewritten, so they are deleted; that is
account-private local evidence and the downgrade need not preserve it.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "authenticated_observations"


def upgrade() -> None:
    op.add_column(
        _TABLE, sa.Column("form_epoch", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "element_inventory",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.drop_constraint("schema_version", _TABLE, type_="check")
    op.create_check_constraint("schema_version", _TABLE, "schema_version IN (1, 2)")
    op.create_check_constraint("form_epoch_nonnegative", _TABLE, "form_epoch >= 0")
    op.create_check_constraint(
        "element_inventory_is_object", _TABLE, "jsonb_typeof(element_inventory) = 'object'"
    )
    op.create_check_constraint(
        "v1_has_no_inventory",
        _TABLE,
        "schema_version <> 1 OR (form_epoch = 0 AND element_inventory = '{}'::jsonb)",
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {_TABLE} WHERE schema_version <> 1")
    op.drop_constraint("v1_has_no_inventory", _TABLE, type_="check")
    op.drop_constraint("element_inventory_is_object", _TABLE, type_="check")
    op.drop_constraint("form_epoch_nonnegative", _TABLE, type_="check")
    op.drop_constraint("schema_version", _TABLE, type_="check")
    op.create_check_constraint("schema_version", _TABLE, "schema_version = 1")
    op.drop_column(_TABLE, "element_inventory")
    op.drop_column(_TABLE, "form_epoch")
