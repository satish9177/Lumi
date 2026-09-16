# Claude Code handoff — Lumi Milestone 4

Date: 2026-09-16. Implementation stopped at the user's request so Claude Code can continue. Do not interpret this handoff as milestone completion.

## Start here

Read AGENTS.md, docs/plans/milestone-4-original-request.md (the full authoritative user request), this file, docs/plans/milestone-4.md, and docs/reviews/milestone-4.md. The review document is chronological: its final committed-evidence section supersedes earlier pending findings.

**Continue implementation in the existing isolated worktree:**

- Primary checkout: C:\Users\SATISH\projects\Lumi
- Primary branch: lumi-agent-v2, HEAD de1cdab (manager plan commit).
- Implementation checkout: C:\Users\SATISH\projects\Lumi\.worktrees\m4-sol
- Implementation branch: codex/m4-secure-runtime-bridge
- Latest implementation commit: 72d4799 — feat(agent): secure runtime supervision
- Shared product baseline: d4edfd7 — feat(agent): add isolated Playwright browser execution
- Earlier foundations: 473d7ca durable action ledger/recovery; 1b2fcdb PostgreSQL task runtime.

The implementation worktree was clean when stopped. Product changes have NOT been merged or cherry-picked into the primary branch. Primary has uncommitted manager documents, including this handoff and the copied original request. Preserve them during integration. The implementation worktree branched before the primary manager-plan commit, so read the documents from the primary checkout.

The implementation agent was interrupted. Only slice 1 is implemented; there are no saved slice 2–4 implementation drafts. Do not restart the old agents. Use explicit working directories and absolute edit targets to avoid editing the wrong checkout.

## Goal and architecture

Milestone 4 is Secure Electron ↔ Agent Runtime Bridge + Trusted Task/Approval UI in the EXISTING Electron/React/TypeScript app:

Renderer → typed preload → Electron main trusted broker → authenticated loopback FastAPI → PostgreSQL ledger / isolated browser worker.

The rendered app must create a durable task, display its timeline and a trusted persisted consequential-action preview, let the user approve/reject, execute through the ledger, show honest outcomes, and restore the same task after restart. OUTCOME_UNKNOWN requires read-only reconciliation, never automatic retry.

Keep local tools in the existing in-memory PendingActionStore. Durable browser actions belong exclusively to the Python ledger. No planner, autonomous reasoning, voice task creation, Telugu, long-term memory, arbitrary hospital sites, login/OTP/CAPTCHA/payments, generic computer use, or new infrastructure frameworks. Packaged Python bundling may remain deferred; dev setup is allowed. Do not begin Milestone 5.

## What was completed

Astra managed architecture, baseline verification and source/security review. Sol implemented slice 1 in the isolated worktree. A read-only supporting review inventoried contracts. The user is now switching implementation to Claude Code.

Commit 72d4799 changes 22 files:

- services/agent/.env.example and README.md
- services/agent/app/api/{errors,routes,schemas,security}.py
- services/agent/app/{config,main,server}.py
- services/agent/app/services/{parent_watchdog,runtime,windows_job}.py
- services/agent/tests/{conftest,test_config,test_persistence,test_runtime_lifecycle,test_runtime_security,test_schema,test_tasks_api}.py
- src/main/index.ts
- src/main/services/agent-runtime-supervisor.ts and its test

Implemented behavior:

1. Mandatory runtime bearer authentication using a SecretStr token of at least 32 characters. ASGI middleware covers HTTP/WebSocket paths before routing/body parsing, including health, docs, OpenAPI and unknown paths. Numeric 127.0.0.1 Host restriction, supplied Origins rejected, no permissive CORS. Constant-time byte comparison safely rejects non-ASCII presented tokens. Validation errors do not echo rejected inputs.
2. Authenticated health exposes the runtime generation UUID; private authenticated lifecycle shutdown endpoint.
3. Fixed python -m app.server --port N entrypoint binds only 127.0.0.1 and disables access logs.
4. Main-owned AgentRuntimeSupervisor launches the fixed dev Python path with shell:false/windowsHide:true, minimal allowlisted environment, a fresh 32-byte token, parent PID and private readiness FD. Bounded restart/backoff, startup/shutdown timeouts, graceful shutdown followed by owned-process termination, credential rotation, no runtime credentials/addresses exposed to renderer.
5. Token is sent only AFTER an owned-child fd3 JSON readiness record emitted after successful server startup/bind. This closes the free-port listener race. Main rejects HTTP redirects. Readiness is invalidated by early child exit; failed lifespan startup cannot claim readiness.
6. Windows retained parent process handle and WaitForSingleObject monitoring with explicit 64-bit ctypes signatures; kill-on-close Job Object for runtime descendants; named mutex prevents overlapping app.server instances.
7. PostgreSQL advisory lock before generation registration/recovery; monitored lock connection fails closed on loss.
8. Electron single-instance lock and dev runtime startup/shutdown wiring.

