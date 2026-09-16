# Agent task runtime

Evolving Lumi into a voice, vision, browser and memory agent. Milestone 1 made a
task durable. Milestone 2 makes *acting* durable: a ledger of proposed actions,
durable approvals, execution attempts, and an honest answer when the runtime
dies mid-action. Setup and API details are in
[`services/agent/README.md`](../services/agent/README.md).

## Shape

```text
Electron app (unchanged)          services/agent (Python sidecar)
  renderer ─ preload ─ main          FastAPI ─ services ─ repositories ─ PostgreSQL
                                                                        (infra/docker-compose.yml)
```

- One modular Python application, not a set of services. PostgreSQL is the only
  source of truth; there is no Redis, queue or in-memory task state.
- `app/api` validates HTTP input and maps domain errors to responses.
- `app/services` owns every transaction. Each use case is one short transaction
  with no external I/O inside it. Future external work (browser, model calls)
  must happen between transactions, never inside one.
- `app/repositories` holds the SQL and never commits.
- `app/domain` holds status enums, transition rules and the proposal digest,
  with no I/O.
- Domain types live in `app/domain`, not `app/models`, because the repository's
  root `.gitignore` ignores every `models/` directory (the vision model cache).
- SQLAlchemy Core with `AsyncConnection` is used instead of the ORM session:
  rows are updated with compare-and-swap `UPDATE ... RETURNING`, and there is no
  identity map to keep in sync.

## The rule this milestone exists for

> `FAILED` means Lumi **knows** the operation did not happen.
>
> `OUTCOME_UNKNOWN` means Lumi **does not know** whether the side effect
> occurred.

These must never be treated as equivalent. Collapsing them is how an agent
double-books an appointment or pays twice: it calls a lost response a failure
and retries. Once a consequential action might have reached the outside world,
Lumi never retries it merely because the process crashed or the response was
lost. The only way out of `OUTCOME_UNKNOWN` is authoritative reconciliation.

## Data model

### `tasks` and `task_events` (migration `0001`)

`tasks`: `id` (UUID), `status`, `revision`, `last_event_sequence`, `request`
(JSONB object), `created_at`, `updated_at`.

`task_events`: `id` (identity), `task_id` (FK, `RESTRICT`), `sequence`,
`task_revision`, `event_type`, `payload` (JSONB object), `created_at`;
unique `(task_id, sequence)`.

Migration `0002` widens the task status CHECK constraint with
`WAITING_APPROVAL`, `OUTCOME_UNKNOWN` and `RECONCILING`. `WAITING_CONTEXT`,
`WAITING_INPUT`, `WAITING_USER_AUTH` and `RECOVERING` are planned but not
implemented; each will be another constraint replacement.

### The action ledger (migration `0002`)

`actions`: `id` (UUID), `task_id` (FK), `idempotency_key`, `tool_name`,
`risk_tier`, `proposal` (JSONB object), `proposal_digest`, `status`, `revision`,
`created_at`, `updated_at`; unique `(task_id, idempotency_key)`.

`approvals`: `id`, `action_id` (FK), `action_revision`, `proposal_digest`,
`status`, `created_at`, `expires_at`, `approved_at`, `rejected_at`,
`consumed_at`.

`action_attempts`: `id`, `action_id` (FK), `attempt_number`, `approval_id` (FK,
unique), `runtime_generation` (FK), `started_at`, `finished_at`, `outcome`,
`result` (JSONB), `error_code`, `created_at`; unique
`(action_id, attempt_number)`.

`runtime_generations`: `id`, `started_at`. One row per runtime process.

There is no separate action event table. Action lifecycle changes are recorded
on the existing per-task timeline (`action.proposed`, `action.approved`,
`action.execution_started`, `action.outcome_unknown`, `action.reconciled`, ...),
because the future Lumi UI needs one ordered story per task, not two timelines
to interleave. A separate table would buy nothing here and would cost the
single-sequence ordering that `tasks.last_event_sequence` already guarantees.

## Action state machine

```text
PROPOSED ──> WAITING_APPROVAL ──> APPROVED ──> EXECUTING ──┬──> SUCCEEDED
    │               │                 │                     ├──> FAILED
    └───────────────┴─────────────────┴──> REJECTED         └──> OUTCOME_UNKNOWN
                                                                       │
                                                        ┌──────────────┘
                                                        v
                                                   RECONCILING ──┬──> SUCCEEDED
                                                        ^        ├──> FAILED
                                                        └────────┴──  OUTCOME_UNKNOWN
```

`SUCCEEDED`, `FAILED` and `REJECTED` are terminal. `OUTCOME_UNKNOWN` has exactly
one outgoing edge, to `RECONCILING`: no edge back to `EXECUTING` (a blind retry)
and none to `FAILED` (a claim Lumi cannot support).

