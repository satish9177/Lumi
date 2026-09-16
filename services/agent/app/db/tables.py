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
    UniqueConstraint,
    Uuid,
    false,
    func,
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
    | type[BrowserEffect],
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
    # two attempts even if application logic slips.
    Column("approval_id", Uuid(), ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=False),
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
    Column("status", String(32), nullable=False),
    Column("submitted", Boolean(), nullable=False, server_default=false()),
    Column("observation_id", Uuid(), nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("duration_ms", Integer(), nullable=True),
    Column("result", JSONB(), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
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