This is a reviewed foundation, not the full Electron runtime bridge. Managed browser-worker startup/ownership, domain client, desktop contracts, agent IPC/preload, task UI, restoration and real Electron acceptance tests remain unfinished.

## Review fixes already made

Do not reopen these solely because an earlier chronological review section says pending; inspect final source and regressions:

- Replaced Windows os.kill(pid,0), which could terminate the parent, with a retained process HANDLE.
- Fixed non-ASCII bearer comparison and ctypes HANDLE truncation risk.
- Made already-dead-parent detection synchronous before serving.
- Fixed stop during asynchronous port allocation spawning a process after cancellation.
- Retained child ownership until cleanup/exit is confirmed; report failed cleanup honestly.
- Accounted for signalCode/exit events when exitCode remains null.
- Removed bare taskkill PATH resolution; use owned-child termination and Windows job.
- Refused credential-bearing HTTP redirects.
- Added private post-bind fd3 readiness so a competing loopback listener cannot receive the runtime token.

A final first-principles review of the complete milestone is still required.

## Verification: baseline versus changed code

Independent clean-baseline checks at d4edfd7:

- npm.cmd run typecheck: PASS.
- npm.cmd run build: PASS.
- uv run mypy: PASS, 72 files.
- uv run pytest -v: 319 passed in 301.90 seconds, including real Chromium and lost-response/hard-kill tests.
- npm.cmd test: 75 files passed, 3 skipped, 3 failed; 1511 tests passed, 17 skipped, 1 assertion failure plus 2 collection errors.

Existing baseline failures:

- src/main/vision/real-inference.test.ts:89: missing local vocab.json under %APPDATA%\lifelens\vision-models\clip-vit-base-patch32-q8.
- src/main/vision/tokenizer-pack.test.ts: local tokenizer assets fail with model_load_failed.
- src/renderer/src/accessibility.test.tsx:73: exact LF CSS assertion against CRLF checkout.

Sol reported these final slice-1 checks at 72d4799:

- Focused combined Python run: 48 passed.
- Lifecycle real-process run: 4 passed.
- Authenticated persistence run: 2 passed.
- Supervisor Vitest: 8 passed.
- TypeScript typecheck: PASS.
- mypy: PASS, 67 files.

These focused Python runs overlap; do not sum them into a unique test total. Astra separately verified authentication behavior, retained Windows handles, real descendant cleanup, Node-to-Python fd3 communication and new readiness-security tests. One stale stdio test assertion was corrected by Sol before the final reported 8/8.

Full POSTCHANGE Python/JS suites, final integration build, migration verification and real Electron E2E have NOT been completed. The baseline build is not proof of the changed build. Remaining raw subprocess test clients likely need mandatory-auth adaptation.

Ignored baseline logs in primary: baseline-vitest.log, baseline-pytest.log, baseline-build.log.

## Remaining work, recommended order

### 1. Inspect and stabilize the committed foundation

Review 72d4799 in the implementation worktree. Check that a process denied the Electron single-instance lock cannot continue into whenReady/startup; an explicit guard was requested for the next slice and is not yet evidenced. Use the fixed app.server entrypoint because manual uvicorn launch may omit OS mutex/job protections.

Adapt remaining runtime subprocess tests, particularly tests/browser_harness.py, test_browser_booking.py, test_browser_lost_response.py and test_outcome_unknown_recovery.py. Persistence tests were adapted already. Attach runtime auth ONLY to the exact runtime origin; never globally monkeypatch httpx and accidentally send credentials to fixtures/workers. Do not add an authentication bypass mode.

### 2. Desktop contracts and narrow main-owned runtime client

Implement desktop-safe DTOs and a domain client for create/get task, events/actions, trusted preview, request approval, approve/reject, execute and reconcile. Preparation must use worker-observed facts. No generic request, URL, SQL, browser dispatch, evaluate, result-write or finish-attempt capability may cross the desktop boundary.

