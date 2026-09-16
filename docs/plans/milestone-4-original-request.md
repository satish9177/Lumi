You are the **technical lead, manager, architect, and final reviewer** for an existing project called Lumi.

You are NOT the primary implementation agent.

Your primary implementation agent is:

**GPT-5.6 Sol**

You may also use smaller/cheaper models as bounded sub-agents for research, codebase inspection, test analysis, documentation, and mechanical verification.

Your job is to:

1. Understand the existing system completely.
2. Preserve all security and reliability invariants from Milestones 1–3.
3. Break Milestone 4 into small implementation slices.
4. Assign implementation work primarily to GPT-5.6 Sol.
5. Review Sol's work after important slices.
6. Use smaller agents only for narrowly scoped supporting tasks.
7. Prevent architecture drift and unnecessary refactoring.
8. Run an independent architectural/security review after implementation.
9. Send concrete defects back to Sol.
10. Perform a final verification after fixes.

Do NOT optimize for speed at the expense of correctness.

---

# Existing Project

Repository:

`Lumi`

Lumi is an Electron + React + TypeScript Windows desktop assistant being evolved into a:

**Voice + Vision + Browser + Long-Term Memory Agent**

Do not rebuild the application.

Milestones 1–3 are already complete.

---

# Existing Milestone 1 foundation

The Python sidecar currently has:

* FastAPI
* PostgreSQL
* async SQLAlchemy Core
* Alembic
* Pydantic
* durable tasks
* durable ordered task events
* revision-based optimistic concurrency
* runtime persistence across hard restarts
* schema-head validation
* typed APIs

Tasks survive runtime termination and restart.

---

# Existing Milestone 2 foundation

The runtime also has:

* durable immutable action proposals
* canonical proposal digests
* per-task idempotency keys
* durable approval records
* expiring approvals
* approvals bound to exact proposal + revision
* single-use approvals
* durable action attempts
* runtime generations
* `OUTCOME_UNKNOWN`
* restart recovery
* reconciliation
* hard-kill tests

Critical invariant:

> Once a consequential action might have reached the outside world, Lumi never blindly retries merely because a response was lost or a process crashed.

`FAILED` and `OUTCOME_UNKNOWN` are deliberately different.

---

# Existing Milestone 3 foundation

Lumi now has its first real browser side effects.

Current architecture includes:

* isolated browser worker process
* Playwright Python
* Chromium
* authenticated runtime ↔ worker communication
* closed typed browser tool registry
* deterministic appointment fixture website
* reviewed appointment-site adapter
* `search_appointments`
* `read_available_slots`
* `prepare_booking`
* `commit_booking`
* `lookup_booking`
* browser dispatch ledger
* worker generations
* proposal revalidation immediately before consequential actions
* hostile webpage text treated only as untrusted data
* changed-price protection
* missing-slot protection
* authoritative reconciliation
* lost-response fault injection
* hard-kill tests

The critical Milestone 3 test proves:

```text
1 execution attempt
1 browser submission
1 booking
0 duplicate bookings
```

even when:

```text
website accepts booking
→ response is lost
→ runtime/browser worker die
→ Lumi restarts
→ action becomes OUTCOME_UNKNOWN
→ read-only lookup_booking() reconciles it
→ existing booking is found
```

Do not weaken this.

---

# Milestone 4 objective

Implement:

# Secure Electron ↔ Agent Runtime Bridge + Trusted Task/Approval UI

At the end, the REAL Lumi desktop app should be able to:

1. Start/supervise the Python runtime.
2. Create a durable task.
3. Display the task timeline.
4. Display a consequential action awaiting approval.
5. Show the exact trusted approval preview.
6. Allow the user to approve or reject.
7. Execute the already-approved action through the existing ledger.
8. Display `SUCCEEDED`, `FAILED`, or `OUTCOME_UNKNOWN`.
9. Display reconciliation state.
10. Recover the same durable task after restart.

---

# Required trust architecture

Preserve this boundary:

