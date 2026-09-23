"""Registered project recipes, Milestone 10 S3.

* `projects`: project roots, separate from file roots.
* `project_recipes`: the frozen, digest-identified recipe (fixed executable, argv shape, env allowlist,
  package/lockfile hashes, readiness, timeout, stop policy).
* `project_runs`: one run per run task, committed before spawn; at most one live run per project
  (partial unique index).
* `task_grants.kind` gains `project_run`: one confirmed, exact per-run approval.

Downgrade refuses while any project, recipe or run exists (an audit record).

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_CK = "kind"
_OLD_KIND = (
    "kind IN ('public_research', 'authenticated_read', 'form_prepare', 'desktop_disclose', "
    "'desktop_action_plan', 'desktop_vision_capture', 'desktop_vision_disclose', 'document_disclose', "
    "'file_transfer')"
)
_NEW_KIND = (
    "kind IN ('public_research', 'authenticated_read', 'form_prepare', 'desktop_disclose', "
    "'desktop_action_plan', 'desktop_vision_capture', 'desktop_vision_disclose', 'document_disclose', "
    "'file_transfer', 'project_run')"
)


def upgrade() -> None:
    op.drop_constraint(_KIND_CK, "task_grants", type_="check")
    op.create_check_constraint(_KIND_CK, "task_grants", _NEW_KIND)

    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("canonical_path", sa.Text(), nullable=False),
        sa.Column("path_key", sa.Text(), nullable=False),
        sa.Column("volume_serial", sa.BigInteger(), nullable=False),
        sa.Column("dir_index", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name=op.f("ck_projects_status")),
        sa.CheckConstraint("(revoked_at IS NOT NULL) = (status = 'REVOKED')", name=op.f("ck_projects_revoked_at_set")),
        sa.CheckConstraint("dir_index ~ '^[0-9]{1,20}$'", name=op.f("ck_projects_dir_index_format")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_projects_revision_positive")),
        sa.CheckConstraint("length(label) BETWEEN 1 AND 64", name=op.f("ck_projects_label_present")),
    )
    op.create_index(
        "uq_projects_path_key_active", "projects", ["path_key"], unique=True, postgresql_where=sa.text("status = 'ACTIVE'")
    )

    op.create_table(
        "project_recipes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("script_name", sa.String(length=64), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("invalid_reason", sa.String(length=40), nullable=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_recipes")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_project_recipes_project_id_projects"), ondelete="RESTRICT"
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'INVALIDATED', 'REVOKED')", name=op.f("ck_project_recipes_status")),
        sa.CheckConstraint(
            "(status = 'INVALIDATED') = (invalid_reason IS NOT NULL)", name=op.f("ck_project_recipes_invalid_reason_set")
        ),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_project_recipes_digest_format")),
        sa.CheckConstraint("jsonb_typeof(spec) = 'object'", name=op.f("ck_project_recipes_spec_is_object")),
        sa.CheckConstraint("spec ->> 'stop_policy' = 'terminate_job'", name=op.f("ck_project_recipes_stop_policy_fixed")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_project_recipes_revision_positive")),
        sa.CheckConstraint("length(label) BETWEEN 1 AND 64", name=op.f("ck_project_recipes_label_present")),
    )

    op.create_table(
        "project_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("creation_time", sa.BigInteger(), nullable=True),
        sa.Column("exit_code", sa.BigInteger(), nullable=True),
        sa.Column("action_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_runs")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_project_runs_task_id_tasks"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["task_grants.id"], name=op.f("fk_project_runs_grant_id_task_grants"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"], ["project_recipes.id"], name=op.f("fk_project_runs_recipe_id_project_recipes"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_project_runs_project_id_projects"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["action_id"], ["actions.id"], name=op.f("fk_project_runs_action_id_actions"), ondelete="RESTRICT"),
        sa.UniqueConstraint("task_id", name="uq_project_runs_task_id"),
        sa.UniqueConstraint("grant_id", name="uq_project_runs_grant_id"),
        sa.CheckConstraint(
            "status IN ('STARTING', 'RUNNING', 'READY', 'SUCCEEDED', 'FAILED', 'STOPPED', 'ENDED_WITH_RUNTIME', "
            "'OUTCOME_UNKNOWN')",
            name=op.f("ck_project_runs_status"),
        ),
        sa.CheckConstraint("(pid IS NULL) = (creation_time IS NULL)", name=op.f("ck_project_runs_pid_with_creation_time")),
        sa.CheckConstraint("recipe_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_project_runs_recipe_digest_format")),
        sa.CheckConstraint(
            "status NOT IN ('RUNNING', 'READY') OR (pid IS NOT NULL AND resumed_at IS NOT NULL)",
            name=op.f("ck_project_runs_running_has_process"),
        ),
    )
    op.create_index(
        "uq_project_runs_one_live_per_project",
        "project_runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('STARTING', 'RUNNING', 'READY', 'OUTCOME_UNKNOWN')"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("projects", "project_recipes", "project_runs"):
        if connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar():
            raise RuntimeError("refusing to downgrade: projects, recipes or runs exist and are an audit record")
    if connection.execute(sa.text("SELECT count(*) FROM task_grants WHERE kind = 'project_run'")).scalar():
        raise RuntimeError("refusing to downgrade: project-run grants exist and are an audit record")
    op.drop_index("uq_project_runs_one_live_per_project", table_name="project_runs")
    op.drop_table("project_runs")
    op.drop_table("project_recipes")
    op.drop_index("uq_projects_path_key_active", table_name="projects")
    op.drop_table("projects")
    op.drop_constraint(_KIND_CK, "task_grants", type_="check")
    op.create_check_constraint(_KIND_CK, "task_grants", _OLD_KIND)
