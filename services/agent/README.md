# Lumi agent runtime

A Python sidecar that stores Lumi agent tasks, their event history, and a
durable ledger of proposed, approved and attempted actions in PostgreSQL.

Milestone 1 made a task durable. Milestone 2 made acting durable: actions,
approvals and execution attempts survive a hard kill, and an execution that was
interrupted mid-flight becomes `OUTCOME_UNKNOWN` rather than being guessed at or
retried. Milestone 3 makes the action real: an isolated browser worker drives
Chromium against a deterministic appointment site, and a booking that succeeded
while the response was lost is reconciled rather than repeated. It still does no
planning and runs no LLM. See
[`docs/AGENT-RUNTIME.md`](../../docs/AGENT-RUNTIME.md) and
[Browser execution](#browser-execution-milestone-3) below.

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
| `BROWSER_WORKER_URL` | — | Loopback URL of the browser worker. Unset disables browser execution. |
| `BROWSER_WORKER_TOKEN` | — | Shared credential for that worker. Required together with the URL. |
| `BROWSER_WORKER_TIMEOUT_SECONDS` | `120` | How long the runtime waits for one dispatch. |

## Checks

```powershell
uv run pytest        # needs TEST_DATABASE_URL (a database ending in _test)
uv run mypy          # strict
uv run playwright install chromium   # once, for the browser tests
```

The suite is in four layers, and they can be run separately:

| Layer | Command | What it needs |
| --- | --- | --- |
| Unit | `uv run pytest tests/test_browser_registry.py tests/test_browser_protocol.py tests/test_booking_proposal.py tests/test_appointment_fixture_site.py` | Nothing |
| PostgreSQL integration | `uv run pytest -m "not browser"` | `TEST_DATABASE_URL` |
| Browser fixture integration | `uv run pytest -m "browser and not hardkill"` | Chromium, and the above |
| Hard-kill / restart | `uv run pytest -m hardkill` | All of the above |

The hard-kill layer is the acceptance test. A real Chromium books a real
appointment on the fixture site; the site creates the booking and then never
answers; the test waits until the *site* confirms the booking exists and only
then hard-kills the runtime and the worker; fresh processes start; startup
recovery marks the action `OUTCOME_UNKNOWN`; and a read-only `lookup_booking`
establishes the truth. It asserts exactly one execution attempt, exactly one
browser submission, and exactly one booking.

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
  authoritative state; it never acts again. `lookup_booking` is the first real
  implementation: a GET with no click and no form submission.
- **Browser dispatch** — one row per piece of browser work. `attempt_id` is
  unique, so one execution attempt drives a browser exactly once.

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

> The attempt and reconciliation routes are **internal**. They let a caller
> persist an attempt and report a result directly, which is how the ledger's
> lifecycle is tested without a browser. Real browser execution goes through
> [the browser routes](#browser-routes-internal) instead, which take an action id
> and derive everything else from the persisted proposal. All of them are
> loopback-only and not a public API. Approve/reject likewise stands in for the
> trusted UI until Electron main becomes the security broker.

Every mutating route accepts an optional `{"expected_revision": N}`; a mismatch
returns `409 stale_action_revision` with `current_revision`.

`POST /tasks/{id}/actions` body:

```json
{
  "idempotency_key": "booking-001",
  "tool_name": "commit_booking",
  "risk_tier": "R2",
  "proposal": {
    "site": "appointment_fixture",
    "slot_id": "slot-a-1830",
    "doctor": "Dr A",
    "time": "2026-09-19T18:30:00+05:30",
    "price": 800,
    "currency": "INR"
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
| `browser_execution_not_supported` | `409` | The action's tool has no browser implementation |
| `invalid_booking_proposal` | `409` | The stored proposal is not a valid booking; it is never executed on a partial reading |
| `browser_worker_not_configured` | `503` | No browser worker is configured, so the capability is absent |

## Browser execution (Milestone 3)

Milestone 3 adds the first real side effect outside PostgreSQL. An isolated
browser worker drives Chromium through Playwright against a deterministic local
appointment site, and a booking that succeeded while the response was lost is
reconciled rather than repeated.

> This does **not** establish reliability on arbitrary public websites. It
> establishes execution and reconciliation semantics against a site Lumi
> controls.

### Three processes

```text
fixture site            browser worker              Lumi runtime
evals/sites/...   <--   app.browser.main    <--     app.main
(Playwright)            (Chromium, token)           (PostgreSQL, ledger)
```

The worker is separate on purpose: it is the process that reads untrusted web
pages, so it is the process that must not hold `DATABASE_URL`, provider keys,
the approval API, a shell or a filesystem. It gets a credential, an origin
allowlist, and one typed operation at a time.

### Running it

```powershell
# 0. Once: the browser binary
uv run playwright install chromium

# 1. The deterministic fixture site
uv run python -m evals.sites.appointments.server --port 8801

# 2. Mint a worker credential with trusted code, never by hand
$token = uv run python -c "from app.browser.session import generate_worker_token as g; print(g().get_secret_value())"

# 3. The browser worker (its own process, its own tiny environment)
$env:LUMI_BROWSER_TOKEN = $token
$env:LUMI_BROWSER_ALLOWED_ORIGINS = "appointment_fixture=http://127.0.0.1:8801"
$env:LUMI_BROWSER_HEADLESS = "false"    # headed, to watch it work
uv run python -m app.browser.main --port 8802

# 4. The runtime, told where the worker is
$env:BROWSER_WORKER_URL = "http://127.0.0.1:8802"
$env:BROWSER_WORKER_TOKEN = $token
uv run uvicorn --factory app.main:create_app --host 127.0.0.1 --port 8765
```

With `BROWSER_WORKER_URL` or `BROWSER_WORKER_TOKEN` unset the runtime has no
browser capability and the browser routes answer `503`. A URL without a token is
refused rather than treated as an unauthenticated channel to something that
performs real, irreversible side effects.

| Worker variable | Default | Meaning |
| --- | --- | --- |
| `LUMI_BROWSER_TOKEN` | — | Required. At least 16 characters; mint 32 bytes. |
| `LUMI_BROWSER_ALLOWED_ORIGINS` | `""` | `name=http://host:port`, comma-separated. |
| `LUMI_BROWSER_HEADLESS` | `true` | `false` for a headed local demonstration. |
| `LUMI_BROWSER_OPERATION_TIMEOUT_SECONDS` | `0` | `0` uses each operation's own timeout. |

### The reviewed operations

| Operation | Effect | Retry | Reconciliation |
| --- | --- | --- | --- |
| `search_appointments` | `READ_ONLY` | safe | not required |
| `read_available_slots` | `READ_ONLY` | safe | not required |
| `prepare_booking` | `PREPARE` | safe | not required |
| `commit_booking` | `CONSEQUENTIAL` | reconcile before retry | `lookup_booking` |
| `lookup_booking` | `READ_ONLY` | safe | not required |

The registry is a fixed Python tuple. There is no endpoint that takes
JavaScript, a selector, a URL or an operation the registry does not list, and no
`POST /browser/evaluate`. The worker serves exactly `GET /health` and
`POST /v1/dispatch`.

### The booking proposal

`commit_booking` is the first typed consequential tool:

```json
{
  "idempotency_key": "booking-001",
  "tool_name": "commit_booking",
  "risk_tier": "R2",
  "proposal": {
    "site": "appointment_fixture",
    "slot_id": "slot-a-1830",
    "doctor": "Dr A",
    "time": "2026-09-19T18:30:00+05:30",
    "price": 800,
    "currency": "INR"
  }
}
```

`site` is a reviewed **name**, not a URL: the worker resolves it against its own
allowlist, so a proposal cannot choose where the browser goes. Immediately
before the irreversible click the worker re-reads doctor, time, price and
currency from the page and compares them with this stored proposal. Any
difference means no booking, a `FAILED` action, and structured
`changed_facts` — a new proposal and a new approval are needed to proceed.

### Browser routes (internal)

| Method | Path | Result |
| --- | --- | --- |
| `POST` | `/actions/{id}/browser-execution` | Claims the approval, drives the browser, records the outcome |
| `POST` | `/actions/{id}/browser-reconciliation` | Read-only `lookup_booking`; records the authoritative result |
| `GET` | `/actions/{id}/browser-dispatches` | The browser work recorded for this action |

Both execution routes take an action id and **nothing else**: what happens in the
browser comes from the persisted proposal and the reviewed registry, never from
the caller. Like the attempt routes they are loopback-only internal scaffolding.

### The fixture site

`evals/sites/appointments/` is evaluation infrastructure, never imported by
`app/`. Fixed catalogue: Dr A (Dermatology, ₹800, Saturday 18:30), Dr B
(Dermatology, ₹950, 19:15), Dr C (Dentistry, ₹600, 10:00).

It counts **every** submission, including rejected ones and ones whose response
was dropped, and it deliberately does **not** deduplicate: booking the same
reference twice creates two bookings. A site that absorbed duplicates would hide
the exact bug this milestone exists to catch, so "exactly one booking exists" is
evidence about Lumi rather than a courtesy from the site.

`/__eval__/state`, `/__eval__/reset` and `/__eval__/faults` are the test control
plane. The worker never uses them; it drives the site through Playwright like
any other website.

### Tests

```powershell
uv run pytest -m "not browser"              # unit + PostgreSQL integration
uv run pytest -m "browser and not hardkill" # real Chromium against the fixture
uv run pytest -m hardkill                   # hard-kill and restart
```

The acceptance test in `tests/test_browser_lost_response.py` is the one that
matters. A real Chromium books a real appointment; the site creates the booking
and then never answers; the test waits until the *site* confirms the booking
exists and only then hard-kills both the runtime and the worker; fresh processes
start; startup recovery marks the action `OUTCOME_UNKNOWN`; and `lookup_booking`
establishes the truth. It asserts exactly one execution attempt, exactly one
browser submission, and exactly one booking.
