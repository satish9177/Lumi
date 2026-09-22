# Agent task runtime

Evolving Lumi into a voice, vision, browser and memory agent. Milestone 1 made a
task durable. Milestone 2 made *acting* durable: a ledger of proposed actions,
durable approvals, execution attempts, and an honest answer when the runtime
dies mid-action. Milestone 3 makes the action **real**: an isolated browser
worker drives Chromium against a deterministic appointment site, and a booking
that succeeded while the response was lost is reconciled rather than repeated.
Setup and API details are in
[`services/agent/README.md`](../services/agent/README.md).

> **Milestone 3 does not establish reliability on arbitrary public websites.**
> It establishes reliable execution and reconciliation *semantics* against
> deterministic browser fixtures. Nothing here has been tried against a real
> clinic, a real booking engine, or any site Lumi does not control, and the
> `lookup_booking` trust assumption that lets an absent booking be reported as a
> known failure is explicitly a property of the fixture, not of the web.

## Shape

```text
Electron app (unchanged)      services/agent (Python sidecar)        separate processes
  renderer ─ preload ─ main     FastAPI ─ services ─ repositories       browser worker
                                            │           │                  │ Chromium
                                            │        PostgreSQL            ↓
                                            └── typed dispatch ────→  reviewed adapter
                                                (loopback + token)         │
                                                                           ↓
                                                                 evals/sites/appointments
                                                                  (deterministic fixture)
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

### Browser dispatches (migration `0003`)

`browser_worker_generations`: `id`, `runtime_generation` (FK),
`worker_started_at`, `registered_at`. One row per browser worker process.

`browser_dispatches`: `id`, `action_id` (FK), `attempt_id` (FK, nullable,
**unique**), `worker_generation` (FK), `operation`, `site`, `effect`, `status`,
`submitted`, `observation_id`, `error_code`, `duration_ms`, `result` (JSONB),
`started_at`, `finished_at`. A CHECK requires an `attempt_id` for any
`CONSEQUENTIAL` dispatch. See
[Browser execution](#browser-execution-milestone-3).

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
Milestone 3 supplies the first real implementation: `lookup_booking` navigates
the site read-only and reports `FOUND`, `NOT_FOUND` or `UNKNOWN`. See
[Browser execution](#browser-execution-milestone-3), and in particular
[when absence means absence](#when-absence-means-absence) -- `NOT_FOUND` becomes
`FAILED` only where a site guarantees it can.

## Browser execution (Milestone 3)

### Why the worker is a separate process

The browser is the component that reads untrusted input. Everything a web page
says arrives through it, and a page is free to lie, to change underneath it, or
to contain text designed to be mistaken for an instruction. Running it inside
the runtime would mean the process holding `DATABASE_URL`, the action ledger and
the approval API is also the process parsing whatever a website sent.

So the worker is its own process, and least privilege is a fact about what it
was given rather than a promise about how it behaves:

| The worker has | The worker does not have |
| --- | --- |
| A credential and a list of allowed origins | Any database connection or URL |
| One typed operation and its typed input | The task, its history, or any other task |
| A fresh browser context per dispatch | Any approval surface, read or write |
| A place to record "I submitted" | Any way to alter a proposal |
| | Provider keys, the Electron `.env`, a shell, or the filesystem |

It cannot approve anything because there is no code path to an approval, not
because it is trusted not to try.

### The trust boundary

Loopback stopped being sufficient the moment real side effects became possible:
any process on the machine can reach loopback, and a consequential booking is
not something to hand to whoever connects first. Every request to the worker,
including health, carries a credential in the `x-lumi-worker-token` header,
compared with `hmac.compare_digest`.

The credential is minted by trusted bootstrap code (`generate_worker_token`, 32
bytes), passed to the worker through its environment, and held as a `SecretStr`.
It is never in a URL (URLs reach access logs, history and referrers), never
logged, never stored in the database, and never reaches the Electron renderer. A
URL without a token produces no browser capability at all, rather than an
unauthenticated one.

The worker allowlists **origins by name**. A proposal names a site
(`appointment_fixture`); the worker resolves that against its own configuration.
A proposal carrying a URL would let whoever wrote the proposal choose where the
browser goes.

### The closed tool registry

`app/browser/registry.py` holds a fixed tuple of reviewed operations. A dispatch
names one; the name resolves there or the dispatch is refused. There is no
selector parameter, no URL parameter, no script parameter, and no
`POST /browser/evaluate`. The worker serves exactly two routes, `GET /health`
and `POST /v1/dispatch`, and a test asserts that nothing else exists.

| Operation | Effect | Retry | Reconciliation |
| --- | --- | --- | --- |
| `search_appointments` | `READ_ONLY` | safe | not required |
| `read_available_slots` | `READ_ONLY` | safe | not required |
| `prepare_booking` | `PREPARE` | safe | not required |
| `commit_booking` | `CONSEQUENTIAL` | **reconcile before retry** | `lookup_booking` |
| `lookup_booking` | `READ_ONLY` | safe | not required |

Each operation declares a typed input and output model, an effect class,
preconditions, postconditions, a timeout, **and what that timeout means**. The
registry refuses to construct itself if a consequential operation is marked
retryable or declares no reconciliation path: such an operation would have no way
out of `OUTCOME_UNKNOWN` except a guess.

### `commit_booking` versus `lookup_booking`

These are the two halves of the design, and they are deliberately asymmetric.

`commit_booking` is the only thing in Lumi that can change the outside world. It
requires a claimed approval and a persisted attempt; the worker refuses it
outright without an `attempt_id`. Its order is not negotiable:

1. Navigate fresh. Never book from a page left over from preparation.
2. Refuse if the slot is gone.
3. Read doctor, time, price and currency; compare against the **persisted**
   proposal; refuse on any difference.
4. Fill the reference Lumi derived from the action.
5. Re-read from newly resolved locators and compare **again**, because the page
   may have changed since step 3.
6. Only now set `submitted`, then click.
7. Prove a booking exists by reading a receipt id off a confirmation page.

`lookup_booking` is a GET. It navigates, reads, and returns `FOUND`, `NOT_FOUND`
or `UNKNOWN`. It has no click, no form submission and no access to the
`submitted` flag; a test asserts its source contains neither. That is what makes
it safe to run against an action whose outcome is unknown: **running it cannot
create the thing it is looking for.** Reconciliation that could act would be a
retry wearing a different name.

### When absence means absence

`NOT_FOUND` is recorded as `FAILED` only where the site's own trust declaration
in `app/domain/sites.py` says absence is authoritative, alongside the reason:

> The fixture's booking store is the single writer, in one process, with no
> queue, no settlement step and no expiry. A booking is visible to the very next
> lookup by construction, and a booking that was never created can never appear
> later.

For every other site the default is `lookup_absence_is_authoritative = False`,
and `NOT_FOUND` leaves the action `OUTCOME_UNKNOWN`. That is the correct answer
for essentially any real booking site: a booking can be pending, queued, held
behind an unsettled payment, visible only to a logged-in session, or merely
eventually consistent. On such a site an empty lookup is evidence about the
lookup, not about the world.

A lookup that *failed* (unreachable worker, timeout, unreadable page) resolves
nothing either. Failing to look is not evidence of absence.

### Changed values invalidate an approval

An approval authorises specific values, and a website may change them at any
time. `changed_facts` compares the page against the persisted proposal field by
field. Times are compared as instants, so the same moment written in another
offset is not a change; everything else is compared exactly.

On any difference the worker returns `CHANGED_RESOURCE`, **nothing is
submitted**, and the action becomes `FAILED` with the differences recorded
structurally (`{"field": "price", "approved": "800", "observed": "950"}`). The
approval was already consumed by the attempt, so the new values have no
authorisation and cannot acquire any: proceeding needs a new proposal and a new
approval. The changed value is never silently accepted, and never renegotiated
by the worker, which has no way to do either.

### Classification: what each failure may claim

| What happened | Outcome | Why |
| --- | --- | --- |
| Postcondition verified | `SUCCEEDED` | A receipt id on a confirmation page carrying our reference |
| Approved values changed | `FAILED` | Never submitted |
| Slot withdrawn | `FAILED` | Never submitted |
| Site returned its own refusal page | `FAILED` | A definitive negative acknowledgement after the click |
| Error before the click | `FAILED` | The worker can show no submission went out |
| Connection to the worker refused | `FAILED` | The dispatch was never delivered |
| Worker refused (auth, unknown op, stale generation) | `FAILED` | Refusal happens before any browser work |
| **Timeout after the click** | `OUTCOME_UNKNOWN` | A statement about how long we waited |
| Dropped connection mid-request | `OUTCOME_UNKNOWN` | Delivered; the answer was lost |
| Confirmation page unrecognised | `OUTCOME_UNKNOWN` | Submitted, and nothing proved |
| Runtime killed mid-flight | `OUTCOME_UNKNOWN` | Milestone 2 startup recovery |
| Duplicate dispatch already in flight | `OUTCOME_UNKNOWN` | It may well be succeeding right now |

`submitted` is set immediately **before** the click, never after. It is the bit
that separates the top half of that table from the bottom.

### Generations and stale results

`runtime_generations` (Milestone 2) is joined by `browser_worker_generations`:
one row per worker process, owned by the runtime generation that registered it.

The runtime handshakes before claiming an approval, learns the worker's
generation, and addresses every dispatch to it. A worker that has restarted has a
new generation and refuses with `stale_worker_generation`; the runtime discards
any reply naming a different runtime generation, worker generation, dispatch or
operation. Doing the handshake *before* the approval is claimed means a missing
or replaced worker is discovered while nothing is at stake: no attempt started,
no approval spent, nothing to reconcile.

This is deliberately **not** a lease. There is no heartbeat, no expiry and no
renewal, none of which would change a decision while one worker runs at a time.
It becomes a lease by adding `expires_at` and `heartbeat_at` to
`browser_worker_generations` and making "is this generation current" a query
instead of an equality check. Every caller already asks that question in one
place, so no call site moves.

### `browser_dispatches`

One row per piece of browser work: `action_id`, a nullable `attempt_id`,
`worker_generation`, `operation`, `site`, `effect`, `status`, `submitted`,
`observation_id`, `error_code`, `duration_ms`, timestamps, and a bounded result.

Two constraints carry the weight:

- **`attempt_id` is UNIQUE.** One execution attempt dispatches browser work
  exactly once. With Milestone 2's "one unfinished attempt per action" and "one
  approval funds one attempt", a second real submission for one approved action
  cannot be written down: the insert fails in PostgreSQL before any request
  leaves the process.
- **`effect <> 'CONSEQUENTIAL' OR attempt_id IS NOT NULL`.** Consequential work
  is only ever done on behalf of a persisted attempt. A reconciliation lookup
  carries no `attempt_id`, so it can never be counted as an execution.

Startup recovery closes dispatches a dead runtime left open as
`OUTCOME_UNKNOWN` / `runtime_restart`. `submitted` stays false there, not as a
claim that nothing was submitted, but because nothing observed one. The
uncertainty lives on the action.

### Ordering: commit, then browse

```text
persist proposal -> request approval -> approve exact digest
        |
