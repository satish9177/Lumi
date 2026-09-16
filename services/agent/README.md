# Lumi agent runtime

A Python sidecar that stores Lumi agent tasks, their event history, and a
durable ledger of proposed, approved and attempted actions in PostgreSQL.

Milestone 1 made a task durable. Milestone 2 makes acting durable: actions,
approvals and execution attempts survive a hard kill, and an execution that was
interrupted mid-flight becomes `OUTCOME_UNKNOWN` rather than being guessed at or
retried. It does no planning, runs no LLM or browser, and **executes nothing**;
starting an attempt persists the intent to act and stops. See
[`docs/AGENT-RUNTIME.md`](../../docs/AGENT-RUNTIME.md).

## Setup (Windows, PowerShell)

Prerequisites: Docker Desktop, [uv](https://docs.astral.sh/uv/).

```powershell
# 1. Development PostgreSQL (loopback only)
Copy-Item infra\.env.example infra\.env          # then set LUMI_POSTGRES_PASSWORD
docker compose -f infra\docker-compose.yml up -d --wait

# 2. Runtime configuration
cd services\agent
Copy-Item .env.example .env                       # use the same password
uv sync

# 3. Schema (the runtime never creates tables itself)
uv run alembic upgrade head

# 4. Run
uv run uvicorn --factory app.main:create_app --host 127.0.0.1 --port 8765
```

The runtime refuses to start if `DATABASE_URL` is missing or invalid, the
database is unreachable, or the schema is not at the Alembic head. On startup it
also recovers unfinished execution attempts left by a previous process (see
[Crash recovery](#crash-recovery)) before it accepts any request.

`infra/postgres/init` creates `lumi_agent_test` only when the data volume is
first initialized. For an existing volume, create it manually.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | — | Required. Must use `postgresql+asyncpg://`. |
| `DATABASE_POOL_SIZE` | `5` | Connection pool size. |
| `DATABASE_CONNECT_TIMEOUT_SECONDS` | `5.0` | Connection timeout. |
| `APPROVAL_TTL_SECONDS` | `300` | How long a granted approval may be claimed. |
| `TEST_DATABASE_URL` | — | Test suite only; must name a database ending in `_test`. |

## Checks

```powershell
uv run pytest        # needs TEST_DATABASE_URL (a database ending in _test)
uv run mypy          # strict
```

The suite includes a real hard-kill acceptance test: it starts the runtime as a
subprocess, drives an action to `EXECUTING`, kills the process, starts a fresh
one, and asserts the action recovered as `OUTCOME_UNKNOWN` with exactly one
execution attempt.

## Concepts

- **Action** — an exact proposed operation. `proposal` is immutable after
  creation (enforced by a database trigger) and identified by a server-computed
  SHA-256 `proposal_digest` over canonical JSON.
- **Approval** — a durable, expiring, single-use record bound to one action, one
  proposal digest and one action revision. Not a boolean.
- **Attempt** — one recorded intent to execute, claiming exactly one approval.
  Separates "I intended to execute" from "I know what happened".
- **`OUTCOME_UNKNOWN`** — Lumi does not know whether the side effect occurred.
  Never the same as `FAILED`, and never retried automatically.
- **Reconciliation** — the only way out of `OUTCOME_UNKNOWN`. It reads
  authoritative state; it never acts again.

## API

### Tasks

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/health` | `200 {"status":"ok","database":"ok"}`, or `503` when the database is unreachable |
| `POST` | `/tasks` | `201` task in `CREATED`, revision 1, with a `task.created` event |
| `GET` | `/tasks/{id}` | `200` task, `404 task_not_found`, `422` malformed UUID |
| `GET` | `/tasks/{id}/events?after_sequence=0&limit=100` | `200` events ordered by `sequence` |
| `POST` | `/tasks/{id}/cancel` | see cancellation rules |

`POST /tasks` body: `{"request": {"type": "appointment_search", "text": "..."}}`.
`type` is required (`^[a-z][a-z0-9_.]*$`); other JSON fields are stored verbatim;
the request is capped at 64 KB.

#### Cancellation rules

| Current status | Result |
| --- | --- |
| Any non-terminal status | `200`, becomes `CANCELLED`, revision + 1, one `task.cancelled` event |
| `CANCELLED` | `200`, unchanged: same revision, no new event (idempotent) |
| `SUCCEEDED`, `FAILED` | `409 task_not_cancellable`, nothing written |

An optional body `{"expected_revision": N}` makes the cancel conditional: a
mismatch returns `409 stale_revision` with `current_revision`.

### Actions

| Method | Path | Result |
| --- | --- | --- |
| `POST` | `/tasks/{id}/actions` | `201` new action in `PROPOSED`; `200` on an idempotent replay |
| `GET` | `/tasks/{id}/actions?limit=100` | `200` the task's actions, oldest first |
| `GET` | `/actions/{id}` | `200` action with its live approval and attempts |
| `POST` | `/actions/{id}/approval-request` | `200`, action `WAITING_APPROVAL`, a `PENDING` approval with `expires_at` |
| `POST` | `/actions/{id}/approve` | `200`, action `APPROVED`, approval granted and re-bound to the new revision |
| `POST` | `/actions/{id}/reject` | `200`, action `REJECTED`; it can never execute |
| `POST` | `/actions/{id}/attempts` | `201`, claims the approval and persists the intent to execute |
| `POST` | `/actions/{id}/attempts/finish` | `200`, records `SUCCEEDED`, `FAILED` or `OUTCOME_UNKNOWN` |
| `POST` | `/actions/{id}/reconciliation` | `200`, action `RECONCILING`; only valid from `OUTCOME_UNKNOWN` |
| `POST` | `/actions/{id}/reconciliation/finish` | `200`, records the authoritative result |

> The attempt and reconciliation routes are **internal scaffolding**. There is no
> external executor in this milestone: starting an attempt persists intent and
> stops, and results are supplied by the caller. They exist so the durable
> lifecycle can be proven now, and so the trusted Electron broker and the future
> isolated browser worker have a stable surface to call. They are loopback-only
> and not a public API. Approve/reject likewise stands in for the trusted UI
> until Electron main becomes the security broker.

Every mutating route accepts an optional `{"expected_revision": N}`; a mismatch
returns `409 stale_action_revision` with `current_revision`.

`POST /tasks/{id}/actions` body:

```json
{
  "idempotency_key": "booking-001",
  "tool_name": "commit_booking",
  "risk_tier": "R2",
  "proposal": {
    "appointment_id": "slot-123",
    "doctor": "Dr Example",
    "time": "2026-09-19T18:30:00+05:30",
    "price": 800
  }
}
```

There is no `proposal_digest` field: the server computes it, and supplying one
is a `422`. `risk_tier` is `R0`–`R3`; every tier requires an explicit approval in
this milestone.

#### Idempotency

| Request | Result |
| --- | --- |
| New key | `201`, action created, one `action.proposed` event |
| Same key, same proposal (any key order) | `200`, the stored action, nothing written |
| Same key, different proposal or tool | `409 action_proposal_conflict`, nothing changed |

#### Crash recovery

An execution attempt that a previous runtime process started and never finished
is recovered at startup as `OUTCOME_UNKNOWN` with `error_code = runtime_restart`.
The action and its task become `OUTCOME_UNKNOWN`, the original attempt is kept,
and **no second attempt is created**. The action cannot be re-approved or
re-executed; the only way forward is reconciliation.

#### Error codes

| Code | Status | Meaning |
| --- | --- | --- |
| `task_not_found`, `action_not_found` | `404` | No such record |
| `task_not_cancellable` | `409` | The task is terminal |
| `task_not_accepting_actions` | `409` | The task is terminal, so it takes no action work |
| `action_proposal_conflict` | `409` | The idempotency key holds a different proposal |
| `invalid_action_transition` | `409` | Not a legal move in the action state machine |
| `stale_revision`, `stale_action_revision` | `409` | `expected_revision` did not match |
| `approval_not_usable` | `409` | Missing, ungranted, expired, consumed or unbound approval (see `reason`) |
| `no_unfinished_attempt` | `409` | Nothing in flight to finish |
| `concurrent_modification` | `409` | Retry the request |