The owning task mirrors the action while it is unresolved (`WAITING_APPROVAL`,
`EXECUTING`, `OUTCOME_UNKNOWN`, `RECONCILING`). A resolved action returns the
task to `READY` so a future planner can continue. **An action succeeding does
not make the task `SUCCEEDED`.**

## Proposal immutability and the digest

`proposal` is the exact proposed action and never changes after creation. A
`BEFORE UPDATE` trigger refuses any statement that would alter `proposal`,
`proposal_digest`, `tool_name`, `risk_tier`, `idempotency_key` or `task_id`, and
refuses a `revision` that does not move forward. This is a database guarantee,
not a convention: a proposal cannot be swapped underneath an approval the user
already granted.

`proposal_digest` is SHA-256 over a canonical JSON encoding (keys sorted, no
insignificant whitespace, non-ASCII kept as characters), so two proposals with
the same meaning always produce the same digest regardless of key order or the
caller's escaping. **The server always computes it.** The API has no digest
field; supplying one is a `422`. A caller-supplied digest would let an approval
be bound to bytes the server never stored.

## Idempotency

`(task_id, idempotency_key)` is unique.

| Request | Result |
| --- | --- |
| New key | `201`, action created in `PROPOSED`, one `action.proposed` event |
| Same key, same proposal | `200`, the stored action, nothing written |
| Same key, different proposal or tool | `409 action_proposal_conflict`, nothing changed |

The conflict is deliberate. Returning the stored action would answer a question
the caller did not ask; replacing the proposal would invalidate an approval the
user may already have granted. This protects against duplicate LLM/tool
proposals and against reconnect and retry storms.

## Approval model

Approval is a durable record, not a boolean on the action, because a boolean
cannot expire, cannot be single-use, and cannot say *what* was approved.

Every approval is bound to:

- **one exact action** (`action_id`);
- **the exact proposal** (`proposal_digest`);
- **one action revision** (`action_revision`, re-bound at grant time to the
  revision the grant produced, so any later change to the action unbinds it);
- **a deadline** (`expires_at`, from the configurable `APPROVAL_TTL_SECONDS`).

It is **single-use for execution**: claiming it sets `CONSUMED`. A partial
unique index allows at most one `PENDING`/`APPROVED` approval per action, so
"one live approval" is a database fact. `action_attempts.approval_id` is unique,
so one approval can never fund two attempts.

No caller may approve an arbitrary proposal payload. `POST
/actions/{id}/approve` carries no proposal: the server reconstructs everything
from persisted action state. Expired and rejected approvals are kept for audit,
including the `approved_at` of a grant that was later withdrawn.

Expiry is enforced by the statement that claims the approval, not by a cleanup
job, so an approval is unusable the moment it expires even if nothing has swept
it.

### Risk tiers

`risk_tier` (`R0` local read, `R1` scoped read/preparation, `R2`
disclosure/consequential, `R3` financial/destructive/security-sensitive) is
persisted for the policy engine, which is a later milestone. In this milestone
`requires_approval()` returns `True` for every tier: until a real policy engine
exists, the fail-safe answer is the only correct one, and nothing executes
without an explicit durable approval.

## Attempt lifecycle

An attempt separates *"I intended to execute"* from *"I know what happened"*.

Starting one is a single transaction that:

1. verifies the action is `APPROVED`,
2. claims the approval — a single `UPDATE ... WHERE status = 'APPROVED' AND
   action_revision = ... AND proposal_digest = ... AND expires_at > now()`, so
   the database, not an earlier read, decides validity, expiry and binding,
3. moves the action to `EXECUTING`,
4. inserts the attempt with its `attempt_number` and `runtime_generation`,
5. moves the owning task and appends `action.execution_started`.

Only after that commits may an external executor act. **There is no external
executor in this milestone**; the endpoint persists the intent and stops.

A partial unique index allows at most one unfinished attempt per action, so a
duplicate execution start can never produce a second in-flight side effect even
if application logic slips.

`finish_attempt` records `SUCCEEDED`, `FAILED` or `OUTCOME_UNKNOWN`. The last is
how an executor says it lost the response; it is not a failure.

**`OUTCOME_UNKNOWN` never creates a new attempt automatically.** An action can
in principle have more than one attempt, but only a deliberate future decision
can create one, never recovery and never reconciliation.

## `OUTCOME_UNKNOWN` recovery

The crash this milestone is built around:

```text
action APPROVED ──> attempt persisted ──> action EXECUTING ──> process dies
```

On restart the database says an attempt started and never finished. Nothing in
it can say whether the side effect reached the outside world. So startup, before
serving any request:

1. registers a new `runtime_generations` row;
2. finds unfinished attempts whose `runtime_generation` is **not** the current
   one;
3. for each, in its own short transaction: finishes the attempt with outcome
   `OUTCOME_UNKNOWN` and `error_code = runtime_restart`, moves the action to
   `OUTCOME_UNKNOWN`, moves the task to `OUTCOME_UNKNOWN`, and appends
   `action.outcome_unknown`.