```text
React Renderer
      ↓
Typed Preload Bridge
      ↓
Electron Main
TRUSTED SECURITY BROKER
      ↓
Authenticated Loopback Channel
      ↓
FastAPI Agent Runtime
      ↓
PostgreSQL / Browser Worker
```

The renderer must NEVER communicate directly with FastAPI.

The renderer must NEVER receive:

* runtime bootstrap credential
* browser worker token
* `DATABASE_URL`
* PostgreSQL credentials
* permanent provider credentials
* internal browser-worker URLs

Electron main is the sole trusted runtime client.

Preserve Electron's existing:

* context isolation
* sandbox
* disabled Node integration
* sender validation
* restrictive IPC surface
* CSP/security assumptions

---

# Critical design rule

The LLM/model is never an authority.

Use the principle:

> Models propose. Trusted application code validates scope, obtains authorization, executes, and verifies outcomes.

Do not permit any implementation shortcut that violates this.

---

# Your role as manager

Do NOT begin by making large code edits yourself.

First inspect:

* repository tree
* current git status
* recent Milestone commits
* `src/main`
* `src/preload`
* `src/renderer`
* `src/shared`
* existing typed `window.lifeLens` bridge
* sender validation
* existing `PendingActionStore`
* existing approval UI patterns
* `services/agent`
* runtime APIs
* action ledger
* browser worker
* current tests
* architectural docs

Then create a concise implementation plan.

---

# Primary implementation model

Use:

**GPT-5.6 Sol**

as the primary implementation owner.

Keep one primary Sol implementation thread/context if possible so architectural assumptions remain consistent.

Do not spawn many coding agents modifying overlapping files.

---

# Recommended task decomposition

Manage Milestone 4 approximately as these six slices.

You may adjust boundaries after inspecting the codebase, but preserve the intent.

---

## Sol Task 1 — Runtime authentication + Electron sidecar manager

Implement trusted sidecar lifecycle.

Electron main should:

* generate a high-entropy per-runtime bootstrap credential
* launch the Python runtime
* use a fixed executable/path strategy
* use process spawning with `shell: false`
* pass controlled arguments/environment
* wait for authenticated health handshake
* monitor process exit
* restart using bounded retry/backoff
* gracefully terminate on Lumi shutdown
* avoid orphan processes

Runtime should:

* bind only to `127.0.0.1`
* authenticate every endpoint
* authenticate streaming endpoints
* validate Host
* reject unexpected Origins
* have no permissive CORS
* never log the bootstrap credential

For development, using the local uv/Python environment is acceptable.

Do not overbuild packaged-sidecar bundling yet.

### Astra review after Task 1

Review specifically for:

* shell injection
* user-controlled executable paths
* credential leakage
* direct renderer access
* unauthenticated endpoints
* hot restart loops
* orphan processes
* insecure environment propagation

Do not proceed until serious findings are fixed.

---

## Sol Task 2 — Narrow typed Electron runtime client

Build a runtime client owned exclusively by Electron main.

It should expose only domain operations required by Lumi.

Examples:

```text
createTask
getTask
getTaskEvents
getTaskActions
getAction

requestApproval
approveAction
rejectAction

executeApprovedAction

beginReconciliation
reconcileAction
```

Do NOT expose:

```text
fetchAnyPath
rawRuntimeRequest
rawHttp
browserDispatch
sql
playwright
evaluate
```

The renderer must not choose arbitrary runtime URLs.

### Astra review after Task 2

Check:

* capability surface
* payload validation
* stale revisions
* error handling
* secrets
* route smuggling
* generic escape hatches

---

## Sol Task 3 — Shared contracts and drift detection

Create a robust but simple contract strategy between:

* Python
* Electron main
* preload
* renderer

Cover:

* task
* task event
* action
* approval preview
* action status
* task status
* revisions
* runtime errors

Options include:

* JSON Schema
* generated TypeScript from schemas
* OpenAPI-derived types
* shared fixtures verified on both sides

Do not build a giant schema framework.

