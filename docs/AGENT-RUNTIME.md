# Agent task runtime

Milestone 1 of evolving Lumi into a voice, vision, browser and memory agent: a
durable task record. Setup and API details are in
[`services/agent/README.md`](../services/agent/README.md).

## Shape

```text
Electron app (unchanged)          services/agent (Python sidecar)
  renderer ─ preload ─ main          FastAPI ─ TaskService ─ TaskRepository ─ PostgreSQL
                                                                              (infra/docker-compose.yml)
```

- One modular Python application, not a set of services. PostgreSQL is the only
  source of truth; there is no Redis, queue or in-memory task state.
- `app/api` validates HTTP input and maps domain errors to responses.
- `app/services/tasks.py` owns every transaction. Each use case is one short
  transaction with no external I/O inside it. Future external work (browser,
  model calls) must happen between transactions, never inside one.
- `app/repositories/tasks.py` holds the SQL and never commits.
- `app/domain` holds the status enum and transition rules, with no I/O.
- Domain types live in `app/domain`, not `app/models`, because the repository's
  root `.gitignore` ignores every `models/` directory (the vision model cache).
- SQLAlchemy Core with `AsyncConnection` is used instead of the ORM session: the
  task row is updated with compare-and-swap `UPDATE ... RETURNING`, and there is
  no identity map to keep in sync.

## Data model (migration `0001`)

`tasks`: `id` (UUID), `status`, `revision`, `last_event_sequence`, `request`
(JSONB object), `created_at`, `updated_at`.

`task_events`: `id` (identity), `task_id` (FK, `RESTRICT`), `sequence`,
`task_revision`, `event_type`, `payload` (JSONB object), `created_at`;
unique `(task_id, sequence)`.

### Invariants

- Creating a task and its `task.created` event is one transaction.
- Every state change is one transaction that updates the task and appends
  exactly one event.
- `revision` starts at 1 and every mutation is
  `UPDATE ... WHERE id = :id AND revision = :expected`, setting `revision + 1`.
  Zero affected rows means a stale writer, never a silent overwrite. Future
  workers and approvals can pass the revision they acted on.
- `sequence` comes from `tasks.last_event_sequence`, bumped in that same
  UPDATE. The row lock therefore orders event appends per task, and the unique
  constraint is a backstop.
- `status` is text with a CHECK constraint, not a native PostgreSQL enum. Adding
  `WAITING_APPROVAL`, `OUTCOME_UNKNOWN`, `RECONCILING` and the other planned
  states means replacing that constraint in a migration. A test fails if the
  Python enum and the constraint drift apart.
- The runtime never calls `metadata.create_all`. At startup it checks that the
  database is at the Alembic head and refuses to serve otherwise.

## Security direction

These hold now and constrain later milestones:

- The renderer never receives `DATABASE_URL`, provider secrets or a direct route
  to this runtime. When a bridge is added, Electron main is the only client.
  It calls the runtime on loopback and exposes narrow, typed, validated IPC
  methods through the existing `window.lifeLens` bridge.
- The runtime has no shell, desktop, file or network-automation capability.
- Future model output is stored as a proposal. Approval and execution of
  consequential actions stay in trusted application code, following the rules
  in `DECISIONS.md` under "Action safety" and "Confirmation and provenance".
- PostgreSQL binds to `127.0.0.1` only. Credentials live in ignored `.env` files.

## Not in this milestone

There is no Electron to runtime bridge or process management yet.
`PendingActionStore` is still in memory and `LocalStore` is unchanged. There is
no persistent approval or action ledger, Playwright, browser agent, voice
change, long-term memory, embeddings, LLM planning or evaluation harness.
