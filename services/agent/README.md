# Lumi agent runtime

A Python sidecar that stores Lumi agent tasks, their event history, and a
durable ledger of proposed, approved and attempted actions in PostgreSQL.

Milestone 1 made a task durable. Milestone 2 made acting durable: actions,
approvals and execution attempts survive a hard kill, and an execution that was
interrupted mid-flight becomes `OUTCOME_UNKNOWN` rather than being guessed at or
retried. Milestone 3 makes the action real: an isolated browser worker drives
Chromium against a deterministic appointment site, and a booking that succeeded
while the response was lost is reconciled rather than repeated. It still does no
planning and runs no LLM. Milestone 4 connects it to the real desktop app:
Electron main supervises the runtime (which owns its browser worker), and the
renderer drives a trusted task/approval UI through a narrow typed bridge. See
[`docs/AGENT-RUNTIME.md`](../../docs/AGENT-RUNTIME.md),
[Browser execution](#browser-execution-milestone-3) and
[Desktop bridge](#desktop-bridge-milestone-4) below.

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

# 4. Run manually (Electron normally mints this per process generation)
$env:LUMI_RUNTIME_TOKEN = uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
uv run python -m app.server --port 8765
```

The runtime refuses to start if `DATABASE_URL` is missing or invalid, the
database is unreachable, or the schema is not at the Alembic head. On startup it
also recovers unfinished execution attempts left by a previous process (see
[Crash recovery](#crash-recovery)) before it accepts any request.

Every endpoint, including health, docs and unknown paths, requires `Authorization:
Bearer <LUMI_RUNTIME_TOKEN>`. The fixed `app.server` entry binds only to
`127.0.0.1`, rejects non-loopback Host values and every supplied Origin, and
does not write access logs. Electron main passes a fresh token through the child
environment; it never crosses preload or reaches the renderer.

`infra/postgres/init` creates `lumi_agent_test` only when the data volume is
first initialized. For an existing volume, create it manually.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | — | Required. Must use `postgresql+asyncpg://`. |
| `LUMI_RUNTIME_TOKEN` | — | Required. Per-process credential minted by Electron main; at least 32 characters. |
| `LUMI_RUNTIME_PARENT_PID` | — | Electron pid. When present, the runtime exits if that exact process handle closes. |
| `DATABASE_POOL_SIZE` | `5` | Connection pool size. |
| `DATABASE_CONNECT_TIMEOUT_SECONDS` | `5.0` | Connection timeout. |
| `APPROVAL_TTL_SECONDS` | `300` | How long a granted approval may be claimed. |
| `TEST_DATABASE_URL` | — | Test suite only; must name a database ending in `_test`. |
| `BROWSER_WORKER_URL` | — | Loopback URL of the browser worker. Unset disables browser execution. |
| `BROWSER_WORKER_TOKEN` | — | Shared credential for that worker. Required together with the URL. |
| `BROWSER_WORKER_TIMEOUT_SECONDS` | `120` | How long the runtime waits for one dispatch. |
| `LUMI_BROWSER_SITE_ORIGIN` | — | Managed mode: with no external worker, the runtime launches and owns a worker allowed to visit only this `http://127.0.0.1:<port>` origin. |
| `LUMI_BROWSER_HEADLESS` | `true` | Headless flag for the managed worker. |

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
| `GET` | `/health` | Authenticated `200` with database status and runtime generation, or `503` |
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
| `browser_worker_unavailable` | `503` | The worker could not be reached before anything was dispatched |
| `browser_observation_failed` | `503` | A read-only search or slot observation produced no usable facts |
| `booking_slot_unavailable` | `409` | The site no longer offers the slot; nothing was proposed |
| `action_already_open` | `409` | The task already has a booking that is unresolved or succeeded |
| `task_kind_mismatch` | `409` | Booking preparation on a task that is not `appointment_booking` |

The complete list is generated into `src/shared/agent-runtime-contract.json`
(see [Contract](#contract-python--typescript)).

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
$env:LUMI_RUNTIME_TOKEN = uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
uv run python -m app.server --port 8765
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

## Desktop bridge (Milestone 4)

```text
React renderer -> typed preload (window.lifeLens.agent)
  -> Electron main (sender/frame check, validation, domain client)
  -> authenticated loopback runtime (app.server) -> PostgreSQL ledger
                                                  -> owned browser worker -> fixture site
```

### Process ownership

- Electron main (`src/main/services/agent-runtime-supervisor.ts`) launches
  `.venv\Scripts\python.exe -m app.server --port N` with `shell: false`, a
  constructed environment and a fresh 32-byte credential per process; it sends
  the credential only after the child reports its bound port on a private pipe.
  Restarts are bounded; a user can restart explicitly afterwards.
- The runtime launches its **own** browser worker (`app/browser/managed.py`)
  when `LUMI_BROWSER_SITE_ORIGIN` is set and no external worker is configured.
  The worker's environment is built from an allowlist: a fresh worker token,
  the single site origin, headless flag, the runtime pid and OS basics. It
  never receives `DATABASE_URL`, the runtime token or provider keys. The
  worker binds port 0 and reports the port it owns on its stdout pipe, so the
  worker token is never sent to a guessed port. It is a child inside the
  runtime's kill-on-close Windows job and also watches the runtime's process
  handle; a dead worker is replaced (bounded) with a new credential.
- The fixture site is **not** owned by Lumi. It is an evaluation site that must
  survive runtime crashes; start it yourself (see below).

### Desktop routes

The only runtime routes Electron main can call are allowlisted in the
supervisor: create/get task, paged events, list actions, get action,
approval-request, approve, reject, `browser-execution`,
`browser-reconciliation`, and the two booking preparation routes:

| Method | Path | Result |
| --- | --- | --- |
| `POST` | `/tasks/{id}/booking/search` | Read-only `search_appointments` using the criteria stored in the task request |
| `POST` | `/tasks/{id}/booking/prepare` | Body `{"slot_id": "..."}` only. Read-only `read_available_slots`; the proposal is built from what the worker observed, then an approval is requested |
| `POST` | `/tasks/{id}/booking/criteria` | Body `{"expected_revision": n, "criteria": {...}}`. Replaces the booking constraints under the task lock, records `task.criteria_updated`, and rejects (`reason=criteria_changed`) any not-yet-executed booking the new constraints exclude. `409 task_has_unresolved_action` / `task_already_booked` while a booking may exist or is confirmed |
| `POST` | `/tasks/{id}/booking/cancel` | Optional `{"expected_revision": n}`. Refused while a booking is unresolved or confirmed; otherwise rejects open bookings and cancels the task in one transaction |

`browser-execution` and `browser-reconciliation` accept an optional
`{"expected_revision": N}`. For execution it is enforced by the transaction
that claims the approval; reconciliation is refused before any lookup unless
the action is `OUTCOME_UNKNOWN` or `RECONCILING`, and its verdict is written
only against the revision the check started from. A task may hold at most one
booking that is not `FAILED` or `REJECTED`, checked under the task lock, so an
unknown outcome cannot be side-stepped by preparing a new booking.

`GET /tasks/{id}` now includes `last_event_sequence`, so a replaying client
knows when it has caught up.

### Contract (Python / TypeScript)

`app/api/contract.py` generates `src/shared/agent-runtime-contract.json` from
the real enums and Pydantic models, including model-validated example payloads.
`tests/test_desktop_contract.py` fails when the committed file is stale, and
`src/main/services/agent-wire.test.ts` checks the TypeScript enums and wire
parsers against the same file.

```powershell
uv run python -m app.api.contract --write
```

### Running the desktop flow in development

```powershell
# 1. The fixture site (keep it running; it is the authoritative booking store)
cd services\agent
uv run python -m evals.sites.appointments.server --port 8801

# 2. Lumi (repository root). Main starts the runtime, which starts the worker.
npm run dev
```

Development-only main-process settings (never sent to the renderer):

| Variable | Default | Meaning |
| --- | --- | --- |
| `LUMI_APPOINTMENT_FIXTURE_ORIGIN` | `http://127.0.0.1:8801` | The one site origin the worker may visit |
| `LUMI_BROWSER_HEADED` | unset | `1` shows the Chromium window |
| `LUMI_AGENT_DATABASE_URL` | unset | Overrides the runtime's `.env` database (tests use the `_test` database) |
| `LUMI_USER_DATA_DIR` | unset | Absolute path of an isolated Electron profile |

Packaged builds do not bundle the Python runtime yet; the task panel then
reports that the agent runtime is not available.

Only one `app.server` may run per Windows session (named mutex) and per
database (advisory lock), so stop `npm run dev` before running the hard-kill
or Electron acceptance tests.

### Real Electron acceptance

```powershell
npm run build                              # repository root
cd services\agent
$env:LUMI_ELECTRON_E2E = "1"
uv run pytest tests/test_electron_acceptance.py -s
```

The tests launch the built app with an isolated profile and the `_test`
database, drive the rendered controls over the DevTools protocol (renderer ->
preload -> main -> runtime; they never call the runtime themselves), and count
tasks, actions, attempts and dispatches in PostgreSQL and submissions and
bookings on the fixture. The lost-response test hard-kills Electron after the
fixture has created the booking, checks that the runtime and worker died with
it, restarts the same profile, and reconciles read-only.

### Booking constraints (Milestone 5)

A booking task request may carry `specialty`, `day`, `earliest_time` /
`latest_time` (clinic-local `HH:MM`, inclusive) and `max_price` with
`max_price_currency`. Search sends only specialty/day to the site, then keeps
the observed slots inside the time window and price ceiling and appends them to
the timeline as `task.search_completed`. Prepare refuses (`409
booking_criteria_mismatch`) a slot whose *current* observation falls outside
the constraints. Unreadable bounds make the task unsearchable
(`invalid_booking_criteria`); `POST /tasks` rejects them up front.
Voice-created tasks also carry `source: "voice"`, `voice_turn_id` and the
utterance in `text`, for traceability and duplicate-turn detection.

## Milestone 6 additions

| Method | Path | Result |
| --- | --- | --- |
| `POST` | `/tasks/{id}/info/lookup` | `200` `{task, profiles}` for a `clinic_info` task; read-only, safe to repeat; `409 task_kind_mismatch` for other tasks; `503` without a worker |

- `POST /tasks` accepts `{"type": "clinic_info", "doctor" | "specialty", "topic"}`
  (topics: `overview`, `hours`, `fee`, `languages`, `address`, `walk_ins`);
  unknown fields are refused with `422`.
- Booking criteria accept `date_from` and `date_to` (`YYYY-MM-DD`, both or
  neither, ≤ 14 days) on task creation and on `POST /tasks/{id}/booking/criteria`.
- New event type: `task.info_lookup_completed`.
- `uv run python -m app.migrate` upgrades the schema non-interactively (used by
  the packaged app).
- The fixture site serves `/doctors` and accepts `fee_overrides` in
  `/__eval__/faults`; `--demo-dates` and `--parent-pid` are for the packaged
  demo only.
- `greenlet` is constrained below 3.5 (Windows Application Control, see
  [docs/PACKAGING.md](../../docs/PACKAGING.md)).