Required property:

> Python and TypeScript cannot silently disagree.

Add automated drift tests.

### Small-model tasks allowed here

A smaller model may:

* inventory Python Pydantic schemas
* inventory TypeScript types
* compare enum values
* identify missing contract tests

It must NOT independently redesign the API.

---

## Sol Task 4 — Typed preload / IPC bridge

Extend existing `window.lifeLens`.

Use narrow agent methods.

Conceptually:

```text
lifeLens.agent.createTask(...)
lifeLens.agent.getTask(...)
lifeLens.agent.getTaskEvents(...)
lifeLens.agent.getActions(...)

lifeLens.agent.approveAction(...)
lifeLens.agent.rejectAction(...)
lifeLens.agent.executeAction(...)
lifeLens.agent.reconcile(...)
```

Preserve existing IPC security.

Required:

* sender validation
* request validation
* response validation
* no generic IPC channel
* no arbitrary method names
* no runtime credential crossing preload
* no worker credential crossing preload
* no database secret crossing preload

### Astra review after Task 4

Treat this as a security review.

Attempt to find ways that compromised renderer content could:

* invoke arbitrary IPC
* select arbitrary runtime routes
* mutate an approved proposal
* retrieve secrets
* communicate directly with browser worker
* execute arbitrary system commands

Any such path is a blocker.

---

## Sol Task 5 — Task timeline + trusted approval UI

Add a focused Lumi task UI without redesigning the entire product.

Display understandable task states.

Example:

```text
Task created
↓
Action prepared
↓
Waiting for approval
↓
Approved
↓
Executing
↓
Completed
```

Also support:

```text
Executing
↓
Outcome uncertain
↓
Checking existing result
↓
Confirmed
```

Allow expandable technical details for portfolio/debugging:

* task id
* action id
* revision
* event sequence
* attempt id
* timestamps
* proposal digest

---

# Trusted approval card

For booking, render a trusted preview from persisted runtime state.

Example:

```text
Book appointment

Doctor: Dr A
Specialty: Dermatology
Time: Saturday 6:30 PM
Price: ₹800

[Reject] [Approve]
```

Renderer approval input must be only something equivalent to:

```text
action_id
expected_revision
```

NOT:

```text
doctor
slot
time
price
proposal
proposal_digest selected by renderer
```

The renderer must not redefine the consequential action.

If revision changes, stale preview must be invalidated.

---

# Changed-resource UX

Preserve M3 semantics.

If:

```text
approved price = ₹800
current website price = ₹950
```

show clearly:

```text
The appointment changed after approval.

Approved price: ₹800
Current price: ₹950

Nothing was booked.
Review the updated details before trying again.
```

No automatic approval.

No silent update.

No booking.

---

# OUTCOME_UNKNOWN UX

Never display `OUTCOME_UNKNOWN` as generic failure.

Display meaning clearly, for example:

```text
Booking status uncertain

The website may have accepted the booking, but Lumi did not receive reliable confirmation.

Lumi is checking the existing booking before attempting anything else.
```

When state becomes `RECONCILING`, explain that Lumi is checking, NOT retrying.

Only display:

```text
Booking confirmed
```

or:

```text
No booking was created
```

when authoritative reconciliation supports it.

### Astra review after Task 5

Check whether the UI accidentally:

* treats unknown as failed
* offers a retry button while outcome is unknown
* lets renderer alter consequential values
* obscures approval consequences
* creates duplicate execution pathways

---

## Sol Task 6 — Restart/reconnect + full integration

Implement robust runtime reconnect.

If Python runtime dies:

```text
Electron notices
↓
UI marks runtime unavailable
↓
controlled restart
↓
new authentication handshake
↓
reload persisted task state
↓
continue from durable truth
```

Electron must never invent recovery state.

Python/PostgreSQL remain authoritative.

Implement durable timeline replay.

Preferred model:

```text
GET persisted events after sequence N
+
live updates / polling
```

Use streaming only if it adds reliability.

