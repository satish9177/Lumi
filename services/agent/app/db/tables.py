from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

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

_status_values = ", ".join(f"'{status.value}'" for status in TaskStatus)

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
    CheckConstraint(f"status IN ({_status_values})", name="status"),
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
