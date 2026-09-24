# Milestone 11 S3: effectful capability composition

> No new effect primitive. Choosing `project_start` opens exactly the same R3 "this executes code" warning
> card a direct request opens, the same effect key and cross-executor lock apply, and the coordinator's own
> "auto-start" nudge cannot cause a run to start without that approval already having happened.

Status: **COMPLETE**. No migration (S3 adds no table; it composes one more capability over S2's existing
graph).

## What was built

* `project_start` joins `public_research` as a second **task-backed** composed capability
  (`app/domain/orchestration.py`'s `TASK_BACKED_CAPABILITY_IDS`, `EXPECTED_TASK_TYPE["project_start"] =
  "project_run_task"`). `OrchestrationService` gains a required `project: ProjectService` dependency,
  wired in `main.py` after `ProjectService` itself (ordering fixed so the dependency exists before use).
* `_read_task_backed_resolution`'s new `project_start` branch reads `ProjectService.describe()`'s
  `RunView.phase` -- the same closed vocabulary (`app/services/projects.py`'s `_phase()`) a direct request
  already uses -- and maps it exhaustively: `awaiting_approval` -> `AWAITING_APPROVAL`; the four "alive"
  phases (`starting`/`running`/`ready`/`succeeded`) -> `SUCCEEDED` (the effect happened; a later,
  independent `project_status` step reports live readiness); the five "ended badly" phases
  (`declined`/`expired`/`failed`/`stopped`/`ended_with_runtime`) -> `FAILED`; `approved` (grant active, run
  not yet started) and `outcome_unknown` fall through to `PENDING` -- conservative, never guessed.
* `OrchestrationCoordinator.dispatch()`'s new `project_start` branch: the planner names only the
  capability id, never a recipe. The coordinator resolves "the" registered recipe itself
  (`getRegisteredRecipeId()`, a dependency Electron main will implement in S4) and calls
  `createProjectRun(recipeId)` -- `ProjectService`'s own existing entry point, opening the R3 warning card.
  Nothing is approved yet.
* `OrchestrationCoordinator.progressPendingStep()` (new): when `run()` resumes a paused orchestration whose
  pending step is `project_start`, it calls `startProjectRun(childTaskId)` as a best-effort nudge before
  re-reading state. This is the one genuinely new mechanism S3 needed beyond S2's pattern -- research's own
  product code already drives its whole flow to completion after one grant; a project run needs a second,
  separate `start()` call that nothing else triggers automatically.

## Why the nudge cannot start an unapproved run

This was the review's central question, verified by tracing the real `ProjectService.start()`
(`app/services/projects.py`), not by trusting the coordinator's own comments:

1. `start()` re-reads the grant fresh and raises `ProjectRefusal("grant_not_active")` if
   `grant.status is not GrantStatus.ACTIVE` -- strictly before any run row is inserted, before an effect
   key is claimed, and before any process is spawned. A nudge against a `PENDING` (not yet approved) grant
   is a guaranteed no-op.
2. `start()` is idempotent by construction ("one start per approval, ever"): if a run already exists for the
   task it returns the existing view immediately, backed by the database's own partial unique index
   (one live run per *project*, not per task) -- so neither a repeated nudge nor a second orchestration
   racing to start the same project can produce two runs.
3. The effect lock (`PROJECT_RUN`, a global-tier kind in `app/domain/effects.py`) is claimed inside
   `ProjectService._start()` itself, exactly as a direct request's would be. `OrchestrationService` never
   touches the action ledger or the effect lock directly -- there is no route around
   `ActionService._claim_effect_keys` through the orchestrator.

## Adversarial review

One fresh Claude agent reviewed the complete staged diff independently, reading `ProjectService.start()`
line by line (the grant check, the idempotency check, the effect-key claim, all confirmed to run in that
order before any spawn), the effect registry (`GLOBAL_TIER`, `TOOL_PROJECT_START`), the database's own
one-live-run-per-project constraint, and the phase-mapping's exhaustiveness against `_phase()`'s full
12-value vocabulary. It also confirmed the planner can never name a `recipe_id` -- `dispatch()`'s
`project_start` branch takes no planner input beyond the capability id.

**Findings: none at reportable severity.** One non-blocking observation: the Python integration tests
exercise `OrchestrationService`'s read/resolution logic against a real `ProjectService` and a real
`node.exe` process, while the TypeScript coordinator tests exercise `progressPendingStep()`'s plumbing
against a mocked `startProjectRun`. The "never starts unapproved" property is therefore proven by combining
the two (TS: the call happens with the right task id, gated on the right capability; Python: `start()`
itself refuses an inactive grant) rather than by one true end-to-end test -- expected, since no IPC wiring
exists yet to drive a real cross-process test, and unchanged from S2's own scope boundary.

## Deliberate S3 scope

`project_start` was chosen as S3's one new effectful capability because it reuses S2's exact task-backed
pattern (link an existing task; read resolution from that capability's own service) with only one genuinely
new piece (the `progressPendingStep()` nudge, needed because starting is a separate call from granting,
unlike research). The remaining catalog capabilities stay deliberately uncomposed:

* `launch_registered_app` uses a different authority shape entirely -- an exact per-action approval on the
  ordinary `actions` ledger (M9 S3), not a task/grant pair -- and needs its own resolution-reading pattern.
* `download_document`, `place_downloaded_file`, `document_read`, `form_prepare`, `workflow_prepare` all need
  the "trusted available-refs" mechanism flagged as separable work in the S2 review: a way to show the
  planner which pre-approved documents or transfers it may reference by id, since a planner cannot invent a
  path or URL itself.
* `project_stop` needs the coordinator to resolve "which run" from a *prior* step's own child task within
  the same orchestration -- a cross-step reference derivation not yet built.

Acceptance E's full three-capability chain ("Open VS Code and start Lumi, then tell me when it is healthy")
is not yet demonstrable end to end in this slice (it needs `launch_registered_app` too); `project_start` ->
`project_status` alone is. No IPC channel, preload method or renderer UI exists yet -- unchanged from S2,
still explicitly S4's job.

## Tests

* `services/agent/tests/test_orchestration_service.py`'s new `TestProjectStartCapability` (4 tests, real
  PostgreSQL, a real synthetic Node.js project and a real `node.exe` process -- the same rig
  `test_projects_service.py` uses, skipped on non-Windows): a freshly created run pauses for the warning
  card; `resume` settles `SUCCEEDED` once the grant is confirmed and `start()` is called, with the result
  handle and summary correctly populated; a declined warning card settles `FAILED`, never stuck; a task of
  the wrong kind is refused.
* `src/main/services/orchestration-coordinator.test.ts`'s 4 new tests: `project_start` dispatch pauses for
  its warning card without ever calling `startProjectRun` on link; a missing registered recipe is refused
  rather than inventing one; resuming a paused `project_start` step calls `startProjectRun` with exactly
  that step's task id; resuming a paused step of any *other* capability never calls it.

## Validation

* `uv run mypy app tests`: clean (308 files).
* `uv run pytest -m "not browser and not desktop_uia"`: 2,531 passed; the only failure is the pre-existing,
  documented environment baseline (`test_booking_routes_without_a_worker_answer_503`), unrelated to this
  diff.
* `npm.cmd run typecheck`: clean.
* `npx vitest run`: 2,538 passed, 22 skipped; the only 3 failing files are the pre-existing, documented
  machine-specific baselines, unrelated to this diff.
* `npm.cmd run build`: main/preload/renderer all build clean.
* `pytest -m desktop_uia`, browser suites, `npm.cmd run eval`, `npm.cmd run package:dir`: not re-run for a
  slice that touches no desktop/browser/eval/packaging code; will be re-run at the M11 final audit.

## What S3 does not do (by design)

Five of the sixteen catalog capabilities remain composed (`public_research`, `project_status`,
`project_start`); eleven stay real-but-uncomposed, documented above. No capability aggregation occurred:
`project_start`'s approval is entirely separate from `public_research`'s, and choosing one never implies or
widens the other's authority. `project_stop`, `launch_registered_app`, and the document/form-related
capabilities are explicitly left for a later slice or a dedicated follow-up once the shared ref-plumbing
mechanism exists.