Polling is acceptable.

Important requirements:

* renderer reload does not lose timeline
* reconnect does not duplicate logical events
* sequence ordering preserved
* same task reused after restart

---

# Required end-to-end scenarios

Sol must prove:

## Normal booking

```text
Electron Lumi
↓
create task
↓
durable action
↓
trusted approval card
↓
approve
↓
execute
↓
Playwright
↓
fixture booking
↓
verified receipt
↓
UI = succeeded
```

Verify:

```text
1 task
1 action
1 attempt
1 browser submission
1 booking
0 duplicates
```

---

## Lost-response restart scenario

```text
booking submitted
↓
fixture creates booking
↓
response lost
↓
runtime/Lumi dies
↓
restart
↓
same task reloaded
↓
OUTCOME_UNKNOWN
↓
reconciliation
↓
existing booking found
↓
SUCCEEDED
```

Verify again:

```text
1 task
1 action
1 attempt
1 browser submission
1 booking
0 duplicates
```

No retry booking.

---

# Existing PendingActionStore

Do NOT rewrite it yet.

Current boundary:

```text
Existing Lumi local tools
→ existing PendingActionStore

New durable agent/browser actions
→ Python action ledger
```

Never have both systems own the same action.

Document this temporary boundary.

Migration can happen later.

---

# Small-agent usage

You may spawn smaller agents for bounded tasks only.

Good examples:

### Repository inspector

Ask it to map:

* IPC handlers
* preload methods
* renderer call sites
* sender validation

### Security checker

Ask it to identify:

* generic IPC
* token leakage
* shell invocation
* renderer-visible secrets

### Contract checker

Compare:

* Python enums/schemas
* TypeScript definitions

### Test analyst

Analyze failures and determine whether they are:

* new
* baseline
* flaky

### Documentation agent

Update documentation after architecture is already decided.

Do NOT let these agents independently edit shared critical architecture unless Sol integrates/reviews the change.

---

# Parallelism rule

Do NOT spawn many implementation agents modifying overlapping modules.

For example, do NOT have:

```text
agent A edit preload
agent B edit preload
agent C edit runtime contracts
agent D simultaneously change same IPC types
```

Prefer:

```text
Astra plans
↓
Sol implements coherent slice
↓
small agents inspect/test
↓
Astra reviews
↓
Sol fixes
```

---

# Testing expectations

After every major slice, Sol should run relevant local tests.

At final integration run:

```text
npm run typecheck
npm test
npm run build
```

and:

```text
cd services/agent
uv run pytest -v
uv run mypy
uv run alembic upgrade head
```

plus new Electron/runtime integration tests.

Do not claim success without executing tests.

Keep previously documented unrelated failures separate.

---

# Architecture invariants that must survive M4

Continuously verify:

1. Proposal remains immutable.
2. Renderer cannot approve a modified proposal.
3. Approval remains single-use.
4. One approval cannot create two execution attempts.
5. Consequential browser operations require a persisted attempt.
6. `OUTCOME_UNKNOWN` never blindly retries.
7. Reconciliation is read-only.
8. Page text has no authority.
9. Browser worker cannot approve.
10. Renderer cannot contact browser worker.
11. Renderer cannot contact FastAPI directly.
12. Browser worker does not receive database credentials.
13. Renderer does not receive runtime credentials.
14. No generic browser/IPC/runtime execution surface is introduced.

If an implementation violates one of these, reject it even if the demo works.

---

# Final Sol integration review

Once all six slices are complete, have Sol:

1. Inspect all changed files.
2. Remove dead code.
3. Run full test suites.
4. Run normal booking E2E.
5. Run lost-response/hard-restart E2E.
6. Check git diff for unrelated changes.
7. Produce a technical implementation report.

Do NOT consider Sol's report proof by itself.

Inspect the diff independently.

---

# Astra independent final review

After Sol declares implementation complete, perform a NEW review from first principles.

Do not merely confirm Sol's explanation.

Review actual source/diff.

Search specifically for:

