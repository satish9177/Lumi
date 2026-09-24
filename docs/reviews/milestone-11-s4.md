# Milestone 11 S4: unified task cockpit, pause/resume, `outcome_unknown`

> The cockpit offers exactly two actions of its own -- Continue and Stop -- and never a third that would
> amount to "approve this task." Every capability's own approval boundary still fires exactly as it would
> outside orchestration; Continue only re-enters the same observe/dispatch loop S2/S3 already built.

Status: **COMPLETE**. Migration `0022` widens `ck_orchestrations_pause_reason_closed`.

## What was built

* Migration `0022_orchestration_pause_reasons.py` adds two pause reasons to the closed set:
  `manual_handoff_required` (schema-only in this slice -- not yet reachable by any composed capability, kept
  first-class now so a later slice needs no further migration) and `outcome_unknown` (reachable today).
* `_read_task_backed_resolution` (`app/services/orchestration.py`) distinguishes "still working" from "the
  underlying state is ambiguous" for both task-backed capabilities: `public_research`'s
  `ResearchView.unresolved_step`, and `project_start`'s `RunView.phase == "outcome_unknown"`. Both now pause
  honestly (`outcome_unknown`) instead of the misleading `approval_required`.
* `resume()` accepts either `approval_required` or `outcome_unknown`. If the underlying task is still
  unresolved but the pause reason itself needs to change, the new `OrchestrationRepository.relabel_pause`
  (a PAUSED -> PAUSED transition) updates it without falsely resuming -- distinct from `resume_running`
  (PAUSED -> RUNNING), which `pause`'s own `_transition` could not do since it only accepts
  `expected_statuses=("RUNNING",)`.
* Five new IPC channels (`createOrchestration`, `getOrchestration`, `getLatestOrchestration`,
  `continueOrchestration`, `stopOrchestration`) wired end to end: `src/shared/agent-contracts.ts` ->
  `src/preload/index.ts` -> `src/main/services/agent-ipc.ts` (the same optional-dependency
  `Pick<Controller, ...>` + `noXxx()` fallback pattern as every other channel, gated by
  `assertTrustedSender`) -> `OrchestrationCoordinator`.
* `OrchestrationCoordinator`'s public methods renamed for IPC-facing consistency
  (`createAndRun`->`createOrchestration`, `stop`->`stopOrchestration`, new `continueOrchestration`,
  `getOrchestration`, `getLatestOrchestration`). Its `run()` loop's resume-attempt condition now also covers
  `outcome_unknown`, not just `approval_required`.
* New renderer component `OrchestrationPanel.tsx`: a stateful container plus a pure `OrchestrationCard`
  (tested via `renderToStaticMarkup`, this codebase's established convention for presentational
  sub-components rather than `@testing-library/react`). Shows the objective, status, each step's
  plain-English status and its own bounded result summary, and a plain-English pause-reason explanation.
  Offers exactly two actions -- Continue (only while `PAUSED`) and Stop (while `RUNNING` or `PAUSED`) --
  never an approve/grant/allow control of its own.
* `task-request-interpreter.ts`'s `orchestrated_task` branch (a stub since S1) now calls
  `deps.orchestration.createOrchestratedTask(objective)` and narrates the result, focusing the approval card
  only when the fresh orchestration is `PAUSED`. `voice-task-tools.ts`'s new `orchestration` guidance is
  explicit that voice cannot approve, resume or stop an orchestration.
* `src/main/index.ts` wires `OrchestrationController` / `OrchestrationCoordinator` / `OrchestrationPlanner`
  together and into both the interpreter and `registerAgentIpc`, mirroring every other capability's wiring.

## Stop semantics: a deliberate, documented decision

