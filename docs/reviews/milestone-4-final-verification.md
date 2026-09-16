# Milestone 4 — implementation completion and final verification

Branch `codex/m4-secure-runtime-bridge`, continuing from slice-1 commit
`72d4799`. Implementation of slices 2–7 and the final review were done by
Claude Code (Opus 5) after the handoff in the primary checkout
(`docs/CLAUDE-CODE-HANDOFF-M4.md`). The manager plan, handoff and earlier review
record in the primary checkout are unchanged by this branch.

## What was added

| Area | Files |
| --- | --- |
| Runtime-owned browser worker | `app/browser/managed.py`, `app/browser/main.py` (port-0 bind, stdout readiness, parent watchdog) |
| Read-only preparation | `app/services/booking_preparation.py`; `POST /tasks/{id}/booking/search`, `POST /tasks/{id}/booking/prepare` |
| Revision-bound execution/reconciliation | `app/services/browser_execution.py`, `app/api/routes.py` |
| One live booking per task | `ActionService.propose_exclusive_action` (checked under the task row lock) |
| Contract drift | `app/api/contract.py` → `src/shared/agent-runtime-contract.json`; `tests/test_desktop_contract.py`, `src/main/services/agent-wire.test.ts` |
| Desktop DTOs + API | `src/shared/agent-contracts.ts` |
| Main-owned domain client | `src/main/services/agent-tasks.ts`, `agent-wire.ts`, route allowlist + `request()` in `agent-runtime-supervisor.ts` |
| IPC / preload / sender checks | `src/main/services/agent-ipc.ts`, `ipc-sender.ts`, `src/preload/index.ts`, `src/main/index.ts` |
| CSP | `src/main/services/content-security-policy.ts`, `electron.vite.config.ts` |
| Trusted task/approval UI | `src/renderer/src/components/AgentTaskPanel.tsx`, `agent-task-view.ts` |
| Real Electron acceptance | `services/agent/tests/test_electron_acceptance.py` |
| Test auth adaptation | `tests/browser_harness.py` (`RuntimeHttp`, `app.server` launch), lost-response and recovery tests |

## Findings during this pass (all fixed)

| Severity | Finding | Fix |
| --- | --- | --- |
| HIGH | `uv run alembic upgrade head` failed: `alembic/env.py` built the full runtime `Settings`, which since slice 1 requires `LUMI_RUNTIME_TOKEN`. | `DatabaseSettings` for migrations; subprocess regression test without the token. |
| HIGH | `browser-execution` ignored the reviewed revision; approval claim was not bound to what the user saw. | Optional `expected_revision` enforced by `start_attempt` inside the claiming transaction; early refusal before contacting the worker. |
| HIGH | A new booking could be proposed while an earlier one was `OUTCOME_UNKNOWN`, side-stepping "never retry an unknown outcome". | `propose_exclusive_action` under the task lock; main also refuses to replace/close a task with an unresolved booking. |
| MEDIUM | Reconciliation could dispatch a lookup for actions that were not unknown; two concurrent checks could both write a verdict. | Status guard before any lookup; verdict written against the revision the check started from. |
| MEDIUM | A process denied the Electron single-instance lock could continue into `whenReady` and start a runtime. | Early return in `whenReady`. |
| MEDIUM | `will-navigate` used prefix matching; IPC checks accepted any frame of the window. | Structural URL comparison; exact sender, top frame and app URL required for every IPC handler; subframe navigation and webviews refused. |
| MEDIUM | No renderer CSP. | Production meta CSP (no loopback in `connect-src`, only `https://api.openai.com`); dev header scoped to the exact Vite origin. |
| MEDIUM | Worker token comparison raised on non-ASCII input. | Byte comparison with ASCII guard; regression test. |
| LOW | Managed worker parent-liveness context was garbage-collected, closing its handle and exiting the worker. | Retained on the server object (found by the managed-worker test). |
| LOW | Active-task pointer to a task PostgreSQL no longer has kept producing errors. | Cleared only on a definite `task_not_found`. |

Searches for trust-boundary regressions found no renderer/preload reference to
loopback addresses, runtime/worker credentials, `DATABASE_URL`, `/v1/dispatch`
or lifecycle routes (enforced by `agent-boundary.test.ts`); no `shell: true`;
no generic IPC, route, URL, SQL, evaluate or dispatch surface. The supervisor
refuses every route not on its allowlist, including `/lifecycle/shutdown` via
`request()`, attempt/finish/reconciliation-finish and dispatch listings.

## Verification (2026-09-16, Windows)

| Check | Result |
| --- | --- |
| `npm.cmd run typecheck` | Pass |
| `npm.cmd run build` | Pass (CSP meta present in `out/renderer/index.html`) |
| `npm.cmd test` | 1566 passed, 17 skipped; 3 known baseline failures only (`real-inference.test.ts`, `tokenizer-pack.test.ts`, `accessibility.test.tsx:73` CRLF) |
| `uv run pytest -v` | 373 passed, 2 skipped (the two opt-in Electron tests, run separately below) |
| `uv run mypy` | Pass, 84 files |
| `uv run alembic upgrade head` (configured development DB) | Pass, at `0003 (head)` |
| `LUMI_ELECTRON_E2E=1 uv run pytest tests/test_electron_acceptance.py` | 2 passed |
| Dev-mode smoke (`npm run dev`, CDP) | Renders, HMR connects, CSP header applied, renderer `fetch` to loopback blocked, runtime connected |

Electron acceptance counts (PostgreSQL + fixture authoritative state):

| Scenario | Tasks | Actions | Attempts | Consequential dispatches | Browser submissions (fixture) | Bookings | Duplicates |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Normal booking | 1 | 1 | 1 | 1 (submitted) | 1 | 1 | 0 |
| Lost response + hard kill of Electron + restart | 1 | 1 | 1 | 1 (closed unknown by recovery) | 1 | 1 | 0 |

In the restart scenario the ledger's dispatch row keeps `submitted=false`
because the dead runtime never observed the submission (by design); the fixture
counts the single real submission. The only extra dispatch is one read-only
`lookup_booking` with no attempt. The normal scenario additionally covers
renderer reload restoration, changed price (approved ₹950 vs current ₹1,100,
nothing booked, new review creates a new action), and a missing slot.

## Invariants

1–5 (immutable proposal, no renderer-modified approval, single-use approval,
one approval → one attempt, attempt persisted before dispatch) are unchanged
ledger/database guarantees; approval input from the desktop is action id +
revision only, and preparation values come from worker observation.
6–7: unknown outcomes expose only a read-only check; no retry path exists in
UI, main or runtime. 8–9: page text reaches the proposal only as typed slot
fields; the worker has no approval surface. 10–11: CSP and the boundary test
keep the renderer off loopback; only main holds runtime credentials. 12: the
managed worker's environment is allowlisted and tested. 13: credentials never
cross preload. 14: no generic browser/IPC/runtime surface.

## Limitations / deferred

- Packaged builds do not bundle the Python runtime; the panel reports it as
  unavailable. `npm run package` was not exercised.
- A compromised renderer can still press the approve button for the *displayed*
  booking (as with the existing local confirmation flow); it cannot change what
  is booked.
- One runtime per Windows session and per database; the fixture is the only
  reviewed site. Read-only discovery dispatches are logged, not stored in the
  per-action dispatch ledger (no action exists yet).