### Trust-boundary regressions

* renderer → FastAPI
* renderer → browser worker
* preload secrets
* database secrets
* bootstrap token leaks

### Generic escape hatches

* arbitrary IPC channel
* raw fetch
* arbitrary URL
* arbitrary process spawn
* `shell: true`
* generic browser action
* arbitrary JavaScript
* unrestricted file/system access

### Approval vulnerabilities

* renderer-generated proposal
* stale approval accepted
* digest mismatch ignored
* approval reused
* approve + execute race
* action mutated after approval

### Reliability problems

* duplicate attempts
* duplicate event rendering
* automatic retry of unknown outcome
* restart race
* stale child process result
* stale runtime generation
* stale browser generation
* event sequence gaps

### UX semantic bugs

* `OUTCOME_UNKNOWN` shown as failure
* retry available before reconciliation
* changed price silently accepted
* user not shown consequential values

### Process lifecycle

* orphan Python processes
* endless restart loop
* shutdown races
* token reused across new runtime process unexpectedly

### Logging/security

* bootstrap credential logged
* worker token logged
* DB URL logged
* sensitive proposal contents dumped unnecessarily

Classify findings:

```text
BLOCKER
HIGH
MEDIUM
LOW
NIT
```

Do not invent findings.

Provide exact files/locations and why they matter.

---

# Fix pass

Send only concrete verified findings to GPT-5.6 Sol.

Sol should:

* fix BLOCKER/HIGH findings
* fix justified MEDIUM findings
* avoid unrelated refactors
* rerun affected tests
* rerun full suites if critical code changed
* provide diff summary

---

# Astra final verification

After fixes:

1. Re-inspect relevant source.
2. Verify findings are actually fixed.
3. Ensure fixes did not introduce regressions.
4. Recheck the 14 architecture invariants.
5. Review final test results.
6. Review final E2E counts.

Only then declare Milestone 4 complete.

---

# Do NOT implement in Milestone 4

Do not add:

* LLM planner
* automatic agent reasoning
* voice task creation
* Telugu voice work
* long-term memory
* pgvector
* arbitrary public websites
* real hospital websites
* credentials/login
* OTP
* CAPTCHA
* payments
* native desktop automation
* generic computer control
* Redis
* Celery
* Kafka
* LangChain
* LangGraph

Avoid scope creep.

---

# Final Milestone 4 acceptance criteria

The actual Lumi desktop application must demonstrate:

```text
User
↓
Electron UI
↓
durable task
↓
trusted action preview
↓
human approval
↓
existing action ledger
↓
isolated Playwright worker
↓
real fixture side effect
↓
verified result
↓
durable timeline
```

And the failure path:

```text
side effect happens
↓
response lost
↓
processes die
↓
restart
↓
same task restored
↓
OUTCOME_UNKNOWN
↓
reconciliation
↓
known result
```

with exactly:

```text
1 task
1 action
1 execution attempt
1 browser submission
1 booking
0 duplicate bookings
```

---

# Final deliverable

When everything is complete, give me:

## Manager summary

## Work delegated to GPT-5.6 Sol

## Work delegated to smaller agents

## Files changed

## Architecture changes

## Electron sidecar lifecycle

## Authentication model

## Runtime client API

## IPC/preload design

## Shared contract design

## Timeline UI

## Approval UX

## OUTCOME_UNKNOWN UX

## Restart/recovery behaviour

## Normal booking E2E proof

## Lost-response/restart E2E proof

Include exact:

* task count
* action count
* attempt count
* browser submission count
* booking count

## Full test results

## Baseline failures still present

## Astra independent review findings

## Findings fixed by Sol

## Final invariant verification

## Remaining limitations

## Deferred work

## Recommended Milestone 5

Milestone 5 should connect Lumi's existing realtime/WebRTC voice system to the task controller while preserving this rule:

> Voice may create, modify, pause and discuss tasks, but speech alone must not authorize sensitive consequential actions.

Do NOT begin Milestone 5.