Approval input is action ID plus expected revision ONLY, not renderer-provided proposal, doctor, price, time or digest. Revision binding must reach the atomic approval/attempt transaction, not merely a preflight GET. Validate requests AND responses. Project raw stored JSON into safe closed DTOs; exclude credentials, internal URLs, arbitrary payloads and raw error strings. Add generation/correlation checks and contract drift tests. Never automatically retry mutations.

Python-exported JSON Schema plus generated TypeScript and runtime validators is one possible simple strategy, not an existing implementation. Ajv 8.20.0 is transitive in the lockfile; if used by runtime main code, declare it directly and keep validator/code generation out of renderer/preload execution.

### 3. Typed IPC/preload and renderer containment

Add narrow lifeLens.agent methods and fixed IPC channels. Validate payloads and responses in main. Validate the exact sender, top frame and allowed app URL; existing requireMainWindow is only an owning-window check and navigation has startsWith matching. Add a restrictive CSP. Renderer must not contact runtime or worker, even if it guesses a port.

Preserve the existing ephemeral realtime path: renderer currently fetches https://api.openai.com/v1/realtime/calls; Vite HMR is needed only in dev. Do not broadly allow localhost/http/ws to solve CSP. Baseline inventory found no actual iframe/webview/eval/worker requirements; forms preventDefault.

### 4. Managed worker and deterministic fixture lifecycle

Launch and own the separate browser worker with fresh credentials and minimal environment. Worker must never inherit DB/provider credentials. The existing job only owns real descendants; a separately launched worker is not automatically protected. Ensure runtime, worker and Chromium cleanup behavior is real.

Use only the reviewed deterministic fixture. Fixture must survive the runtime crash used for reconciliation proof; do not tie fixture lifetime to runtime ownership.

### 5. Focused task and trusted approval UI

Add explicit create/search/prepare controls, task timeline, persisted booking preview, reject/approve and permitted execution controls. No state-changing/external operations from mount, polling, reconnect or model/page text. Invalidate stale/expired previews.

For changed price or missing slot, show approved/current facts clearly and require a new review; do not book silently. Distinguish FAILED from OUTCOME_UNKNOWN. Unknown has no Retry button; reconciliation checks the existing result read-only. Include expandable task/action/attempt IDs, revisions, sequences, timestamps and digest for verification. Avoid unrelated redesign.

### 6. Durable restoration and event replay

Persist active task ID in main, keeping Python authoritative. Restore the SAME task after renderer reload or full app restart. Fetch persisted events after_sequence, page until exhausted, deduplicate/order by sequence, reject stale in-flight results and handle generation change. Never invent recovery state or automatically retry a mutation.

### 7. Real Electron acceptance and final review

Drive actual rendered controls through preload/main, not only runtime HTTP APIs. Isolate app profile and test database. Implement normal booking and lost-response/hard-restart scenarios.

Both require exact counts: 1 task, 1 action, 1 attempt, 1 browser submission, 1 booking, 0 duplicates.

Lost-response scenario: wait until fixture accepted booking, hard-kill the relevant runtime/worker/Electron process, restart the same profile, restore same task, display OUTCOME_UNKNOWN, let user reconcile read-only to SUCCEEDED; counts unchanged, no second submit or attempt.

Cover changed price, missing slot, stale approval, unsafe response sanitization and lifecycle races. Then independent security/source review, fix concrete findings, rerun meaningful tests and all critical acceptance checks:

- npm.cmd run typecheck
- npm.cmd test
- npm.cmd run build
- In services/agent: uv run pytest -v
- In services/agent: uv run mypy
- In services/agent: uv run alembic upgrade head (configured intended database)
- New real Electron integration tests

npm.cmd run package is also a verified repo command, but packaged sidecar bundling is explicitly deferrable. Do not claim packaging was tested. Keep known baseline failures separate from regressions. Integrate the reviewed implementation into primary only deliberately, preserving manager documents.

## Contract and ledger facts to avoid subtle mistakes