handshake with the worker            (nothing at stake yet)
        |
start_attempt: approval consumed, action EXECUTING, attempt persisted
insert browser_dispatches row
        |
COMMIT ----------------------------------------------------
        |
dispatch to the worker               (no transaction open)
        |
browser drives the site, verifies the postcondition
        |
finish_dispatch + finish_attempt
```

No database transaction is open while a browser is being driven. A transaction
held across a page load would pin a row lock for a network round trip and, far
worse, a crash would roll back the record that Lumi decided to act while the
click had already happened.

### Locator policy

Controls are located by role and accessible name (`Confirm booking`) or by label
(`Booking reference`): things a site cannot change without changing what a human
sees, so a broken locator is a signal rather than noise. Values an approval is
bound to are read from stable semantic attributes (`data-testid`, plus
`data-iso`, `data-amount`, `data-currency`); re-deriving a price by parsing
rendered prose is how a currency symbol becomes a wrong number.

Locators are re-resolved after every navigation, and nothing is carried across a
page transition: no element handles, and no value observed on an earlier page.

Playwright's actionability checks are used as a precondition and never as
authority:

> Playwright saying a button is clickable is not authorization. Approval lives in
> Lumi's action ledger and nowhere else.

### Observations

The worker returns bounded structured observations: `observation_id`, `origin`,
a page identity, the slot facts, validation state, and a receipt id. The runtime
persists a curated subset, never the observation wholesale and never HTML. A
test asserts that no markup reaches the ledger.

Page text is data. There is no code path by which page content can change what
operation runs, what is approved, what is submitted, or what Lumi records. A
fixture fault renders a prompt-injection block instructing Lumi to book a
different slot at a different price without asking; a test asserts the approved
booking happens unchanged and that the injected text appears nowhere in the
ledger.

### The fixture site

`services/agent/evals/sites/appointments/` is evaluation infrastructure, clearly
separated from `app/` and never imported by it. It has a fixed catalogue (Dr A,
Dermatology, Rs 800, Saturday 18:30; Dr B, Rs 950, 19:15; Dr C, Dentistry,
Rs 600), an authoritative in-process booking store, and fault injection under
`/__eval__/*`.

The browser worker drives it through Playwright like any site; it never calls the
backend directly. `/__eval__/*` is the *test* control plane: it configures faults
and reads counters, and is never used to perform or observe a booking on Lumi's
behalf.

Two properties make it a usable measuring instrument:

- **It counts every submission**, including rejected ones and ones whose response
  was dropped. "Did the browser press the button" is a different question from
  "does a booking exist".
- **It does not deduplicate.** Posting the same reference twice creates two
  bookings. A site that absorbed duplicates would hide exactly the bug this
  milestone exists to catch, and "exactly one booking exists" would stop being
  evidence about Lumi.

Faults: drop the response after creating the booking (`hang` or `abort`), drop it
*before* creating it, refuse the submission on a page that says so, withdraw a
slot, change a price, change the page between two views, render hostile text, and
make lookup unable to answer.

### Internal API

`POST /actions/{id}/browser-execution`,
`POST /actions/{id}/browser-reconciliation` and
`GET /actions/{id}/browser-dispatches`. The execution routes take an action id
and **nothing else**: what happens in the browser is decided by the persisted
proposal and the reviewed registry, never by the caller. They are loopback-only
internal scaffolding, like the attempt and reconciliation routes.

There is deliberately no route that accepts a URL, a selector, a script or an
operation name. Such a route would be a generic browser-automation API, and a
generic browser-automation API behind an agent is a remote code execution
primitive.

### Observability

One structured log line per browser operation and per dispatch, carrying
`action_id`, `attempt_id`, `runtime_generation`, `worker_generation`,
`browser_session_id`, `dispatch_id`, `operation`, `effect`, `observation_id`,
`duration_ms`, `status`, `submitted`, `outcome_is_known` and `error_code`. No
secrets, no proposal bodies, no page content. There is no OpenTelemetry pipeline
and this milestone does not need one.

### Limitations

- Nothing here says anything about arbitrary public websites. The locator policy,
  the postcondition and the reconciliation semantics are the reusable parts; the
  fixture is not a stand-in for the web.
- `NOT_FOUND` as a known failure is a property of the fixture alone.
- There are no sessions, logins, credentials, OTP or CAPTCHA, so nothing is known
  about how any of those interact with this model.
- One worker, one browser, no leases, no concurrency across workers.
- A crash between the click and the `submitted` flag reaching anyone is
  indistinguishable from a crash before the click. Both are `OUTCOME_UNKNOWN`,
  which is correct but means the fast, known-failure path is not always available.
- The fixture holds its state in memory; it is a test instrument, not a database.

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
- The runtime has no shell, desktop or generic network-automation capability.
  Its only reach outside PostgreSQL is a typed dispatch to the browser worker,
  which can perform exactly the operations in the reviewed registry.
- The browser worker is a separate, least-privilege process with no database,
  no approval surface, no secrets and no generic scripting. Web page content is
  untrusted data with no authority over policy, approval or execution. See
  [Browser execution](#browser-execution-milestone-3).
- PostgreSQL binds to `127.0.0.1` only, and so do the runtime, the browser worker
  and the fixture site. Credentials live in ignored `.env` files and in the
  worker process's own environment; the worker token is never written down.

## Not in this milestone

The Electron `PendingActionStore` is unchanged and still in memory, deliberately:
this Python ledger establishes the durable execution model first, and a second
competing implementation inside Electron would be worse than none. There is no
Electron-to-runtime bridge or process management yet, and `LocalStore` is
unchanged.

Milestone 3 removed the largest piece of what used to be synthetic here:
`commit_booking` really runs, in Chromium, against a real site, and both its
result and its reconciliation come from what a browser observed rather than from
a test fixture handing an answer in. What is still deliberately absent:

- Any public website. The only reviewed site is the local deterministic fixture,
  and no claim is made about anything else.
- Google, Practo, hospital portals, payments, OTP, CAPTCHA and stored user
  credentials.
- Sessions and logins. The worker uses a fresh browser context per dispatch and
  persists no storage state.
- LLM planning: proposals are still written by callers and tests, not generated.
- Long-term memory, embeddings, pgvector, Redis, Celery, Kafka, LangChain,
  LangGraph.
- Voice changes, the Electron-to-FastAPI bridge, and native desktop control.
- Worker leases and multi-worker concurrency. One worker at a time, identified by
  generation.

## Milestone 4: secure desktop bridge

Milestone 4 connects the runtime to the real Lumi desktop app. The trust chain
is:

```text
React renderer -> typed preload (window.lifeLens.agent, fixed channels)
  -> Electron main: exact sender/top-frame/app-URL check, input validation,
     main-owned domain client, response projection
  -> authenticated loopback runtime (fresh bearer credential per process)
  -> PostgreSQL ledger / runtime-owned browser worker -> fixture site
```

- **Lifecycle.** Electron main launches `app.server` (fixed venv path, no
  shell, constructed environment) and sends its credential only after the child
  reports its bound port on a private pipe. The runtime launches and owns its
  browser worker with an allowlisted environment (no `DATABASE_URL`, no runtime
  credential); the worker binds port 0 and reports its port the same way. The
  runtime's Windows kill-on-close job and parent-handle watchdogs take the
  runtime, worker and Chromium down with Electron, even on a hard kill.
- **Capability surface.** Main can reach an allowlist of runtime routes only:
  task create/read, paged events, action list/read, approval request, approve,
  reject, `browser-execution`, `browser-reconciliation`, and read-only booking
  search/prepare. Preparation takes a slot id; the proposal is built from what
  the worker observes. Approve/reject/execute/reconcile take an action id and
  the revision the user reviewed, and nothing else. Execution's revision is
  enforced by the transaction that claims the approval.
- **Projection.** Responses are validated and projected into closed DTOs
  (`src/shared/agent-contracts.ts`). Stored JSON (results, event payloads,
  evidence) is copied field by field; credentials, URLs, references, raw errors
  and page prose never reach the renderer. Errors become app-authored messages.
- **Contract.** `app/api/contract.py` generates
  `src/shared/agent-runtime-contract.json`; both test suites check it.
- **Restoration.** Main stores only the active task id. The renderer replays
  events after a sequence (paged, gap-checked, de-duplicated) and rebuilds from
  sequence 0 when the runtime generation changes. Unconfirmed mutations are
  reported, never retried.
- **UX semantics.** `OUTCOME_UNKNOWN` and `RECONCILING` offer only "Check
  existing booking" (read-only). "Booking confirmed" / "No booking was created"
  appear only from a verified receipt or an authoritative reconciliation. A
  changed price or missing slot shows the facts, states that nothing was booked
  and requires a new proposal and approval. A task cannot gain a second booking
  while one is unresolved.
- **Ownership boundary.** Local tools keep using main's in-memory
  `PendingActionStore`; durable browser actions belong only to this ledger.
  Neither system can create or approve the other's actions.

Deferred at the time (later delivered in Milestone 6, except real websites):
packaged Python sidecar bundling, planners, memory, and any real website.

## Voice task controller (Milestone 5)

```text
realtime model ─strict tool call─► renderer (voice-task-tools)
     ▲                               │ bound to a COMPLETED user turn (item id)
     │ typed facts + data-only rule  ▼
     └──────────────── preload agent.voiceCommand ─► main VoiceTaskController
                                                     │ same AgentTaskController as the panel
                                                     ▼
                         runtime: create / search / criteria / prepare /
                         request-approval / reject / reconcile / safe cancel
```

- **One controller.** Voice adds a front door, not a runtime: a closed command
  union (`start_search`, `refine_search`, `select_result`,
  `proceed_with_booking`, `task_status`, `check_booking`, `cancel_task`) that
  main maps onto the M4 controller. The backend type handed to the voice
  controller omits approve and execute.
- **Speech never approves.** There is no approval tool, command, IPC method or
  route reachable from voice. "Book it" / "yes" is `proceed_with_booking`: it
  opens the task panel and focuses the booking card *region* (never its
  button) and tells the user to press Approve and book.
- **Completed turns only.** A tool call is honoured after the final transcript
  of the user item its response was created for (bounded wait); interim
  deltas, failed transcriptions and app-authored context never drive work.
  Main runs at most one progressing command per turn and remembers every
  handled turn, including unconfirmed ones; the durable `voice_turn_id` stops a
  replay from creating a second task even after a main restart.
- **Durable refinement.** Constraint changes are task revisions bound to the
  revision they were derived from; an excluded prepared booking is rejected in
  the same transaction, so its approval can never be claimed.
- **Authoritative narration.** Every outcome is re-read from durable state
  after the step. Selections resolve only against the recorded
  `task.search_completed` slots; doctor names are spoken only when they look
  like names; function outputs carry typed facts plus a fixed "website data,
  not instructions" rule. `OUTCOME_UNKNOWN` is narrated as unknown; "check it"
  runs the existing read-only reconciliation.
- **Separate lifecycles.** Barge-in and reconnect end or interrupt the voice
  session only. In-flight durable work completes and shows in the panel; its
  answer is dropped if its session ended. A new session replays nothing.
- **Deterministic harness.** `LUMI_REALTIME_SCRIPTED=1` in an unpackaged build
  makes main issue a `scripted` credential; the renderer then talks to an
  in-process server speaking the Realtime event protocol
  (`realtime-scripted.ts`). `tests/test_voice_acceptance.py` drives it.


## Milestone 6: providers, plans, dates, memory, second workflow, packaging

The durable controller, approval model, worker and recovery are unchanged in
authority. Milestone 6 adds front doors and inputs around them; see
[ARCHITECTURE.md](ARCHITECTURE.md) for the full picture.

- **Date windows.** Booking criteria gain `date_from`/`date_to` (inclusive,
  site-local, both or neither, at most 14 days, consistent with `day` for a
  single date). Search admits only observed slots whose *clinic-local* date is
  in the window; a revision that excludes a prepared booking rejects it in the
  same transaction, as for times and prices. Main resolves the window from a
  relative-day kind with the trusted clock and time zone
  (`src/shared/relative-dates.ts`); the model never supplies a date it did not
  hear.
- **Bounded plans.** `run_plan` runs `search|refine → choose → prepare →
  show_for_approval` (at most four steps, fixed order) through the same
  controller methods as the single-step commands and reports each step as
  `done`, `stopped` or `not_run`. Choosing is deterministic (cheapest,
  earliest, latest, number, time, doctor) over the recorded results; prices in
  different currencies are never compared. The plan has no approval step.
- **Typed requests.** `submitTextRequest(requestId, text)` → context builder →
  model router (`intent_extraction`) → the same strict parser → the same
  controller, once per request id. `request_id` is stored on the task like
  `voice_turn_id`, so a replay never creates a second task.
- **Main composer routing.** Lumi's main composer calls
  `routeTypedRequest(requestId, text)` before anything else. Main answers
  `{handled: true, result}` or `{handled: false}`, and only an unhandled
  request continues to the realtime conversation, so exactly one path owns a
  request. A request naming exactly one http(s) address is always handled as a
  page inspection, even when the address is refused, so it can never become a
  legacy `open_url` call. Any other command is handled only if the durable
  state it acts on exists (a new search, clinic question or "remember" needs
  none; status and check need a task; "yes", refinements, choices and cancel
  need an open task), so "Hello", "yes" or "check the weather" with no task
  stay ordinary conversation. Invalid input or an interpreter failure is
  refused as handled; the renderer sends nothing if main cannot be asked.
- **Clinic information** (`clinic_info` tasks). `POST
  /tasks/{id}/info/lookup` reads public doctor profiles through the reviewed
  `read_doctor_profiles` operation (READ_ONLY, SAFE_TO_RETRY) and appends
  `task.info_lookup_completed` with typed facts under the task lock. There is
  no action, approval, attempt or dispatch row: nothing changes anywhere. The
  request accepts only `specialty`, `doctor`, `topic` and provenance fields.
- **Memory.** Preferences and episodic summaries live in main
  (`agent-memory.json`) with provenance. Task facts stay in PostgreSQL.
- **Migrations for installed apps.** `python -m app.migrate` upgrades to the
  Alembic head and prints only a status word. The packaged app runs it before
  starting the runtime; the runtime still refuses an unmigrated database.
- **Demo clinic site.** `python -m evals.sites.appointments.server
  --demo-dates --parent-pid <pid>` moves the pinned catalogue to the coming
  Saturday and exits with its owner. Tests use the pinned catalogue.

No migration was added: new data lives in the existing `tasks.request` JSONB
and `task_events`, so the schema head is still `0003`.


## Form planning and exact disclosure approval (M8b S5, migration `0010`)

**No browser writes.** Nothing here reaches the worker: no dispatch, no operation, no page.

* **Migration `0010`** adds `protected_values` (one row per closed kind; `value`, `value_digest` with a CHECK that it equals `sha256(value)`, `preview`) and widens `task_grants` to a third kind, `form_prepare` (`profile_binding` now covers both profile-bound kinds; the one-open-grant index is per `(task_id, kind)`). Downgrade removes `form_prepare` grants and the table. It adds no drafts, freeze, `frozen_at`, written-value hash or dispatch column (S6).
* **Routes.** `GET /protected-values`, `PUT /protected-values/{kind}` (response: `data_ref`, `kind`, `preview`, `updated_at`; never the value); `GET /tasks/{id}/authenticated/form`; `POST .../form/prepare-scope | grant | revoke | planning-context | propose`; `POST /actions/{id}/field-disclosure/approve | reject` (body: `expected_revision` only). The generic action routes refuse the `prepare_form` tool.
* **Flow.** observe (S4) -> `prepare-scope` (PENDING, grants nothing) -> trusted `grant` (CAS) -> `planning-context` (structure + masked previews, one provider) -> planner `prepare_form` -> `propose` (validated; builds the manifest; `WAITING_APPROVAL`) -> trusted `approve` -> `prepared_nothing`.
* **`prepared_nothing` semantics.** No new action status. The exact approval is granted, claimed and terminalised through the existing state machine (`WAITING_APPROVAL -> APPROVED -> EXECUTING -> SUCCEEDED`) in one transaction by `ActionService.settle_exact_approval`; the finished attempt's result is `{"code": "prepared_nothing", "browser_dispatches": 0, ...}`. That attempt creates no `browser_dispatches` row and is never given to a worker; it exists so the approval is spent exactly as an executed one would be. `SUCCEEDED` here means *the approval was recorded and used*, not that anything was done.
* **A task must be open.** Planning happens while the account-reading grant is active, before an answer closes the task; a recorded answer ends the task and form planning with it. Joining the read loop and the form flow into one product path is S6's concern.
* **Events.** `task.form_prepare_scope_requested|granted|revoked` and `task.form_planning_context_built` carry ids, digests and counts only; the disclosure action's `action.*` events carry `tool_name = prepare_form` so the timeline words them truthfully.
* **Errors.** `form_prepare_refused` (422, a proposal/input problem) and `form_prepare_state_changed` (409, the facts moved: `protected_value_changed`, `account_changed`, `stale_observation`, `stale_document_epoch`, `stale_form_epoch`, `origin_changed`, `grant_not_usable`), `protected_value_refused` (422). Each carries a stable `reason` and never a value, label or preview.

## Memory and classification for authenticated tasks (M8a S3)

`authenticated_read` tasks are classified `account_private`. Episodic memory refuses them, the public research context builder never sees their evidence, and diagnostics carry stable codes and counts only. Evidence lives in `authenticated_observations` / `authenticated_answers` (no raw URLs) and is purged with the task or the profile.

**Observation schema versions (M8b S4, migration `0009`).** `authenticated_observations.schema_version = 1` is an S3 text/link observation with no element inventory; existing rows keep it (`form_epoch = 0`, `element_inventory = '{}'`, enforced by a CHECK) and are never rewritten. Every new observation is `schema_version = 2`: it adds `form_epoch` (a per-tab monotonic counter that rises when the form inventory changes, even without a document navigation) and `element_inventory` (bounded, value-free, redacted: forms `f1-f5`, frames `fr0-fr4`, elements `e1-e40`, options `op1-op25`). The inventory is local `account_private` evidence: it is **not** part of the runtime API response (whose `schema_version` stays the S3 text/link shape), not part of any provider prompt, and diagnostics carry only `form_count`, `element_count`, `option_count`, `form_epoch` and `inventory_truncated`. S4 observes form structure only; there is no operation that types, chooses, checks, clicks, uploads or submits.


## Windows desktop observation (M9 S1, migration `0012`)

Observation only; see `docs/plans/milestone-9.md` and `docs/SECURITY.md`. Opt-in with `LUMI_DESKTOP_OBSERVATION=1` (optionally `LUMI_DESKTOP_TIMEOUT_SECONDS`, the hard "hung" deadline, default 30). Off by default, the routes answer `503 desktop_refused` (`desktop_automation_disabled`); on a non-Windows platform they answer `desktop_automation_unsupported` and nothing is faked. No browser or task operation depends on the desktop worker.

* **Worker.** `app.desktop.main` (`python -m app.desktop.main`), supervised by `app.desktop.managed.ManagedDesktopWorker` like the browser worker: fresh generation UUID and credential per start, ready line over a private pipe, parent watchdog, runtime kill-on-close job. Reads run under a soft time budget (min(10 s, 40% of the hard deadline)) and return marked `time` instead of failing; only a provider that does not answer at all costs the worker its life. The worker starts lazily on the first desktop request and is created only when the capability is enabled. `fence()` kills the current generation immediately and the next call starts a new one.
* **Worker routes.** `POST /v1/desktop/surfaces`, `POST /v1/desktop/observe` (and `GET /health`), all authenticated; nothing else exists. Every request names the expected worker generation.
* **Runtime routes.** `GET /desktop/surfaces` (returns the `worker_generation` it was listed under) and `POST /desktop/observations` with body `{worker_generation, surface_ref, surface_epoch}` and nothing else (a pair is only unique within one worker generation; another generation is `stale_worker_generation`, and does not fence the healthy worker) (no handle, pid, selector, coordinates, script, action or property). Both require the runtime bearer credential; Electron has no function that calls them.
* **Errors.** One code, `desktop_refused`, with a closed `reason`: `desktop_automation_unsupported`, `desktop_automation_disabled`, `desktop_worker_unavailable`, `desktop_observation_timeout` (504), `stale_surface`, `stale_worker_generation`, `stale_control`, `surface_unavailable`, `surface_changed`, `elevated_window_refused`, `integrity_unverifiable`, `credential_surface`, `element_missing`, `element_ambiguous`, `element_changed`, `desktop_backend_failed`. A refusal never carries a title, a name or text.
* **Persistence.** `desktop_worker_generations` (identity of one worker run, bound to the runtime generation) and `desktop_observations` (`id` = observation id, `worker_generation`, `surface_ref`, `surface_epoch`, `schema_version`, `classification = 'desktop_private'`, `snapshot` JSONB, `snapshot_digest`, `truncated`, `created_at`). Ownership is the worker generation: there is no task association because no planner consumes these observations. The newest 25 rows are retained, none older than 24 hours (swept at startup, even with the capability off). A timed-out, refused or unstable read stores nothing.
* **Observation schema (version 1).** Nodes `u1..u200` in document order with `parent_ref`, a closed `role`, bounded `name` and `text`, `enabled`, `visible`, `focused`, `focusable`, `selected`, `checked`, `expanded` and advertised `patterns` (availability only). The observation carries `surface_epoch`, `worker_generation`, `truncated` with reasons (`nodes`, `depth`, `text`, `scan`, `time`) and a value-free `fingerprint`.
* **Provider firewall.** In S1 nothing outside `app/desktop`, `app/services/desktop.py`, `app/repositories/desktop.py` and the two routes could read a desktop observation. S2 rewrote that on purpose (see the S2 section): exactly `app/services/desktop_disclosure.py` reads one back, and only after an exact approval; structural tests pin the importer allowlist and the sole `get_observation` caller.

## Desktop disclosure and read-only reasoning (M9 S2, migration `0013`)

Needs `LUMI_DESKTOP_OBSERVATION=1` (S1) and optionally `LUMI_DESKTOP_DISCLOSURE_TTL_SECONDS` (default 600). The desktop worker and its observation-only contract are unchanged. See `docs/plans/milestone-9.md`, `docs/SECURITY.md` and `docs/reviews/milestone-9-s2.md`.

* **Task type** `desktop_read` (`tasks.request = {type, objective}`, the user's typed question only; no snapshot, title or text). Statuses: `WAITING_APPROVAL` -> `EXECUTING` (claimed) -> `SUCCEEDED` | `FAILED` | `OUTCOME_UNKNOWN`; declining or cancelling before the claim is `CANCELLED`.
* **Routes** (all authenticated): `POST /desktop/read-tasks` `{objective, recipient, model, worker_generation, surface_ref, surface_epoch}` (observes locally, opens the card; nothing is sent), `GET /desktop/read-tasks/latest`, `GET /desktop/read-tasks/{id}`, `POST .../grant` and `.../revoke` `{grant_id, expected_revision}` (the trusted click), `POST .../disclosure` (claim: consume the grant, commit, release the redacted projection for ONE call), `POST .../result` `{disclosure_id, result | failure}`. Electron main may call exactly these; `POST /desktop/observations` is not reachable from main.
* **Grant** `task_grants.kind = 'desktop_disclose'`, scope `desktop-disclose-v1`: observation id, snapshot digest, observed-at, worker generation, surface ref/epoch, recipient, model, the fixed allowed-field list, 120 nodes / 8 KB, redaction policy, `max_provider_calls = 1`, `failover = none`, and the display label/title (card only). `PENDING -> ACTIVE` (revision + digest checked, snapshot no older than 10 minutes) `-> COMPLETED` (the claim). Cancelling a task revokes an unclaimed grant.
* **Disclosure** `desktop_disclosures`: `STARTED -> SUCCEEDED | FAILED | OUTCOME_UNKNOWN`; `grant_id` and `task_id` UNIQUE; ids, digests, counts, closed `error_code` (`model_unavailable`, `invalid_output`, `answer_not_grounded`, `observation_unavailable`, `runtime_restart`). `observation_id` is deliberately not a foreign key (raw observations expire under S1's retention; this audit row holds no desktop text). Startup marks any `STARTED` row `OUTCOME_UNKNOWN`; a `STARTED` row older than five minutes is marked on the next read. A result for a disclosure that is no longer `STARTED` is refused.
* **Answer** `desktop_answers`: `classification = 'desktop_private'`, `answer | cannot_answer`, redacted evidence quotes per control ref; never memory or conversation context.
* **Errors**: `desktop_disclosure_refused` (422) and `desktop_disclosure_state_changed` (409) with a closed `reason` (`grant_not_found`, `grant_not_pending`, `grant_not_active`, `grant_expired`, `grant_changed`, `observation_unavailable`, `observation_changed`, `observation_stale`, `disclosure_already_recorded`, `objective_invalid`, `result_malformed`, ...). S1's `desktop_refused` still covers a refused observation, which creates no task and no grant.
* **ModelRouter**: private class `desktop_planning` (one recipient and model, no failover, no image, 8,000 input tokens); the provider receives only the rules, the approved question, capture-time facts and the redacted controls.

## Desktop focus, semantic scroll and registered launch (M9 S3, migration `0014`)

Needs `LUMI_DESKTOP_OBSERVATION=1`; the registered applications come from `LUMI_DESKTOP_REGISTERED_APPS` (a JSON list `[{"appId", "label", "executable", "args"?}]`, set by Electron main from its own environment, validated whole at startup; empty means none; there are **no built-in applications**). See `docs/plans/milestone-9.md`, `docs/SECURITY.md` and `docs/reviews/milestone-9-s3.md`.

* **Task type** `desktop_action` (reserved: the generic `POST /tasks` refuses it). Tools `DESKTOP_FOCUS` (R1), `DESKTOP_SCROLL` (R1), `DESKTOP_LAUNCH` (R2), always approval-gated.
* **Routes** (authenticated; Electron main may call exactly these): `GET /desktop/actions/apps`; `POST /desktop/actions/scroll-targets` `{worker_generation, surface_ref, surface_epoch}` (observes locally, lists only scrollable controls); `POST /desktop/actions/focus|scroll|launch` (open a card, nothing happens); `GET /desktop/actions/latest`, `GET /desktop/actions/{id}`; `POST /desktop/actions/{id}/approve|decline` `{expected_revision}`. Errors: `desktop_action_refused` (409) with a closed `reason` (`desktop_action_open`, `desktop_action_stale`, `desktop_action_observation_stale`, `desktop_action_not_approvable`, `use_desktop_route`, ...); S1's `desktop_refused` still carries worker refusals.
* **Worker** (`app.desktop.worker`): `POST /v1/desktop/input-baseline|focus|scroll|launch`, all behind the token and generation fence; a `dispatch_id` is performed at most once per generation and a finished one replays its stored answer.
* **Ordering** and **classification** as in `docs/reviews/milestone-9-s3.md` (sections 3, 9-11). `desktop_dispatches`: `DISPATCHED -> OK | FAILED_BEFORE_EFFECT | OUTCOME_UNKNOWN`, `attempt_id` UNIQUE, DB-enforced identity per operation, no title/path/handle/coordinate/value columns. Startup recovery closes an orphaned dispatch as `OUTCOME_UNKNOWN` (`runtime_restart`).
* **Freshness**: a scroll proposal must come from the newest observation of that surface, at most 60 seconds old, re-checked in the claim transaction.

## Desktop bounded semantic actions (M9 S4, migration `0015`)

Needs `LUMI_DESKTOP_OBSERVATION=1`, same as S1-S3. See `docs/plans/milestone-9.md`, `docs/SECURITY.md` and
`docs/reviews/milestone-9-s4.md`.

* **Two separate authorities.** A model may *propose* (never authorize) exactly one of `invoke(controlRef)`
  / `set_value(controlRef, valueRef)` / `select(containerRef, optionRef)` from ONE exact, redacted
  disclosure of ONE observation, through a private task class (`desktop_action_planning`, single
  recipient, zero failover, no image) -- disclosure grant `kind = desktop_action_plan`, a new
  `desktop_action_plans` table (`STARTED -> SUCCEEDED | FAILED | OUTCOME_UNKNOWN`, `grant_id`/`task_id`
  UNIQUE, `proposed_action` JSONB opaque-refs-only). `DesktopActionService.propose_from_plan` then
  independently re-verifies the whole proposal against a freshly rebuilt projection and opens an ordinary
  desktop action (tools `DESKTOP_SET_VALUE`/`DESKTOP_SELECT`/`DESKTOP_INVOKE`, all R2) on the SAME S3
  ledger, behind its own SECOND, separate exact approval. The model never sees a raw value, a coordinate,
  a native id, a provider, a risk tier or an approval.
* **Routes** (authenticated; Electron main only): `POST /desktop/action-plans` `{objective, recipient,
  model, worker_generation, surface_ref, surface_epoch, values?}`, `POST /desktop/action-plans/{taskId}/grant|decline`,
  `POST /desktop/action-plans/{taskId}/run` (claims + calls the one provider + records the result),
  `GET /desktop/action-plans/latest|{taskId}`; `POST /desktop/actions/from-plan` `{planId}` (the ONLY way
  an S4 mutation reaches the action ledger -- there is no direct `set-value`/`select`/`invoke` proposal
  route); `POST /desktop/actions/{id}/reconcile` `{expected_revision, outcome}`. Errors:
  `desktop_plan_refused` (422) / `desktop_plan_state_changed` (409) mirroring S2's disclosure errors;
  `desktop_action_unresolved` (blocks a new plan, confirm, claim or execution proposal while a mutation
  is `OUTCOME_UNKNOWN`/`RECONCILING`, in any task); `desktop_action_not_reconcilable`.
* **Worker** (`app.desktop.worker`): `POST /v1/desktop/set-value|select|invoke`, same token/generation
  fence and dispatch-replay-at-most-once-per-generation as S3. `set_control_value`/`select_control`/
  `invoke_control` widen `desktop_dispatches`' operation set and identity columns (`value_ref`,
  `option_container_ref`, `invoke_effect`) -- still no raw value, title, path, handle or coordinate column.
* **Getting unstuck.** `OUTCOME_UNKNOWN`/`RECONCILING` on an S4 mutation blocks every other desktop action
  until a person reports `succeeded`/`failed`/`still_unknown` through `reconcile`; nothing is retried or
  re-derived automatically. `RecoveryService.recover_interrupted_reconciliations` (startup, unscoped by
  `runtime_generation`) closes the case of a process dying between the two committed reconciliation
  transactions, moving an action stuck at `RECONCILING` back to `OUTCOME_UNKNOWN` so it can be reconciled
  again.
* **Freshness**: `PLANNING_OBSERVATION_MAX_AGE_SECONDS = 20` for confirming a plan; S3's existing
  `ACTION_OBSERVATION_MAX_AGE_SECONDS = 60` for opening and claiming the execution proposal. Live target
  re-resolution immediately before the effect is mandatory regardless, by exact match only.