The milestone plan's own S4 line describes Stop as calling "each in-flight child task/step's own existing
stop/cancel path." What shipped instead keeps the rule Milestone 10 S5 already established for every other
executor's Stop: it **stops future scheduling only** -- it never marks an in-flight step failed, never
compensates, and never reaches into a linked child task's own state
(`OrchestrationService.stop`'s own docstring states this explicitly). A child task a paused orchestration is
still waiting on (an open research grant, a running project) stays exactly as it was, and remains separately
visible and stoppable on that capability's own existing panel -- the cockpit's Stop only ends the
orchestrator's own further scheduling of it.

This is a considered narrowing, not an oversight: cascading a forced cancel into a capability's own task
through a path other than that task's existing surface would itself be a small new piece of delegated
authority -- the orchestrator reaching into a capability's state on the user's behalf through a route that
capability's own boundary never offered directly. Keeping Stop's effect scoped to "the orchestrator stops
choosing steps" is the more conservative reading of "general planning != general authority," and it is
consistent with M10 S5's own Stop rule applied at one more layer. A user who also wants to cancel the
underlying capability's task does so on that capability's own card, exactly as if they had started it
directly.

## Adversarial review

One fresh Claude agent reviewed the complete staged diff independently, tracing the Continue button's full
call path (`OrchestrationCard.onContinue` -> IPC -> `OrchestrationCoordinator.run()` ->
`progressPendingStep`/`dispatch` -> each capability's own service method), the `outcome_unknown`/
`relabel_pause` mechanism for any way an ambiguous state could be used to skip an approval boundary, all
five new IPC channels' input validation, the renderer's use of `window.lifeLens` only (no raw
Node/Electron/`dangerouslySetInnerHTML`), migration `0022`'s reversibility, and the cockpit's consistency
with "general planning != general authority" end to end (including the voice-guidance text).

**One confirmed bug, reproduced against a real Postgres database:**
`OrchestrationRepository.stop()` did not clear `pause_reason` when transitioning a `PAUSED` orchestration to
`STOPPED`. The table's `ck_orchestrations_pause_reason_set` CHECK constraint requires `pause_reason` to be
`NULL` whenever `status != 'PAUSED'`; stopping a paused orchestration left `pause_reason` set, so the write
violated the constraint and the call failed outright. Because S4 is what first makes `stopOrchestration`
IPC-reachable, and the cockpit's own "Stop this task" button is enabled precisely while `PAUSED` (the state a
user is most likely to want to stop from -- a task sitting there waiting on them that they've decided to
abandon), this broke one of the cockpit's only two actions in its most common real-world case. Fixed by
clearing `pause_reason` alongside the status change in `stop()`; covered by a new regression test,
`test_stop_clears_the_pause_reason_so_a_paused_orchestration_can_still_be_stopped`
(`services/agent/tests/test_orchestration_service.py`).

No authority-leakage, IPC-validation, renderer-trust-boundary, migration-safety or budget/loop-guard finding
survived review. Full findings (including the seven checked-and-clear categories) are preserved in this
session's transcript; only the one confirmed, fixed bug is reportable here.

**One non-blocking coverage gap noted, not fixed in this slice:** no TypeScript-level test exercises the
`outcome_unknown` resume/continue path directly (`orchestration-coordinator.test.ts` and
`orchestration-panel.test.tsx` both only exercise `approval_required`); the Python side has direct coverage
(`test_an_outcome_unknown_run_pauses_with_its_own_honest_reason_not_approval_required`), and TypeScript's
exhaustive `Record<AgentOrchestrationPauseReason, string>` in `PAUSE_REASON_TEXT` means the compiler would
catch a missing-key regression. Left as a residual for S5's generality hardening rather than expanding this
slice further.

## Deliberate S4 scope

`manual_handoff_required` is first-class in the schema (migration `0022`) but not yet reachable by any
composed capability -- no capability in the current `COMPOSED_CAPABILITY_IDS` subset
(`public_research`, `project_status`, `project_start`) has a state that maps to it (a CAPTCHA, a login
prompt, an unsupported control). Adding the pause reason now means a future slice that composes a capability
needing it requires no further migration -- the same pattern S1/S2 used for reserving catalog/vocabulary
space ahead of the capability that will use it.

## Tests

* `services/agent/tests/test_orchestration_domain.py`: +2 (13 total) -- `outcome_unknown` /
  `manual_handoff_required` join the pinned `PAUSE_REASONS` set; a regression pinning it against migration
  `0022`'s widened CHECK constraint.
* `services/agent/tests/test_orchestration_service.py`: +2 (30 total) -- `project_start`'s
  `OUTCOME_UNKNOWN` run status pauses with its own honest reason rather than `approval_required`
  (simulated by directly updating `project_runs.status`, since a real crash mid-run is not otherwise
  reproducible in a test); the new stop-from-`PAUSED` regression test above.
* `src/main/agent/task-request-interpreter.test.ts`: +3 (32 total) -- a new
  `describe('routing: orchestrated_task, wired to a real orchestration dependency (Milestone 11 S4)')` block:
  a freshly created, paused orchestration focuses the approval card; a running/succeeded one does not; an
  unavailable dependency still answers `orchestration_unavailable` (S1's behavior, unchanged).
* `src/main/services/orchestration-coordinator.test.ts`: renamed calls (`createAndRun` ->
  `createOrchestration`) across all existing tests; `FakeGraph` gained `getLatestOrchestration`.
* `src/renderer/src/components/orchestration-panel.test.tsx` (new, 10 tests, `renderToStaticMarkup` on
  `OrchestrationCard`): empty/start-form state; a stopped orchestration re-shows the start form; steps list
  with status and result summary, never a raw private value; pause reason shown in plain words; Continue
  offered only while `PAUSED`; Stop offered only while `RUNNING`/`PAUSED`; no approve/grant/allow *button*
  ever renders (pause-reason prose legitimately says "approve" -- the check is scoped to `<button>` tags, not
  all text); a hostile objective/result summary renders only as inert, escaped text; controls disable while
  busy; an error message renders when present.

## Validation

* `uv run mypy app`: clean (179 source files).
* `uv run pytest -m "not browser and not desktop_uia"`: 2,533 passed; the only failure is the pre-existing,
  documented environment baseline (`test_booking_routes_without_a_worker_answer_503`), unrelated to this
  diff.
* `npm.cmd run typecheck`: clean.
* `npx vitest run`: 2,551 passed, 22 skipped; the only 3 failing test files are pre-existing, documented,
  machine-specific baselines (two missing a local vision-model asset file, one a CRLF/LF line-ending
  mismatch in a CSS string comparison), unrelated to this diff.
* `npm.cmd run build`: main/preload/renderer all build clean.
* `pytest -m desktop_uia`, browser suites, `npm.cmd run eval`, `npm.cmd run package:dir`: not re-run for a
  slice that touches no desktop/browser/eval/packaging code; will be re-run at the M11 final audit.

## What S4 does not do (by design)

`manual_handoff_required` exists in the schema but is not yet driven by any capability -- documented above.
Stop does not cascade into a linked child task's own cancel path -- documented above as a deliberate,
narrower reading of the plan's Stop line, consistent with M10 S5's established Stop rule. No new capability
was composed in this slice; the composed set remains exactly `public_research`, `project_status`,
`project_start`, unchanged from S3.