- TaskStatus: CREATED, PLANNING, READY, WAITING_APPROVAL, EXECUTING, VERIFYING, OUTCOME_UNKNOWN, RECONCILING, SUCCEEDED, FAILED, CANCELLED, PAUSED.
- ActionStatus: PROPOSED, WAITING_APPROVAL, APPROVED, REJECTED, EXECUTING, SUCCEEDED, FAILED, OUTCOME_UNKNOWN, RECONCILING.
- ApprovalStatus: PENDING, APPROVED, REJECTED, CONSUMED. AttemptOutcome: SUCCEEDED, FAILED, OUTCOME_UNKNOWN. RiskTier: R0–R3.
- BookingProposal contains site, slot_id, doctor, timezone-aware time, integer price and three-uppercase-letter currency. There is NO specialty field; do not trust/invent one from renderer input.
- changed_facts entries are field/approved/observed, with fields slot_id/doctor/time/price/currency. Validate stored results before projecting.
- Raw ActionResponse proposal/results and event payloads are arbitrary JSON; never forward wholesale to renderer.
- Reconciliation updates ACTION and emits action.reconciled with result/evidence/reason. The original attempt intentionally remains OUTCOME_UNKNOWN with its original result. UI final truth comes from action plus reconciliation event, not only the attempt row.
- Reconciliation evidence includes source/site/reference/operation/observation_id/lookup and booking or authoritative absence. Define safe typed projections.
- Action success/failure/rejection returns owning task to READY; do not conflate action SUCCEEDED with task SUCCEEDED.
- ActionResponse.approval is only the current live approval. Consumed/rejected history is in events.
- Events are ordered by sequence; cursor means sequence > after_sequence, page limit <=500. TaskResponse currently lacks repository last_event_sequence.
- Event types include task.created, task.cancelled, action.proposed, action.approval_requested, action.approved, action.rejected, action.execution_started, action.succeeded, action.failed, action.outcome_unknown, action.reconciliation_started, action.reconciled.
- Reconciliation dispatch uses attempt_id=null, operation=lookup_booking, effect=READ_ONLY; it does not create a second attempt. BrowserDispatchResponse does not include the stored dispatch result.
- Startup recovery marks unfinished old-generation attempts unknown and closes orphan dispatches before serving; never retries them.
- Existing DB triggers/constraints enforce immutable proposal/digest binding/single-use approval.
- Closed worker registry: search_appointments, read_available_slots, prepare_booking, commit_booking, lookup_booking.
- Existing local PendingActionStore is in-memory, two-minute expiry, cleared on quit. Preserve this separate ownership boundary.

## Non-negotiable invariants

1. Immutable proposal. 2. Renderer cannot approve a modified proposal. 3. Single-use approval. 4. Approval cannot fund two attempts. 5. Persist attempt before consequential dispatch. 6. Never retry unknown outcome. 7. Reconciliation read-only. 8. Page text has no authority. 9. Worker cannot approve. 10. Renderer cannot contact worker. 11. Renderer cannot contact FastAPI. 12. Worker has no DB credentials. 13. Renderer has no runtime credentials. 14. No generic browser/IPC/runtime execution surface.

All state-changing or external operations require explicit renderer confirmation before main executes them. Screen capture stays user initiated, file searches stay within approved roots, and secrets never enter renderer or committed files.

## Local setup and operational notes

- uv executable: C:\Users\SATISH\AppData\Local\hermes\bin\uv.exe
- Primary Python venv exists at services\agent\.venv\Scripts\python.exe. Implementation worktree currently has NO .venv; provision a proper local environment there for the supervisor's fixed path before desktop testing.
- Both primary and worktree have ignored services/agent/.env files. Never print or commit their values. No secrets are copied into this handoff.
- Primary node_modules exists; worktree npm commands previously resolved ancestor dependencies.
- Python Playwright and Chromium are installed. Node playwright/playwright-core were not installed at the checkpoint. Python Chromium connect_over_cdp can be an option for a test-launched Electron, or add an appropriate dev dependency.
- PostgreSQL was available for successful baseline tests. DB tests truncate/downgrade the TEST database. Verify the test DB URL ends in _test and never run DB suites concurrently or against production data.
- Initial restrictive sandbox access issues with Vite/uv cache were environment issues, resolved for authorized checks; distinguish them from product defects.
- Old accidental partial primary writes were backed up under ignored out/m4-draft-backup and then restored/removed after provenance checks. Do NOT resurrect those drafts; commit 72d4799 is authoritative. Primary contains documentation changes only.

## Final reporting expected after actual completion

Use the original request for full reporting requirements. Explain files/architecture, auth and lifecycle, domain client/contracts/IPC, trusted UI, approval and unknown-outcome behavior, restoration, exact E2E counts, executed checks and baseline failures, review fixes, all 14 invariants, limitations/deferred scope and Milestone 5 recommendation. Do not call Milestone 4 complete until its real Electron acceptance and final review pass.