It does not become `FAILED`, does not return to `APPROVED`, does not execute
again, and no second attempt is created. Running the pass before the server
accepts traffic means no request can ever observe a dead process's work as still
`EXECUTING`.

Ownership is decided by generation, not by a timeout, so the current process's
own healthy in-flight work is never mistaken for stale work. This is correct
while one runtime runs at a time, which is the current architecture. When
workers are added, the generation check becomes an expired lease (the
`runtime_generations` table is where heartbeat and expiry columns go) and
nothing else has to change.

## Reconciliation

The only exit from `OUTCOME_UNKNOWN`. `begin_reconciliation` moves the action to
`RECONCILING`; `finish_reconciliation` records the authoritative answer:

| Result | Action | Task |
| --- | --- | --- |
| `SUCCEEDED` | `SUCCEEDED` | `READY` |
| `FAILED` | `FAILED` | `READY` |
| `OUTCOME_UNKNOWN` | `OUTCOME_UNKNOWN` | `OUTCOME_UNKNOWN` |

An inconclusive reconciliation is recorded honestly and may be repeated. It is
never downgraded to `FAILED` just because looking did not settle the question.

Reconciliation **looks; it never acts**. It does not start a second attempt.
Later browser adapters will answer the question by reading authoritative state
(`lookup_booking()`, `lookup_submission()`). In this milestone the result is
supplied synthetically by tests.

## Transactional invariants

- Creating a task and its `task.created` event is one transaction.
- Proposal creation and its `action.proposed` event are one transaction.
- Approval request, grant and rejection each mutate the approval, the action and
  the task, and append their event, in one transaction.
- Attempt start, approval consumption, the action and task moves and the event
  are one transaction.
- Attempt finish, the action and task transitions and the event are one
  transaction.
- Every reconciliation transition and its event are one transaction.
- Every action mutation first takes the owning task's row lock
  (`SELECT ... FOR UPDATE`), so all work on one task runs in a single order.
  Duplicate approvals and execution starts serialize rather than race, event
  sequences stay ordered, and lock ordering is uniform so there is no deadlock.
- Every mutation is compare-and-swap on `revision`, setting `revision + 1`. Zero
  affected rows means a stale writer, never a silent overwrite.
- `sequence` comes from `tasks.last_event_sequence`, bumped in the same UPDATE,
  so the task row lock orders event appends and the unique constraint is a
  backstop. Exactly one event per transaction.
- No external I/O happens inside any transaction.
- The runtime never calls `metadata.create_all`. At startup it checks the
  database is at the Alembic head and refuses to serve otherwise. Tests fail if
  a Python enum and its CHECK constraint drift apart.

## Events

`task.created`, `task.cancelled`, `action.proposed`,
`action.approval_requested`, `action.approved`, `action.rejected`,
`action.execution_started`, `action.succeeded`, `action.failed`,
`action.outcome_unknown`, `action.reconciliation_started`, `action.reconciled`.

Payloads carry IDs, `tool_name`, `risk_tier`, `proposal_digest`, action status
and revision, and where relevant the attempt, approval and a `reason`. They
never carry the proposal body: the digest identifies it exactly, and the
timeline is the surface most likely to be shown, logged or exported. No secrets
go into events.

## Security direction

These hold now and constrain later milestones:

- The renderer never receives `DATABASE_URL`, provider secrets or a direct route
  to this runtime. When a bridge is added, Electron main is the only client. It
  calls the runtime on loopback and exposes narrow, typed, validated IPC methods
  through the existing `window.lifeLens` bridge.
- Approval is a server-side record reconstructed from persisted state. No caller
  hands in a proposal to be approved, and no caller supplies a digest. Today the
  FastAPI test API stands in for the approving UI; later Electron main is the
  trusted UI and security broker, and the approve/reject surface moves behind it.
- The execution and reconciliation routes are internal scaffolding for tests and
  for the future trusted Electron/browser integration. They are not a public API
  and must not be exposed beyond loopback.
- The runtime has no shell, desktop, file or network-automation capability.
- PostgreSQL binds to `127.0.0.1` only. Credentials live in ignored `.env` files.

## Not in this milestone

The Electron `PendingActionStore` is unchanged and still in memory, deliberately:
this Python ledger establishes the durable execution model first, and a second
competing implementation inside Electron would be worse than none. There is no
Electron-to-runtime bridge or process management yet, and `LocalStore` is
unchanged.

What remains synthetic: nothing executes. `start_attempt` persists intent and
stops, attempt results are supplied by the caller, and reconciliation results
are supplied by tests. There is no Playwright, Chromium, browser automation,
external network action, real appointment website, LLM planning, long-term
memory, embeddings, pgvector, Redis, voice change or evaluation harness.
