"""Milestone 12 S2: document composition -- resource backing identity.

`document_read`/`document_compare` compose over M10 S1's task-scoped document model: a `document_task` holds
up to four files, each identified by a `file_id`, and extraction produces a `document_id` within that same
task. Two documents can only be compared if both live in the same task (`DocumentService.compare_local`), so
one orchestration gets exactly one lazily-created, durable document task (`orchestrations.document_task_id`)
that every `document_ref`/`document_result_ref` resource it mints refers back into.

`orchestration_resources` gains two nullable, model-invisible columns:

* `backing_id`: the file id (`document_ref`) or document id (`document_result_ref`) within
  `orchestrations.document_task_id`. Never sent to the planner -- only the trusted runtime API response
  (main-process only) projects it, so main can call `DocumentService`'s own existing, already-reviewed
  methods (`extract`, `compare_local`) without ever handing the model a path, a root id or a file name.
* `backing_text`: the canonical, already-policy-checked public URL a `public_url_ref` resource represents.
  Reserved by this migration for the download/inspection composition that follows; unused by document kinds.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orchestrations", sa.Column("document_task_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_orchestrations_document_task_id_tasks"), "orchestrations", "tasks",
        ["document_task_id"], ["id"], ondelete="RESTRICT",
    )

    op.add_column("orchestration_resources", sa.Column("backing_id", sa.Uuid(), nullable=True))
    op.add_column("orchestration_resources", sa.Column("backing_text", sa.String(length=2048), nullable=True))
    # A resource carries at most one of the two backing facts, and only for the kinds that use it -- the
    # service layer additionally enforces exactly which kind requires which, this CHECK only rules out the
    # shape that can never be correct (both set at once, which no kind ever needs).
    op.create_check_constraint(
        op.f("ck_orchestration_resources_backing_exclusive"),
        "orchestration_resources",
        "backing_id IS NULL OR backing_text IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_orchestration_resources_backing_exclusive"), "orchestration_resources", type_="check")
    op.drop_column("orchestration_resources", "backing_text")
    op.drop_column("orchestration_resources", "backing_id")
    op.drop_constraint(op.f("fk_orchestrations_document_task_id_tasks"), "orchestrations", type_="foreignkey")
    op.drop_column("orchestrations", "document_task_id")
