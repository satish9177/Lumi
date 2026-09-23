from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.domain.action_status import (
    OPEN_APPROVAL_STATUSES,
    ActionStatus,
    ApprovalStatus,
    AttemptOutcome,
    RiskTier,
)
from app.domain.browser_dispatch import BrowserEffect, DispatchStatus
from app.domain.browser_profile import ProfileStatus
from app.domain.local_form_draft import DraftStatus
from app.domain.login_takeover import LoginAttemptStatus
from app.domain.protected_values import PROTECTED_KINDS
from app.domain.research import GrantStatus
from app.domain.task_status import TaskStatus

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

_HEX_DIGEST_FORMAT = "proposal_digest ~ '^[0-9a-f]{64}$'"


def _values(
    members: type[TaskStatus]
    | type[ActionStatus]
    | type[ApprovalStatus]
    | type[AttemptOutcome]
    | type[RiskTier]
    | type[DispatchStatus]
    | type[BrowserEffect]
    | type[ProfileStatus]
    | type[LoginAttemptStatus],
) -> str:
    return ", ".join(f"'{member.value}'" for member in members)


tasks = Table(
    "tasks",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("status", String(32), nullable=False),
    # Incremented by every mutation; updates are compare-and-swap on this value.
    Column("revision", BigInteger(), nullable=False),
    # Allocates task_events.sequence. Bumping it in the same UPDATE that changes
    # the task serializes event appends through the task row lock.
    Column("last_event_sequence", BigInteger(), nullable=False),
    Column("request", JSONB(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(f"status IN ({_values(TaskStatus)})", name="status"),
    CheckConstraint("revision >= 1", name="revision_positive"),
    CheckConstraint("last_event_sequence >= 1", name="last_event_sequence_positive"),
    CheckConstraint("jsonb_typeof(request) = 'object'", name="request_is_object"),
)

task_events = Table(
    "task_events",
    metadata,
    Column("id", BigInteger(), Identity(always=True), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("sequence", BigInteger(), nullable=False),
    # The task revision this event produced, for later stale-write reconciliation.
    Column("task_revision", BigInteger(), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("payload", JSONB(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("task_id", "sequence"),
    CheckConstraint("sequence >= 1", name="sequence_positive"),
    CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_is_object"),
)

#: One row per runtime process. An execution attempt records the generation that
#: started it, so startup can tell "my own in-flight work" from "work a dead
#: process left behind" without guessing. A future worker-lease design adds
#: heartbeat/expiry columns here and stops relying on "one process at a time".
runtime_generations = Table(
    "runtime_generations",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

actions = Table(
    "actions",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    # Deduplicates re-proposals from a retrying planner or a reconnecting client.
    Column("idempotency_key", String(200), nullable=False),
    Column("tool_name", String(64), nullable=False),
    Column("risk_tier", String(8), nullable=False),
    # The exact proposed action. Immutable after insert, enforced by a trigger.
    Column("proposal", JSONB(), nullable=False),
    # SHA-256 over canonical JSON of `proposal`, always computed by the server.
    Column("proposal_digest", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("revision", BigInteger(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("task_id", "idempotency_key"),
    CheckConstraint(f"status IN ({_values(ActionStatus)})", name="status"),
    CheckConstraint(f"risk_tier IN ({_values(RiskTier)})", name="risk_tier"),
    CheckConstraint("revision >= 1", name="revision_positive"),
    CheckConstraint("jsonb_typeof(proposal) = 'object'", name="proposal_is_object"),
    CheckConstraint(_HEX_DIGEST_FORMAT, name="proposal_digest_format"),
    CheckConstraint("length(idempotency_key) >= 1", name="idempotency_key_present"),
)

Index("ix_actions_task_id_created_at", actions.c.task_id, actions.c.created_at)

approvals = Table(
    "approvals",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("action_id", Uuid(), ForeignKey("actions.id", ondelete="RESTRICT"), nullable=False),
    # The action revision this approval is bound to. Execution refuses to claim
    # an approval whose bound revision is no longer the action's revision, so any
    # later mutation of the action invalidates an approval already granted.
    Column("action_revision", BigInteger(), nullable=False),
    # Binds the approval to the exact proposal bytes that were approved.
    Column("proposal_digest", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    Column("rejected_at", DateTime(timezone=True), nullable=True),
    Column("consumed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(f"status IN ({_values(ApprovalStatus)})", name="status"),
    CheckConstraint(_HEX_DIGEST_FORMAT, name="proposal_digest_format"),
    CheckConstraint("action_revision >= 1", name="action_revision_positive"),
    CheckConstraint("expires_at > created_at", name="expires_after_creation"),
    # One-way: an approved approval always has approved_at, and keeps it if it
    # is later rejected, so the audit trail survives the status change.
    CheckConstraint(
        "status NOT IN ('APPROVED', 'CONSUMED') OR approved_at IS NOT NULL",
        name="approved_at_set",
    ),
    CheckConstraint("(rejected_at IS NOT NULL) = (status = 'REJECTED')", name="rejected_at_set"),
    CheckConstraint("(consumed_at IS NOT NULL) = (status = 'CONSUMED')", name="consumed_at_set"),
)

#: At most one live approval per action, which makes "approval is single-use" a
#: database fact: claiming one flips it to CONSUMED, which leaves this index.
Index(
    "uq_approvals_action_id_open",
    approvals.c.action_id,
    unique=True,
    postgresql_where=approvals.c.status.in_(sorted(s.value for s in OPEN_APPROVAL_STATUSES)),
)

action_attempts = Table(
    "action_attempts",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("action_id", Uuid(), ForeignKey("actions.id", ondelete="RESTRICT"), nullable=False),
    Column("attempt_number", Integer(), nullable=False),
    # The approval this attempt claimed. Unique, so one approval can never fund
    # two attempts even if application logic slips. Null exactly when the
    # attempt was funded by a scoped step authorization instead (Milestone 7b).
    Column("approval_id", Uuid(), ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=True),
    # Milestone 7b: the single-use authorization a research step derived from
    # the task's grant. Unique for the same reason `approval_id` is.
    Column(
        "step_authorization_id",
        Uuid(),
        ForeignKey("step_authorizations.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column(
        "runtime_generation",
        Uuid(),
        ForeignKey("runtime_generations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("outcome", String(32), nullable=True),
    Column("result", JSONB(), nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("action_id", "attempt_number"),
    UniqueConstraint("approval_id"),
    UniqueConstraint("step_authorization_id"),
    # Every attempt is funded by exactly one authority: an exact approval the
    # user granted for this proposal, or a single-use step authorization
    # derived from a confirmed task grant. Never both, never neither.
    CheckConstraint(
        "(approval_id IS NULL) <> (step_authorization_id IS NULL)",
        name="one_authorization",
    ),
    CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
    CheckConstraint(f"outcome IS NULL OR outcome IN ({_values(AttemptOutcome)})", name="outcome"),
    # An attempt is unfinished exactly while Lumi does not yet have an outcome.
    CheckConstraint("(finished_at IS NULL) = (outcome IS NULL)", name="finished_with_outcome"),
    CheckConstraint("result IS NULL OR jsonb_typeof(result) = 'object'", name="result_is_object"),
)

#: At most one unfinished attempt per action, so a duplicate execution-start can
#: never produce a second in-flight side effect.
Index(
    "uq_action_attempts_action_id_unfinished",
    action_attempts.c.action_id,
    unique=True,
    postgresql_where=action_attempts.c.finished_at.is_(None),
)


#: One row per browser worker process, owned by the runtime generation that
#: registered it. The worker's counterpart to `runtime_generations`, and for the
#: same reason: a result from a process that no longer exists must be
#: recognisable as such rather than written into the ledger. Deliberately not a
#: lease -- adding `expires_at` and `heartbeat_at` here is how it becomes one.
browser_worker_generations = Table(
    "browser_worker_generations",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column(
        "runtime_generation",
        Uuid(),
        ForeignKey("runtime_generations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("worker_started_at", DateTime(timezone=True), nullable=False),
    Column("registered_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

#: The browser work done for one execution attempt. `attempt_id` is UNIQUE, so
#: with Milestone 2's "one unfinished attempt per action" and "one approval funds
#: one attempt", a second real browser submission for one approved action cannot
#: be written down at all.
browser_dispatches = Table(
    "browser_dispatches",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("action_id", Uuid(), ForeignKey("actions.id", ondelete="RESTRICT"), nullable=False),
    # Null for a read-only reconciliation lookup, which is not an execution
    # attempt and must never be recorded as one.
    Column(
        "attempt_id",
        Uuid(),
        ForeignKey("action_attempts.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column(
        "worker_generation",
        Uuid(),
        ForeignKey("browser_worker_generations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("operation", String(64), nullable=False),
    Column("site", String(64), nullable=False),
    Column("effect", String(16), nullable=False),
    # Milestone 7b: the task-owned browser session this dispatch drove, for a
    # research step. Null for one-shot work that owns no session.
    Column(
        "session_id",
        Uuid(),
        ForeignKey("research_sessions.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("status", String(32), nullable=False),
    Column("submitted", Boolean(), nullable=False, server_default=false()),
    Column("observation_id", Uuid(), nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("duration_ms", Integer(), nullable=True),
    Column("result", JSONB(), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    # Milestone 8b S6. The proof that the runtime received a successful worker freeze
    # verification (both layers frozen, nothing in flight, no relay open) BEFORE the
    # first field write. Written once, only while the dispatch is DISPATCHED, and never
    # back-filled after a write. NULL for every dispatch that is not a frozen local draft.
    Column("frozen_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("attempt_id"),
    CheckConstraint(f"status IN ({_values(DispatchStatus)})", name="status"),
    CheckConstraint("result IS NULL OR jsonb_typeof(result) = 'object'", name="result_is_object"),
    CheckConstraint(
        "(finished_at IS NULL) = (status = 'DISPATCHED')", name="finished_with_status"
    ),
    CheckConstraint(
        "effect <> 'CONSEQUENTIAL' OR attempt_id IS NOT NULL",
        name="consequential_needs_attempt",
    ),
)

Index(
    "ix_browser_dispatches_action_id_started_at",
    browser_dispatches.c.action_id,
    browser_dispatches.c.started_at,
)


ANSWER_STATUSES = ("answered", "not_found", "ambiguous", "not_verified")

#: Milestone 7a: one bounded, hashed page observation per successful
#: `inspect_public_page` attempt, and at most one grounded answer for it. The
#: evidence columns are immutable and the answer can be written once (trigger
#: in migration 0004). There is deliberately no column for cookies, headers,
#: storage state, DOM or screenshots.
page_observations = Table(
    "page_observations",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("action_id", Uuid(), ForeignKey("actions.id", ondelete="RESTRICT"), nullable=False),
    Column(
        "attempt_id", Uuid(), ForeignKey("action_attempts.id", ondelete="RESTRICT"), nullable=False
    ),
    Column(
        "dispatch_id",
        Uuid(),
        ForeignKey("browser_dispatches.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "worker_generation",
        Uuid(),
        ForeignKey("browser_worker_generations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("schema_version", Integer(), nullable=False),
    Column("provenance", String(32), nullable=False),
    Column("requested_url", String(2048), nullable=False),
    Column("final_url", String(2048), nullable=False),
    Column("title", String(200), nullable=False),
    Column("document_epoch", Integer(), nullable=False),
    Column("settled", Boolean(), nullable=False),
    Column("truncated", Boolean(), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("projection", JSONB(), nullable=False),
    Column("answer_status", String(16), nullable=True),
    Column("answer", JSONB(), nullable=True),
    Column("answer_provider", String(16), nullable=True),
    Column("answer_model", String(64), nullable=True),
    Column("answered_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("attempt_id"),
    UniqueConstraint("dispatch_id"),
    CheckConstraint("schema_version = 1", name="schema_version"),
    CheckConstraint("provenance = 'untrusted_environment'", name="provenance"),
    CheckConstraint("document_epoch >= 1", name="document_epoch_positive"),
    CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_format"),
    CheckConstraint("jsonb_typeof(projection) = 'object'", name="projection_is_object"),
    CheckConstraint(
        "answer_status IS NULL OR answer_status IN ("
        + ", ".join(f"'{status}'" for status in ANSWER_STATUSES)
        + ")",
        name="answer_status",
    ),
    CheckConstraint(
        "(answered_at IS NULL) = (answer IS NULL) AND (answered_at IS NULL) = (answer_status IS NULL)",
        name="answer_complete",
    ),
    CheckConstraint("answer IS NULL OR jsonb_typeof(answer) = 'object'", name="answer_is_object"),
)

Index(
    "ix_page_observations_task_id_created_at",
    page_observations.c.task_id,
    page_observations.c.created_at,
)
Index("ix_page_observations_action_id", page_observations.c.action_id)


# ---- Milestone 7b: public web research -------------------------------------
#
# Five tables, added to the same ledger rather than beside it. A research step
# is an ordinary `actions` row with an ordinary `action_attempts` row; what is
# new is only *where its authority came from*.

GRANT_STATUSES = tuple(status.value for status in GrantStatus)

#: One reusable, scoped authorization per research task, created by a trusted
#: renderer click. It is task-bound (`task_id`), scope-bound (`scope_digest`),
#: policy-version-bound, expiring (`expires_at`) and revocable (`revoked_at`).
#: It authorises operations *inside* its scope; it never authorises an exact
#: step, and a model can neither create nor widen one -- there is no route,
#: parameter or code path by which a proposal becomes a grant.
task_grants = Table(
    "task_grants",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    # Bumped by every mutation; confirm and revoke are compare-and-swap on it,
    # so a stale trusted click cannot confirm a scope the user did not see.
    Column("revision", BigInteger(), nullable=False),
    Column("policy_version", String(40), nullable=False),
    # The exact scope, immutable after insert (trigger in migration 0005).
    Column("scope", JSONB(), nullable=False),
    Column("scope_digest", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("confirmed_at", DateTime(timezone=True), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    # Budget accounting the runtime, not the planner, keeps.
    Column("first_step_at", DateTime(timezone=True), nullable=True),
    Column("planner_calls", Integer(), nullable=False, server_default=text("0")),
    # Milestone 8a S3. The profile an `authenticated_read` grant is bound to,
    # and the value that profile's `revoke_epoch` had when the scope was shown.
    # Both null for public research, both required for an authenticated read
    # (`profile_binding`), and immutable after insert like `scope`.
    Column(
        "profile_id",
        Uuid(),
        ForeignKey("browser_profiles.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("profile_revoke_epoch", BigInteger(), nullable=True),
    # Milestone 9 S5. The `GetLastInputInfo` tick read at CONFIRM time (the trusted click) for a
    # `desktop_vision_capture` or `desktop_vision_disclose` grant, set atomically in the same
    # PENDING -> ACTIVE compare-and-swap as `confirm_grant`. Never re-read fresh at claim time,
    # which would trivially always match "now" and detect no human-input takeover. Deliberately not
    # part of the immutable `scope` JSON: a fact about the approval, not what was approved.
    Column("approval_input_tick", BigInteger(), nullable=True),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status}'" for status in GRANT_STATUSES) + ")",
        name="status",
    ),
    CheckConstraint(
        "kind IN ('public_research', 'authenticated_read', 'form_prepare', 'desktop_disclose', "
        "'desktop_action_plan', 'desktop_vision_capture', 'desktop_vision_disclose', 'document_disclose')",
        name="kind",
    ),
    CheckConstraint(
        "((kind IN ('authenticated_read', 'form_prepare')) = (profile_id IS NOT NULL)) "
        "AND ((profile_id IS NULL) = (profile_revoke_epoch IS NULL)) "
        "AND (profile_revoke_epoch IS NULL OR profile_revoke_epoch >= 0)",
        name="profile_binding",
    ),
    CheckConstraint("revision >= 1", name="revision_positive"),
    CheckConstraint("scope_digest ~ '^[0-9a-f]{64}$'", name="scope_digest_format"),
    CheckConstraint("jsonb_typeof(scope) = 'object'", name="scope_is_object"),
    CheckConstraint("planner_calls >= 0", name="planner_calls_positive"),
    CheckConstraint(
        "approval_input_tick IS NULL OR (approval_input_tick >= 0 AND approval_input_tick <= 4294967295)",
        name="approval_input_tick_range",
    ),
    # A grant only authorises while ACTIVE, and only an active grant has both a
    # confirmation and a window. Nothing can be active without an expiry.
    CheckConstraint(
        "(status <> 'PENDING') = (confirmed_at IS NOT NULL)",
        name="confirmed_when_not_pending",
    ),
    CheckConstraint(
        "status <> 'ACTIVE' OR (expires_at IS NOT NULL AND expires_at > confirmed_at)",
        name="active_has_window",
    ),
    CheckConstraint("(revoked_at IS NOT NULL) = (status = 'REVOKED')", name="revoked_at_set"),
    CheckConstraint("(completed_at IS NOT NULL) = (status = 'COMPLETED')", name="completed_at_set"),
)

#: At most one grant per task that is still pending or active, so a task can
#: never hold two live scopes and a second confirmation cannot widen the first.
Index("ix_task_grants_profile_id", task_grants.c.profile_id)

#: Per kind since Milestone 8b S5: a task holds its account-reading grant and its
#: form-planning grant at once, and never two live grants of the same kind.
Index(
    "uq_task_grants_task_id_kind_open",
    task_grants.c.task_id,
    task_grants.c.kind,
    unique=True,
    postgresql_where=task_grants.c.status.in_(("PENDING", "ACTIVE")),
)

#: One single-use authorization per research step, minted by the runtime only
#: after checking that the step is inside its grant. Bound to the grant *and*
#: its revision, the action and its revision, the exact proposal digest, the
#: policy version and its own expiry. Consumed by exactly one attempt
#: (`action_attempts.step_authorization_id` is UNIQUE), so a replay cannot fund
#: a second execution of the same step.
step_authorizations = Table(
    "step_authorizations",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("grant_id", Uuid(), ForeignKey("task_grants.id", ondelete="RESTRICT"), nullable=False),
    Column("grant_revision", BigInteger(), nullable=False),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("action_id", Uuid(), ForeignKey("actions.id", ondelete="RESTRICT"), nullable=False),
    Column("action_revision", BigInteger(), nullable=False),
    Column("proposal_digest", String(64), nullable=False),
    Column("scope_digest", String(64), nullable=False),
    Column("policy_version", String(40), nullable=False),
    Column(
        "runtime_generation",
        Uuid(),
        ForeignKey("runtime_generations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("action_id"),
    CheckConstraint("grant_revision >= 1", name="grant_revision_positive"),
    CheckConstraint("action_revision >= 1", name="action_revision_positive"),
    CheckConstraint(_HEX_DIGEST_FORMAT, name="proposal_digest_format"),
    CheckConstraint("scope_digest ~ '^[0-9a-f]{64}$'", name="scope_digest_format"),
    CheckConstraint("expires_at > created_at", name="expires_after_creation"),
)

Index("ix_step_authorizations_grant_id", step_authorizations.c.grant_id)

RESEARCH_SESSION_STATUSES = ("OPEN", "CLOSED", "STALE")

#: One task-owned public browser context, reused across the task's steps. It is
#: bound to the worker generation that created it: after a worker or runtime
#: restart the native context is gone, so the row becomes STALE and every
#: semantic ref issued under it stops resolving. There is deliberately no
#: column for cookies, storage state or any imported profile -- an
#: unauthenticated context is the only kind this milestone can create.
research_sessions = Table(
    "research_sessions",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("grant_id", Uuid(), ForeignKey("task_grants.id", ondelete="RESTRICT"), nullable=False),
    Column(
        "worker_generation",
        Uuid(),
        ForeignKey("browser_worker_generations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "runtime_generation",
        Uuid(),
        ForeignKey("runtime_generations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status}'" for status in RESEARCH_SESSION_STATUSES) + ")",
        name="status",
    ),
    CheckConstraint("(closed_at IS NULL) = (status = 'OPEN')", name="closed_with_status"),
)

Index("ix_research_sessions_task_id", research_sessions.c.task_id)

#: One bounded, hashed observation per research step. `sequence` is allocated
#: per task and is the model-facing `o<n>` ref. `targets` holds the addresses
#: those refs resolve to; it is the controller's table and is never sent to a
#: model. No DOM, no cookies, no storage, no headers, no screenshots.
research_observations = Table(
    "research_observations",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("grant_id", Uuid(), ForeignKey("task_grants.id", ondelete="RESTRICT"), nullable=False),
    Column("action_id", Uuid(), ForeignKey("actions.id", ondelete="RESTRICT"), nullable=False),
    Column(
        "attempt_id", Uuid(), ForeignKey("action_attempts.id", ondelete="RESTRICT"), nullable=False
    ),
    Column(
        "dispatch_id",
        Uuid(),
        ForeignKey("browser_dispatches.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column(
        "session_id",
        Uuid(),
        ForeignKey("research_sessions.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column(
        "worker_generation",
        Uuid(),
        ForeignKey("browser_worker_generations.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("sequence", Integer(), nullable=False),
    Column("schema_version", Integer(), nullable=False),
    Column("provenance", String(32), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("operation", String(32), nullable=False),
    Column("tab", String(4), nullable=True),
    Column("document_epoch", Integer(), nullable=False),
    Column("query", String(200), nullable=True),
    Column("requested_url", String(2048), nullable=True),
    Column("final_url", String(2048), nullable=True),
    Column("final_host", String(253), nullable=True),
    Column("title", String(200), nullable=False),
    Column("settled", Boolean(), nullable=False),
    Column("truncated", Boolean(), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("projection", JSONB(), nullable=False),
    Column("targets", JSONB(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("attempt_id"),
    UniqueConstraint("task_id", "sequence"),
    CheckConstraint("schema_version = 1", name="schema_version"),
    CheckConstraint("provenance = 'untrusted_environment'", name="provenance"),
    CheckConstraint("sequence >= 1", name="sequence_positive"),
    CheckConstraint("document_epoch >= 1", name="document_epoch_positive"),
    CheckConstraint("kind IN ('page', 'search_results', 'tab_state')", name="kind"),
    CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_format"),
    CheckConstraint("jsonb_typeof(projection) = 'object'", name="projection_is_object"),
    CheckConstraint("jsonb_typeof(targets) = 'object'", name="targets_is_object"),
)

Index(
    "ix_research_observations_task_id_sequence",
    research_observations.c.task_id,
    research_observations.c.sequence,
)

RESEARCH_ANSWER_STATUSES = ("answered", "partial", "not_found", "not_verified")
RESEARCH_STOP_REASONS = (
    "goal_reached",
    "no_evidence",
    "budget_exhausted",
    "blocked",
    "planner_failed",
    "user_stopped",
    "outside_scope",
)

#: The one grounded answer a research task ends with, written once. Its
#: evidence references this task's own observations and blocks, verified before
#: the row exists.
research_answers = Table(
    "research_answers",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("grant_id", Uuid(), ForeignKey("task_grants.id", ondelete="RESTRICT"), nullable=False),
    Column("status", String(16), nullable=False),
    Column("stop_reason", String(24), nullable=False),
    Column("answer", JSONB(), nullable=False),
    Column("provider", String(16), nullable=False),
    Column("model", String(64), nullable=False),
    Column("steps_used", Integer(), nullable=False),
    Column("observations_used", Integer(), nullable=False),
    Column("planner_calls", Integer(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("task_id"),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status}'" for status in RESEARCH_ANSWER_STATUSES) + ")",
        name="status",
    ),
    CheckConstraint(
        "stop_reason IN (" + ", ".join(f"'{reason}'" for reason in RESEARCH_STOP_REASONS) + ")",
        name="stop_reason",
    ),
    CheckConstraint("jsonb_typeof(answer) = 'object'", name="answer_is_object"),
    CheckConstraint(
        "steps_used >= 0 AND observations_used >= 0 AND planner_calls >= 0", name="counters"
    ),
)


# ---- Milestone 8a S1: persistent browser profiles ---------------------------
#
# One row per Lumi-managed Chromium profile. The row is the profile's *identity
# and lifecycle*; its contents are Chromium's and stay in the profile
# directory. Read the right-hand column of this table as a promise:
#
#   stored here                     | never stored anywhere outside the profile
#   --------------------------------|------------------------------------------
#   id, label, site, allowed_origins| cookies of any kind
#   status, revision                | access, refresh, bearer or CSRF tokens
#   chromium/playwright/app version | localStorage, sessionStorage, IndexedDB
#   lease generation and expiry     | Chromium's credential database
#   revoke_epoch                    | the profile directory path
#   account fingerprint *hashes*    | the raw account identity string
#
# There is deliberately **no** column for a path, a cookie, a token or a
# storage blob, and no migration adds one later without this comment changing.

PROFILE_STATUSES = tuple(status.value for status in ProfileStatus)

browser_profiles = Table(
    "browser_profiles",
    metadata,
    Column("id", Uuid(), primary_key=True),
    # Shown to a person; bounded and sanitised by `canonical_label`. It is not
    # unique, is not a key, and never becomes a filesystem path component.
    Column("label", String(60), nullable=False),
    # The registrable domain (eTLD+1) from the pinned Public Suffix List.
    # Immutable after insert, enforced by a trigger: a profile holding site A's
    # session cookies must never start calling itself site B.
    Column("site", String(253), nullable=False),
    # The origins this profile may ever be navigated to. Derived from `site` at
    # creation and frozen with it.
    Column("allowed_origins", JSONB(), nullable=False),
    Column("status", String(16), nullable=False),
    # Compare-and-swap target for every mutation, as everywhere else.
    Column("revision", BigInteger(), nullable=False),
    # What last opened the directory. A Chromium profile is forward-compatible
    # only, so an older build refuses rather than risking corruption.
    Column("chromium_build", String(64), nullable=True),
    Column("playwright_version", String(32), nullable=True),
    Column("app_version", String(32), nullable=True),
    # The authoritative "one owner at a time" lease. Generation-bound and
    # expiring; the OS file handle in the profile directory is the independent
    # backstop that catches a second Lumi install with its own database.
    Column(
        "lease_runtime_generation",
        Uuid(),
        ForeignKey("runtime_generations.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    # Bumped when the account behind the profile is found to have changed.
    # S2/S3 will bump it; S1 only creates the column and starts it at 0.
    Column("revoke_epoch", BigInteger(), nullable=False, server_default=text("0")),
    # Hashes, never the identity string, and never defaulted to a fake value:
    # NULL means "not observed", and NULL must never satisfy a bound grant.
    # S1 adds the columns and writes neither -- observation is S2/S3's.
    Column("account_fingerprint", String(64), nullable=True),
    Column("account_label_hash", String(64), nullable=True),
    Column("last_login_completed_at", DateTime(timezone=True), nullable=True),
    Column("last_observed_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status}'" for status in PROFILE_STATUSES) + ")",
        name="status",
    ),
    CheckConstraint("revision >= 1", name="revision_positive"),
    CheckConstraint("revoke_epoch >= 0", name="revoke_epoch_positive"),
    CheckConstraint("length(label) BETWEEN 1 AND 60", name="label_length"),
    # A site is a registrable domain: at least two ASCII labels, lower case.
    CheckConstraint(
        r"site ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'",
        name="site_format",
    ),
    CheckConstraint("jsonb_typeof(allowed_origins) = 'array'", name="allowed_origins_is_array"),
    CheckConstraint("(deleted_at IS NOT NULL) = (status = 'DELETED')", name="deleted_at_set"),
    # A deleted profile holds no lease, and a lease always has an expiry.
    CheckConstraint(
        "(lease_runtime_generation IS NULL) = (lease_expires_at IS NULL)", name="lease_complete"
    ),
    CheckConstraint(
        "status <> 'DELETED' OR lease_runtime_generation IS NULL", name="deleted_holds_no_lease"
    ),
    CheckConstraint(
        "account_fingerprint IS NULL OR account_fingerprint ~ '^[0-9a-f]{64}$'",
        name="account_fingerprint_format",
    ),
    CheckConstraint(
        "account_label_hash IS NULL OR account_label_hash ~ '^[0-9a-f]{64}$'",
        name="account_label_hash_format",
    ),
)

#: One live profile per site. A second profile for `github.com` while the first
#: is alive would mean two directories holding the same site's cookies, and
#: "delete this profile" would stop being a meaningful action. Deleted rows are
#: excluded so the site becomes available again after a delete.
Index(
    "uq_browser_profiles_site_live",
    browser_profiles.c.site,
    unique=True,
    postgresql_where=browser_profiles.c.status != ProfileStatus.DELETED.value,
)


# ---- Milestone 8a S2: manual login and human takeover -----------------------
#
# One row per takeover: a bounded interval in which the human, not Lumi, owned
# the browser. This is not an authorization -- there is no `kind`, no `scope`,
# nothing a step could consume. It records only that the interval existed and
# how it ended, so a crash mid-login can never be silently read back as a
# successful sign-in. See `app/domain/login_takeover.py`.

LOGIN_ATTEMPT_STATUSES = tuple(status.value for status in LoginAttemptStatus)
_OPEN_LOGIN_ATTEMPT_STATUSES = ("OPEN", "UNCONFIRMED")

login_attempts = Table(
    "login_attempts",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("profile_id", Uuid(), ForeignKey("browser_profiles.id", ondelete="RESTRICT"), nullable=False),
    Column(
        "runtime_generation",
        Uuid(),
        ForeignKey("runtime_generations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # Set once the worker has opened the headed context for this attempt.
    # Null only for the brief window between the row being created and that
    # confirmation; a row that never gets one is exactly what "interrupted"
    # covers.
    Column(
        "worker_generation",
        Uuid(),
        ForeignKey("browser_worker_generations.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    # The profile's revision this attempt is bound to, captured at start. Not
    # a compare-and-swap target for the profile row -- that is `revision`
    # itself, checked by the service -- but a durable record of what the user
    # was looking at when they clicked "Sign in manually".
    Column("profile_revision", BigInteger(), nullable=False),
    Column("status", String(16), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status}'" for status in LOGIN_ATTEMPT_STATUSES) + ")",
        name="status",
    ),
    CheckConstraint("profile_revision >= 1", name="profile_revision_positive"),
    CheckConstraint("expires_at > started_at", name="expires_after_start"),
    CheckConstraint("(completed_at IS NOT NULL) = (status = 'COMPLETED')", name="completed_at_set"),
    CheckConstraint("(cancelled_at IS NOT NULL) = (status = 'CANCELLED')", name="cancelled_at_set"),
)

#: At most one live (OPEN or UNCONFIRMED) attempt per profile. A second
#: "Sign in manually" click while one is already active must be refused, not
#: silently start a second headed browser on the same profile directory.
Index(
    "uq_login_attempts_profile_id_open",
    login_attempts.c.profile_id,
    unique=True,
    postgresql_where=login_attempts.c.status.in_(_OPEN_LOGIN_ATTEMPT_STATUSES),
)
Index("ix_login_attempts_profile_id_started_at", login_attempts.c.profile_id, login_attempts.c.started_at)


# ---- Milestone 8a S3: account-private evidence -------------------------------
#
# Deliberately separate from `research_observations` and `research_answers`.
# `classification` is a CHECK that admits exactly one value, so the boundary is
# a property of the table and not of a WHERE clause somebody might forget. There
# is **no URL column**: an authenticated link can be a capability, so the
# address a ref means lives only in the worker's memory for one document epoch.
# Everything textual here is the *redacted* projection the provider received.

AUTHENTICATED_ANSWER_STATUSES = RESEARCH_ANSWER_STATUSES

authenticated_observations = Table(
    "authenticated_observations",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("grant_id", Uuid(), ForeignKey("task_grants.id", ondelete="RESTRICT"), nullable=False),
    Column(
        "profile_id", Uuid(), ForeignKey("browser_profiles.id", ondelete="CASCADE"), nullable=False
    ),
    Column("action_id", Uuid(), ForeignKey("actions.id", ondelete="RESTRICT"), nullable=False),
    Column(
        "attempt_id", Uuid(), ForeignKey("action_attempts.id", ondelete="RESTRICT"), nullable=False
    ),
    Column(
        "dispatch_id",
        Uuid(),
        ForeignKey("browser_dispatches.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column(
        "worker_generation",
        Uuid(),
        ForeignKey("browser_worker_generations.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("sequence", Integer(), nullable=False),
    Column("schema_version", Integer(), nullable=False),
    Column("classification", String(16), nullable=False),
    Column("provenance", String(32), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("operation", String(32), nullable=False),
    Column("tab", String(4), nullable=True),
    Column("document_epoch", Integer(), nullable=False),
    Column("host", String(253), nullable=True),
    Column("title", String(200), nullable=False),
    Column("settled", Boolean(), nullable=False),
    Column("truncated", Boolean(), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("projection", JSONB(), nullable=False),
    # Milestone 8b S4. A version-1 row is an S3 text/link observation and keeps
    # the column defaults (epoch 0, empty inventory); it is never rewritten.
    Column("form_epoch", Integer(), nullable=False, server_default=text("0")),
    Column(
        "element_inventory", JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("attempt_id"),
    UniqueConstraint("task_id", "sequence"),
    CheckConstraint("schema_version IN (1, 2)", name="schema_version"),
    CheckConstraint("form_epoch >= 0", name="form_epoch_nonnegative"),
    CheckConstraint(
        "jsonb_typeof(element_inventory) = 'object'", name="element_inventory_is_object"
    ),
    CheckConstraint(
        "schema_version <> 1 OR (form_epoch = 0 AND element_inventory = '{}'::jsonb)",
        name="v1_has_no_inventory",
    ),
    CheckConstraint("classification = 'account_private'", name="classification"),
    CheckConstraint("provenance = 'untrusted_environment'", name="provenance"),
    CheckConstraint("sequence >= 1", name="sequence_positive"),
    CheckConstraint("document_epoch >= 1", name="document_epoch_positive"),
    CheckConstraint("kind IN ('page', 'tab_state')", name="kind"),
    CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_format"),
    CheckConstraint("jsonb_typeof(projection) = 'object'", name="projection_is_object"),
)

Index(
    "ix_authenticated_observations_task_id_sequence",
    authenticated_observations.c.task_id,
    authenticated_observations.c.sequence,
)
Index("ix_authenticated_observations_profile_id", authenticated_observations.c.profile_id)

authenticated_answers = Table(
    "authenticated_answers",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("grant_id", Uuid(), ForeignKey("task_grants.id", ondelete="RESTRICT"), nullable=False),
    Column(
        "profile_id", Uuid(), ForeignKey("browser_profiles.id", ondelete="CASCADE"), nullable=False
    ),
    Column("classification", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("stop_reason", String(24), nullable=False),
    Column("answer", JSONB(), nullable=False),
    Column("provider", String(16), nullable=False),
    Column("model", String(64), nullable=False),
    Column("steps_used", Integer(), nullable=False),
    Column("observations_used", Integer(), nullable=False),
    Column("planner_calls", Integer(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("task_id"),
    CheckConstraint("classification = 'account_private'", name="classification"),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status}'" for status in AUTHENTICATED_ANSWER_STATUSES) + ")",
        name="status",
    ),
    CheckConstraint(
        "stop_reason IN (" + ", ".join(f"'{reason}'" for reason in RESEARCH_STOP_REASONS) + ")",
        name="stop_reason",
    ),
    CheckConstraint("jsonb_typeof(answer) = 'object'", name="answer_is_object"),
    CheckConstraint(
        "steps_used >= 0 AND observations_used >= 0 AND planner_calls >= 0", name="counters"
    ),
)

Index("ix_authenticated_answers_profile_id", authenticated_answers.c.profile_id)


#: Milestone 8b S5. The user's saved details, one row per closed kind. The value
#: is **plaintext task data in the local runtime database** -- not encrypted at
#: rest and not a credential -- and it never leaves this row in S5: the digest
#: and the masked preview are what every other surface sees. The database
#: refuses a digest that is not the SHA-256 of the stored value.
protected_values = Table(
    "protected_values",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("value", Text(), nullable=False),
    Column("value_digest", String(64), nullable=False),
    Column("preview", String(120), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("kind"),
    CheckConstraint(
        "kind IN (" + ", ".join(f"'{kind}'" for kind in PROTECTED_KINDS) + ")", name="kind"
    ),
    CheckConstraint("value_digest ~ '^[0-9a-f]{64}$'", name="value_digest_format"),
    CheckConstraint(
        "value_digest = encode(sha256(convert_to(value, 'UTF8')), 'hex')",
        name="digest_matches_value",
    ),
    CheckConstraint(
        "length(value) BETWEEN 1 AND 300", name="value_bounded"
    ),
    CheckConstraint("length(preview) >= 1", name="preview_present"),
)


#: Milestone 8b S6. What Lumi *prepared or attempted* in a form, never a restorable draft
#: (the page is browser-local and is lost on any restart). `fields` holds refs, identity
#: hashes, approved digests and verified-local-value hashes only -- never a raw value, a
#: selector or a locator description. At most one live (PREPARED / STALE) draft per
#: profile and per task.
form_drafts = Table(
    "form_drafts",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column(
        "profile_id", Uuid(), ForeignKey("browser_profiles.id", ondelete="CASCADE"), nullable=False
    ),
    Column("action_id", Uuid(), ForeignKey("actions.id", ondelete="RESTRICT"), nullable=False),
    Column(
        "attempt_id", Uuid(), ForeignKey("action_attempts.id", ondelete="RESTRICT"), nullable=False
    ),
    Column(
        "dispatch_id", Uuid(), ForeignKey("browser_dispatches.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("manifest_digest", String(64), nullable=False),
    Column("draft_digest", String(64), nullable=False),
    Column("observation_id", Uuid(), nullable=False),
    Column("tab", String(4), nullable=False),
    Column("document_epoch", Integer(), nullable=False),
    Column("form_epoch", Integer(), nullable=False),
    Column("form_ref", String(4), nullable=False),
    Column("status", String(16), nullable=False),
    Column("revision", Integer(), nullable=False, server_default=text("1")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("fields", JSONB(), nullable=False),
    UniqueConstraint("attempt_id"),
    UniqueConstraint("dispatch_id"),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status.value}'" for status in DraftStatus) + ")", name="status"
    ),
    CheckConstraint("revision >= 1", name="revision_positive"),
    CheckConstraint("manifest_digest ~ '^[0-9a-f]{64}$'", name="manifest_digest_format"),
    CheckConstraint("draft_digest ~ '^[0-9a-f]{64}$'", name="draft_digest_format"),
    CheckConstraint("tab ~ '^t[1-3]$'", name="tab_ref"),
    CheckConstraint("form_ref ~ '^f[1-5]$'", name="form_ref_shape"),
    CheckConstraint("document_epoch >= 1 AND form_epoch >= 1", name="epochs_positive"),
    CheckConstraint(
        "jsonb_typeof(fields) = 'array' AND jsonb_array_length(fields) <= 12", name="fields_bounded"
    ),
)

_LIVE_DRAFT = text("status IN ('PREPARED', 'STALE')")
Index("uq_form_drafts_profile_id_live", form_drafts.c.profile_id, unique=True, postgresql_where=_LIVE_DRAFT)
Index("uq_form_drafts_task_id_live", form_drafts.c.task_id, unique=True, postgresql_where=_LIVE_DRAFT)


#: Milestone 9 S1. Windows desktop observation, local and private.
#:
#: `desktop_worker_generations` mirrors `browser_worker_generations`: the identity of one
#: run of the isolated desktop worker, bound to the runtime generation that started it.
desktop_worker_generations = Table(
    "desktop_worker_generations",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column(
        "runtime_generation",
        Uuid(),
        ForeignKey("runtime_generations.id", ondelete="RESTRICT", name="fk_desktop_worker_generations_runtime_generation"),
        nullable=False,
    ),
    Column("worker_started_at", DateTime(timezone=True), nullable=False),
    Column("registered_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

#: The safe durable projection of one semantic observation. `snapshot` is the closed
#: `DesktopObservation` schema: roles, bounded names and text, and states. It has no column
#: or key for a window handle, a process id or path, a bounding rectangle, an
#: AutomationId, a class name or a password. Ownership is the worker generation (and through
#: it the runtime generation): there is no task association because no planner consumes
#: these observations yet, and nothing here is a provider-visible record.
desktop_observations = Table(
    "desktop_observations",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column(
        "worker_generation",
        Uuid(),
        ForeignKey("desktop_worker_generations.id", ondelete="RESTRICT", name="fk_desktop_observations_worker_generation"),
        nullable=False,
    ),
    Column("surface_ref", String(4), nullable=False),
    Column("surface_epoch", Integer(), nullable=False),
    Column("schema_version", Integer(), nullable=False),
    Column("classification", String(16), nullable=False),
    Column("snapshot", JSONB(), nullable=False),
    Column("snapshot_digest", String(64), nullable=False),
    Column("truncated", Boolean(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("classification = 'desktop_private'", name="classification"),
    CheckConstraint("surface_ref ~ '^s([1-9]|1[0-6])$'", name="surface_ref_shape"),
    CheckConstraint("surface_epoch >= 1", name="epoch_positive"),
    CheckConstraint("schema_version >= 1", name="schema_version_positive"),
    CheckConstraint("snapshot_digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
    CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="snapshot_object"),
)
Index("ix_desktop_observations_created_at", desktop_observations.c.created_at)


#: Milestone 9 S2. One row per disclosure of one exact desktop observation to one provider.
#:
#: A private snapshot leaving the machine is an external effect even though the desktop is not
#: changed, so it has durable history. `grant_id` is UNIQUE (and so is `task_id`): one approval funds
#: one claim, whatever races, and a retry is a new task with a new observation and a new approval.
#: The row is written, and the single-use grant consumed, in ONE transaction that commits *before* the
#: provider is called; a row still `STARTED` when the runtime dies becomes `OUTCOME_UNKNOWN`, never a
#: silent replay. `observation_id` is deliberately not a foreign key: the raw observation is retained
#: under S1's short window, while this audit row (ids, digests, counts, codes -- no desktop text) is not.
DISCLOSURE_STATUSES = ("STARTED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN")

desktop_disclosures = Table(
    "desktop_disclosures",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("grant_id", Uuid(), ForeignKey("task_grants.id", ondelete="RESTRICT"), nullable=False),
    Column("observation_id", Uuid(), nullable=False),
    Column("snapshot_digest", String(64), nullable=False),
    Column("recipient", String(16), nullable=False),
    Column("model", String(64), nullable=False),
    Column("projection_digest", String(64), nullable=False),
    Column("node_count", Integer(), nullable=False),
    Column("text_bytes", Integer(), nullable=False),
    Column("redaction_count", Integer(), nullable=False),
    Column("truncated", Boolean(), nullable=False),
    Column("status", String(20), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("error_code", String(40), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("grant_id", name="uq_desktop_disclosures_grant_id"),
    UniqueConstraint("task_id", name="uq_desktop_disclosures_task_id"),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status}'" for status in DISCLOSURE_STATUSES) + ")", name="status"
    ),
    CheckConstraint("snapshot_digest ~ '^[0-9a-f]{64}$'", name="snapshot_digest_format"),
    CheckConstraint("projection_digest ~ '^[0-9a-f]{64}$'", name="projection_digest_format"),
    CheckConstraint("node_count >= 0 AND text_bytes >= 0 AND redaction_count >= 0", name="counts_non_negative"),
    CheckConstraint("(status = 'STARTED') = (finished_at IS NULL)", name="finished_when_not_started"),
    CheckConstraint(
        "(status IN ('FAILED', 'OUTCOME_UNKNOWN')) = (error_code IS NOT NULL)", name="error_code_when_not_ok"
    ),
)

#: The private answer to one approved disclosure. It stays in the desktop-private domain: it may repeat
#: desktop text, so it is not memory, not conversation context and not shown to any other task. Evidence
#: holds the projection's own *redacted* text for each cited control ref, never the projected tree. It is
#: not pruned with the raw observation: it is a durable, private, user-facing result.
desktop_answers = Table(
    "desktop_answers",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column(
        "disclosure_id", Uuid(), ForeignKey("desktop_disclosures.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("observation_id", Uuid(), nullable=False),
    Column("classification", String(16), nullable=False),
    Column("recipient", String(16), nullable=False),
    Column("model", String(64), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("answer", String(1200), nullable=True),
    Column("reason", String(24), nullable=True),
    Column("evidence", JSONB(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("disclosure_id", name="uq_desktop_answers_disclosure_id"),
    CheckConstraint("classification = 'desktop_private'", name="classification"),
    CheckConstraint("kind IN ('answer', 'cannot_answer')", name="kind"),
    CheckConstraint("jsonb_typeof(evidence) = 'array'", name="evidence_is_array"),
    CheckConstraint(
        "(kind = 'answer') = (answer IS NOT NULL) AND (kind = 'cannot_answer') = (reason IS NOT NULL)",
        name="shape_matches_kind",
    ),
)


#: Milestone 9 S3/S4. One row per request to the desktop worker for ONE effect (focus, scroll, launch,
#: and -- S4 -- set-value, select, invoke), written and committed before the worker is called.
#: `attempt_id` is UNIQUE. Identifiers, opaque refs, digests and closed codes only: never a title,
#: control text, path, handle, coordinate or typed value.
DESKTOP_DISPATCH_OPERATIONS = (
    "focus_surface", "scroll_control", "launch_app",
    "set_control_value", "select_control", "invoke_control",
)
DESKTOP_DISPATCH_STATUSES = ("DISPATCHED", "OK", "FAILED_BEFORE_EFFECT", "OUTCOME_UNKNOWN")

desktop_dispatches = Table(
    "desktop_dispatches",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("action_id", Uuid(), ForeignKey("actions.id", ondelete="RESTRICT", name="fk_desktop_dispatches_action_id_actions"), nullable=False),
    Column(
        "attempt_id",
        Uuid(),
        ForeignKey("action_attempts.id", ondelete="RESTRICT", name="fk_desktop_dispatches_attempt_id_action_attempts"),
        nullable=False,
    ),
    Column(
        "worker_generation",
        Uuid(),
        ForeignKey(
            "desktop_worker_generations.id",
            ondelete="RESTRICT",
            name="fk_desktop_dispatches_worker_generation_desktop_worker_generations",
        ),
        nullable=False,
    ),
    Column("operation", String(24), nullable=False),
    Column("surface_ref", String(4), nullable=True),
    Column("surface_epoch", Integer(), nullable=True),
    Column("observation_id", Uuid(), nullable=True),
    Column("snapshot_digest", String(64), nullable=True),
    Column("control_ref", String(4), nullable=True),
    Column("app_id", String(32), nullable=True),
    Column("value_ref", String(4), nullable=True),
    Column("option_container_ref", String(4), nullable=True),
    Column("invoke_effect", String(32), nullable=True),
    Column("input_tick", BigInteger(), nullable=False),
    Column("status", String(24), nullable=False),
    Column("error_code", String(64), nullable=True),
    Column("result", JSONB(), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("attempt_id", name="uq_desktop_dispatches_attempt_id"),
    CheckConstraint("operation IN (" + ", ".join(f"'{v}'" for v in DESKTOP_DISPATCH_OPERATIONS) + ")", name="operation"),
    CheckConstraint("status IN (" + ", ".join(f"'{v}'" for v in DESKTOP_DISPATCH_STATUSES) + ")", name="status"),
    CheckConstraint("(finished_at IS NULL) = (status = 'DISPATCHED')", name="finished_matches_status"),
    CheckConstraint("surface_ref IS NULL OR surface_ref ~ '^s([1-9]|1[0-6])$'", name="surface_ref_shape"),
    CheckConstraint("control_ref IS NULL OR control_ref ~ '^u([1-9][0-9]?|1[0-9][0-9]|200)$'", name="control_ref_shape"),
    CheckConstraint("app_id IS NULL OR app_id ~ '^[a-z][a-z0-9_-]{0,31}$'", name="app_id_shape"),
    CheckConstraint("snapshot_digest IS NULL OR snapshot_digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
    CheckConstraint("input_tick >= 0 AND input_tick <= 4294967295", name="input_tick_range"),
    CheckConstraint("result IS NULL OR jsonb_typeof(result) = 'object'", name="result_object"),
    CheckConstraint("value_ref IS NULL OR value_ref ~ '^v([1-9]|10)$'", name="value_ref_shape"),
    CheckConstraint(
        "option_container_ref IS NULL OR option_container_ref ~ '^u([1-9][0-9]?|1[0-9][0-9]|200)$'",
        name="option_container_ref_shape",
    ),
    CheckConstraint("invoke_effect IS NULL OR invoke_effect IN ('name_toggle')", name="invoke_effect_shape"),
    CheckConstraint(
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
        "AND option_container_ref IS NULL)",
        name="operation_identity",
    ),
)
Index("ix_desktop_dispatches_action_id", desktop_dispatches.c.action_id)
Index(
    "ix_desktop_dispatches_in_flight",
    desktop_dispatches.c.status,
    postgresql_where=text("status = 'DISPATCHED'"),
)


#: Milestone 9 S4. One row per planning-disclosure attempt: an approved, redacted, bounded snapshot
#: (plus the trusted local value descriptors -- never raw values) shown to ONE provider, and the ONE
#: closed action it proposed back. `grant_id` and `task_id` are UNIQUE, exactly like
#: `desktop_disclosures`: one planning approval funds one claim. This is disclosure authority, not
#: execution authority: the proposed action still needs its own separate, exact approval on the
#: ordinary action ledger (a `DESKTOP_SET_VALUE` / `DESKTOP_SELECT` / `DESKTOP_INVOKE` action) before
#: any effect exists.
DESKTOP_PLAN_STATUSES = ("STARTED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN")

desktop_action_plans = Table(
    "desktop_action_plans",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("grant_id", Uuid(), ForeignKey("task_grants.id", ondelete="RESTRICT"), nullable=False),
    Column("observation_id", Uuid(), nullable=False),
    Column("snapshot_digest", String(64), nullable=False),
    Column("recipient", String(16), nullable=False),
    Column("model", String(64), nullable=False),
    Column("projection_digest", String(64), nullable=False),
    Column("node_count", Integer(), nullable=False),
    Column("text_bytes", Integer(), nullable=False),
    Column("redaction_count", Integer(), nullable=False),
    Column("truncated", Boolean(), nullable=False),
    Column("status", String(20), nullable=False),
    Column("proposed_action", JSONB(), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("error_code", String(40), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("grant_id", name="uq_desktop_action_plans_grant_id"),
    UniqueConstraint("task_id", name="uq_desktop_action_plans_task_id"),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status}'" for status in DESKTOP_PLAN_STATUSES) + ")", name="status"
    ),
    CheckConstraint("snapshot_digest ~ '^[0-9a-f]{64}$'", name="snapshot_digest_format"),
    CheckConstraint("projection_digest ~ '^[0-9a-f]{64}$'", name="projection_digest_format"),
    CheckConstraint("node_count >= 0 AND text_bytes >= 0 AND redaction_count >= 0", name="counts_non_negative"),
    CheckConstraint("(status = 'STARTED') = (finished_at IS NULL)", name="finished_when_not_started"),
    CheckConstraint(
        "(status IN ('FAILED', 'OUTCOME_UNKNOWN')) = (error_code IS NOT NULL)", name="error_code_when_not_ok"
    ),
    CheckConstraint("(status = 'SUCCEEDED') = (proposed_action IS NOT NULL)", name="proposal_when_succeeded"),
    CheckConstraint(
        "proposed_action IS NULL OR jsonb_typeof(proposed_action) = 'object'", name="proposal_is_object"
    ),
)


#: Milestone 9 S5. One row per `desktop_vision_capture` grant claim: a single scoped screenshot taken
#: for LOCAL use only (on-device display, local OCR) -- no provider is ever contacted for this grant
#: kind. Only identity/geometry digests, pixel dimensions and DPI are kept; the image itself never
#: reaches this table or any other durable store.
DESKTOP_CAPTURE_STATUSES = ("STARTED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN")

desktop_captures = Table(
    "desktop_captures",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("grant_id", Uuid(), ForeignKey("task_grants.id", ondelete="RESTRICT"), nullable=False),
    Column("surface_ref", String(4), nullable=False),
    Column("surface_epoch", Integer(), nullable=False),
    # The `GetLastInputInfo` tick read at CONFIRM time (the trusted click), stored so the later
    # claim compares against the approval moment itself, never a tick freshly read at claim time
    # (which would trivially always match "now" and detect nothing).
    Column("approval_input_tick", BigInteger(), nullable=False),
    Column("geometry_fingerprint", String(64), nullable=True),
    Column("frame_digest", String(64), nullable=True),
    Column("width", Integer(), nullable=True),
    Column("height", Integer(), nullable=True),
    Column("dpi", Integer(), nullable=True),
    Column("monitor_id", BigInteger(), nullable=True),
    Column("status", String(20), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("error_code", String(40), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("grant_id", name="uq_desktop_captures_grant_id"),
    UniqueConstraint("task_id", name="uq_desktop_captures_task_id"),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status}'" for status in DESKTOP_CAPTURE_STATUSES) + ")", name="status"
    ),
    CheckConstraint("surface_ref ~ '^s([1-9]|1[0-6])$'", name="surface_ref_shape"),
    CheckConstraint(
        "geometry_fingerprint IS NULL OR geometry_fingerprint ~ '^[0-9a-f]{64}$'",
        name="geometry_fingerprint_format",
    ),
    CheckConstraint("frame_digest IS NULL OR frame_digest ~ '^[0-9a-f]{64}$'", name="frame_digest_format"),
    CheckConstraint("(status = 'STARTED') = (finished_at IS NULL)", name="finished_when_not_started"),
    CheckConstraint(
        "(status IN ('FAILED', 'OUTCOME_UNKNOWN')) = (error_code IS NOT NULL)", name="error_code_when_not_ok"
    ),
    CheckConstraint(
        "(status = 'SUCCEEDED') = (geometry_fingerprint IS NOT NULL AND frame_digest IS NOT NULL "
        "AND width IS NOT NULL AND height IS NOT NULL AND dpi IS NOT NULL AND monitor_id IS NOT NULL)",
        name="frame_fields_when_succeeded",
    ),
    CheckConstraint("width IS NULL OR width > 0", name="width_positive"),
    CheckConstraint("height IS NULL OR height > 0", name="height_positive"),
    CheckConstraint("dpi IS NULL OR dpi > 0", name="dpi_positive"),
    CheckConstraint(
        "approval_input_tick >= 0 AND approval_input_tick <= 4294967295", name="approval_input_tick_range"
    ),
)


#: Milestone 9 S5. One row per `desktop_vision_disclose` grant claim: ONE freshly-recaptured image (not
#: the linked capture's own bytes, which were never persisted) sent to ONE named provider/model for ONE
#: stated purpose. `candidates` is the closed evidence list the provider returned -- never an
#: authorization, a coordinate or a click; nothing reads it as one.
desktop_vision_disclosures = Table(
    "desktop_vision_disclosures",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("grant_id", Uuid(), ForeignKey("task_grants.id", ondelete="RESTRICT"), nullable=False),
    Column("capture_id", Uuid(), ForeignKey("desktop_captures.id", ondelete="RESTRICT"), nullable=False),
    Column("provider", String(16), nullable=False),
    Column("model", String(64), nullable=False),
    Column("purpose", String(400), nullable=False),
    # The `GetLastInputInfo` tick read at CONFIRM time (the trusted click), same reasoning as
    # `desktop_captures.approval_input_tick`.
    Column("approval_input_tick", BigInteger(), nullable=False),
    Column("geometry_fingerprint", String(64), nullable=True),
    Column("frame_digest", String(64), nullable=True),
    Column("width", Integer(), nullable=True),
    Column("height", Integer(), nullable=True),
    Column("dpi", Integer(), nullable=True),
    Column("monitor_id", BigInteger(), nullable=True),
    Column("candidates", JSONB(), nullable=True),
    Column("status", String(20), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("error_code", String(40), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("grant_id", name="uq_desktop_vision_disclosures_grant_id"),
    UniqueConstraint("task_id", name="uq_desktop_vision_disclosures_task_id"),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{status}'" for status in DESKTOP_CAPTURE_STATUSES) + ")", name="status"
    ),
    CheckConstraint(
        "geometry_fingerprint IS NULL OR geometry_fingerprint ~ '^[0-9a-f]{64}$'",
        name="geometry_fingerprint_format",
    ),
    CheckConstraint("frame_digest IS NULL OR frame_digest ~ '^[0-9a-f]{64}$'", name="frame_digest_format"),
    CheckConstraint("(status = 'STARTED') = (finished_at IS NULL)", name="finished_when_not_started"),
    CheckConstraint(
        "(status IN ('FAILED', 'OUTCOME_UNKNOWN')) = (error_code IS NOT NULL)", name="error_code_when_not_ok"
    ),
    CheckConstraint("candidates IS NULL OR jsonb_typeof(candidates) = 'array'", name="candidates_is_array"),
    CheckConstraint("(status = 'SUCCEEDED') = (candidates IS NOT NULL)", name="candidates_when_succeeded"),
    CheckConstraint(
        "approval_input_tick >= 0 AND approval_input_tick <= 4294967295", name="approval_input_tick_range"
    ),
)


# --- Milestone 10 S1: approved documents and the file broker -------------------------------------

#: An M10 file root: a folder the person chose in a native dialog, with EXPLICIT permissions. Search
#: approval (Electron main's legacy approved folders) does not imply any of these. `canonical_path` is
#: controller-local: no route, event or log ever returns it. `(volume_serial, dir_index)` is the
#: directory's identity; a replaced or re-pointed folder is not the approved root. `can_modify` exists
#: so the capability is represented, and is fixed false in M10 (no operation consumes it).
file_roots = Table(
    "file_roots",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("label", String(64), nullable=False),
    Column("canonical_path", Text(), nullable=False),
    #: Case-folded canonical path, for "one live root per folder".
    Column("path_key", Text(), nullable=False),
    Column("volume_serial", BigInteger(), nullable=False),
    Column("dir_index", String(20), nullable=False),
    Column("can_read", Boolean(), nullable=False),
    Column("can_create", Boolean(), nullable=False),
    Column("can_modify", Boolean(), nullable=False, server_default=false()),
    Column("status", String(16), nullable=False),
    Column("revision", BigInteger(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="status"),
    CheckConstraint("(revoked_at IS NOT NULL) = (status = 'REVOKED')", name="revoked_at_set"),
    CheckConstraint("can_modify = false", name="no_modify_in_m10"),
    CheckConstraint("can_read OR can_create", name="some_permission"),
    CheckConstraint("dir_index ~ '^[0-9]{1,20}$'", name="dir_index_format"),
    CheckConstraint("revision >= 1", name="revision_positive"),
    CheckConstraint("length(label) BETWEEN 1 AND 64", name="label_present"),
)

Index(
    "uq_file_roots_path_key_active",
    file_roots.c.path_key,
    unique=True,
    postgresql_where=text("status = 'ACTIVE'"),
)

#: One task-owned file authority. A ROOT_FILE is a root plus a validated relative name; a DROPPED_FILE is
#: exactly the one file main handed over (never its folder). Both bind the file's identity and content
#: hash at the moment it was added, so a different file under the same name is not this ref. Paths are
#: controller-local; the API exposes only the id, the display name and the root-relative name.
file_refs = Table(
    "file_refs",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("source", String(16), nullable=False),
    Column("root_id", Uuid(), ForeignKey("file_roots.id", ondelete="RESTRICT"), nullable=True),
    Column("relative_path", Text(), nullable=True),
    Column("local_path", Text(), nullable=True),
    Column("display_name", String(255), nullable=False),
    Column("format", String(8), nullable=False),
    Column("volume_serial", BigInteger(), nullable=False),
    Column("file_index", String(20), nullable=False),
    Column("size_bytes", BigInteger(), nullable=False),
    Column("mtime_ns", BigInteger(), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("source IN ('ROOT_FILE', 'DROPPED_FILE')", name="source"),
    CheckConstraint(
        "(source = 'ROOT_FILE' AND root_id IS NOT NULL AND relative_path IS NOT NULL AND local_path IS NULL) "
        "OR (source = 'DROPPED_FILE' AND root_id IS NULL AND relative_path IS NULL AND local_path IS NOT NULL)",
        name="source_shape",
    ),
    CheckConstraint("format IN ('pdf', 'docx', 'txt')", name="format"),
    CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
    CheckConstraint("file_index ~ '^[0-9]{1,20}$'", name="file_index_format"),
    CheckConstraint("size_bytes >= 0", name="size_non_negative"),
)

Index("ix_file_refs_task_id", file_refs.c.task_id)

#: Extracted text: private (`document_private`) and untrusted, task-owned, kept at most a day. After
#: `expires_at` the text is purged (NULL) and only its digest and counts remain.
documents = Table(
    "documents",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("file_ref_id", Uuid(), ForeignKey("file_refs.id", ondelete="RESTRICT"), nullable=False),
    Column("format", String(8), nullable=False),
    Column("page_count", Integer(), nullable=True),
    Column("text", Text(), nullable=True),
    Column("text_sha256", String(64), nullable=False),
    Column("text_chars", Integer(), nullable=False),
    Column("truncated", Boolean(), nullable=False),
    Column("flags", JSONB(), nullable=False),
    Column("classification", String(24), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("purged_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("file_ref_id", name="uq_documents_file_ref_id"),
    CheckConstraint("classification = 'document_private'", name="classification"),
    CheckConstraint("format IN ('pdf', 'docx', 'txt')", name="format"),
    CheckConstraint("text_sha256 ~ '^[0-9a-f]{64}$'", name="text_sha256_format"),
    CheckConstraint("(purged_at IS NULL) = (text IS NOT NULL)", name="purged_means_no_text"),
    CheckConstraint("text IS NULL OR length(text) <= 120000", name="text_bounded"),
    CheckConstraint("jsonb_typeof(flags) = 'object'", name="flags_is_object"),
    CheckConstraint("expires_at > created_at", name="expires_after_creation"),
)

Index("ix_documents_task_id", documents.c.task_id)

#: One row per claimed `document_disclose` grant: ONE provider call. UNIQUE on the grant and the task.
document_disclosures = Table(
    "document_disclosures",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("grant_id", Uuid(), ForeignKey("task_grants.id", ondelete="RESTRICT"), nullable=False),
    Column("recipient", String(16), nullable=False),
    Column("model", String(64), nullable=False),
    Column("projection_digest", String(64), nullable=False),
    Column("document_count", Integer(), nullable=False),
    Column("text_bytes", Integer(), nullable=False),
    Column("redaction_count", Integer(), nullable=False),
    Column("truncated", Boolean(), nullable=False),
    Column("status", String(20), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("error_code", String(40), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("grant_id", name="uq_document_disclosures_grant_id"),
    UniqueConstraint("task_id", name="uq_document_disclosures_task_id"),
    CheckConstraint("status IN ('STARTED', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN')", name="status"),
    CheckConstraint("projection_digest ~ '^[0-9a-f]{64}$'", name="projection_digest_format"),
    CheckConstraint(
        "document_count BETWEEN 1 AND 2 AND text_bytes >= 0 AND redaction_count >= 0", name="counts_bounded"
    ),
    CheckConstraint("(status = 'STARTED') = (finished_at IS NULL)", name="finished_when_not_started"),
    CheckConstraint(
        "(status IN ('FAILED', 'OUTCOME_UNKNOWN')) = (error_code IS NOT NULL)", name="error_code_when_not_ok"
    ),
)

#: The private, grounded comparison a disclosure produced. Stored evidence is the projection's own text.
document_answers = Table(
    "document_answers",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("task_id", Uuid(), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column(
        "disclosure_id", Uuid(), ForeignKey("document_disclosures.id", ondelete="RESTRICT"), nullable=False
    ),
    Column("classification", String(24), nullable=False),
    Column("recipient", String(16), nullable=False),
    Column("model", String(64), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("summary", String(800), nullable=True),
    Column("reason", String(24), nullable=True),
    Column("findings", JSONB(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("disclosure_id", name="uq_document_answers_disclosure_id"),
    CheckConstraint("classification = 'document_private'", name="classification"),
    CheckConstraint("kind IN ('comparison', 'cannot_compare')", name="kind"),
    CheckConstraint("jsonb_typeof(findings) = 'array'", name="findings_is_array"),
    CheckConstraint(
        "(kind = 'comparison') = (summary IS NOT NULL) AND (kind = 'cannot_compare') = (reason IS NOT NULL)",
        name="shape_matches_kind",
    ),
)
