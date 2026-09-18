"""Persistent Lumi-managed browser profiles for Milestone 8a S1.

Forward-only, additive, and deliberately small. One new table:

* `browser_profiles` -- the identity and lifecycle of one persistent Chromium
  profile: its label, the single registrable domain it is bound to, its status,
  the browser versions that last opened it, the one-owner lease, and the
  account-change fields S2/S3 will fill.

Nothing existing changes. In particular **no Milestone 7b grant or
authorization semantics are touched**: `task_grants.kind` still admits only
`public_research`, `step_authorizations` is untouched, and no column is added
to `action_attempts`, `browser_dispatches` or `research_sessions`. A profile
existing does not widen any authority that existed before it.

Two invariants live in the database rather than only in the service, because
they are the ones whose violation would be silent and unrecoverable:

* **Site immutability.** A trigger refuses any update that changes `id`,
  `site`, `allowed_origins` or `created_at`, and refuses to bring a `DELETED`
  profile back to life. There is no `changeProfileSite` operation anywhere in
  Lumi, and this makes that structural rather than conventional.
* **One live profile per site**, a partial unique index excluding `DELETED`, so
  two directories can never hold the same site's cookies while both are alive,
  and the site becomes available again once a profile is deleted.

What this migration deliberately does **not** add: any column for a cookie, a
token, a storage blob, a `storageState` document or the profile directory path.
The browser owns the profile's contents; Lumi owns its identity.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROFILE_STATUSES = ("NEW", "NEEDS_LOGIN", "AUTHENTICATED", "DELETED")
_SITE_FORMAT = r"site ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'"

#: A profile's site binding is what makes "delete this profile" and "this
#: profile may only reach this site" mean anything. Re-pointing an existing
#: profile at another site would hand site B a directory full of site A's
#: cookies, so the database refuses it outright rather than trusting that no
#: future code path will try.
_PROFILE_IMMUTABILITY_FUNCTION = """
CREATE FUNCTION browser_profiles_enforce_immutable_binding() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.site IS DISTINCT FROM OLD.site
        OR NEW.allowed_origins IS DISTINCT FROM OLD.allowed_origins
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'browser profile % is bound to one site and cannot be rebound', OLD.id
            USING ERRCODE = '23514';
    END IF;
    IF OLD.status = 'DELETED' AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION
            'browser profile % was deleted and cannot be reopened', OLD.id
            USING ERRCODE = '23514';
    END IF;
    IF NEW.revoke_epoch < OLD.revoke_epoch THEN
        RAISE EXCEPTION
            'browser profile % revoke epoch cannot go backwards', OLD.id
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_PROFILE_IMMUTABILITY_TRIGGER = """
CREATE TRIGGER browser_profiles_immutable_binding
    BEFORE UPDATE ON browser_profiles
    FOR EACH ROW EXECUTE FUNCTION browser_profiles_enforce_immutable_binding()
"""


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "browser_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=60), nullable=False),
        sa.Column("site", sa.String(length=253), nullable=False),
        sa.Column("allowed_origins", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("chromium_build", sa.String(length=64), nullable=True),
        sa.Column("playwright_version", sa.String(length=32), nullable=True),
        sa.Column("app_version", sa.String(length=32), nullable=True),
        sa.Column("lease_runtime_generation", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_epoch", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("account_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("account_label_hash", sa.String(length=64), nullable=True),
        sa.Column("last_login_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_profiles")),
        sa.ForeignKeyConstraint(
            ["lease_runtime_generation"],
            ["runtime_generations.id"],
            name=op.f("fk_browser_profiles_lease_runtime_generation_runtime_generations"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"status IN ({_quoted(_PROFILE_STATUSES)})", name=op.f("ck_browser_profiles_status")
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_browser_profiles_revision_positive")),
        sa.CheckConstraint(
            "revoke_epoch >= 0", name=op.f("ck_browser_profiles_revoke_epoch_positive")
        ),
        sa.CheckConstraint(
            "length(label) BETWEEN 1 AND 60", name=op.f("ck_browser_profiles_label_length")
        ),
        sa.CheckConstraint(_SITE_FORMAT, name=op.f("ck_browser_profiles_site_format")),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_origins) = 'array'",
            name=op.f("ck_browser_profiles_allowed_origins_is_array"),
        ),
        sa.CheckConstraint(
            "(deleted_at IS NOT NULL) = (status = 'DELETED')",
            name=op.f("ck_browser_profiles_deleted_at_set"),
        ),
        sa.CheckConstraint(
            "(lease_runtime_generation IS NULL) = (lease_expires_at IS NULL)",
            name=op.f("ck_browser_profiles_lease_complete"),
        ),
        sa.CheckConstraint(
            "status <> 'DELETED' OR lease_runtime_generation IS NULL",
            name=op.f("ck_browser_profiles_deleted_holds_no_lease"),
        ),
        sa.CheckConstraint(
            "account_fingerprint IS NULL OR account_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_browser_profiles_account_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "account_label_hash IS NULL OR account_label_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_browser_profiles_account_label_hash_format"),
        ),
    )
    op.create_index(
        "uq_browser_profiles_site_live",
        "browser_profiles",
        ["site"],
        unique=True,
        postgresql_where=sa.text("status <> 'DELETED'"),
    )
    op.execute(_PROFILE_IMMUTABILITY_FUNCTION)
    op.execute(_PROFILE_IMMUTABILITY_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER browser_profiles_immutable_binding ON browser_profiles")
    op.execute("DROP FUNCTION browser_profiles_enforce_immutable_binding()")
    op.drop_index("uq_browser_profiles_site_live", table_name="browser_profiles")
    op.drop_table("browser_profiles")
