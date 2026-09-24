# Milestone 11 S2: durable read-only orchestration

> The core property this slice exists to prove: **choosing a capability through the orchestrator never
> skips that capability's own existing approval.** Linking `public_research` shows the same trusted scope
> card a direct research request shows, and nothing is searched or read until the same trusted click.

Status: **COMPLETE**. Migration `0021` (current head, up from `0020`).

## What was built

* `orchestrations` / `orchestration_steps` (migration `0021`): a durable graph over general requests. It
  holds no authority of its own -- creating one grants nothing, and it is never a second action ledger. A
  step's `capability_id` is constrained by a database CHECK to the full Milestone 11 S1 catalog (16 ids,
  spelled identically to `src/shared/agent-capabilities.ts`, pinned by
  `test_orchestration_domain.py::test_the_catalog_matches_the_typescript_catalog_exactly`), whether or not
  this runtime can execute it yet.
* `app/domain/orchestration.py`: the closed vocabulary. `COMPOSED_CAPABILITY_IDS` is an honestly-scoped,
  strict subset of the full catalog -- exactly two capabilities this runtime actually knows how to compose
  today: `public_research` (task-backed) and `project_status` (synchronous, a pure read with no new task).
  A real catalog id outside that subset (e.g. `document_read`, `form_prepare`) pauses the orchestration with
  reason `capability_unavailable` rather than executing anything or crashing.
* `app/services/orchestration.py` (`OrchestrationService`): the deterministic controller. It holds no tool
  of its own:
  * a **task-backed** step only ever *links* a task id the **caller** already created through that
    capability's own existing service (`ResearchService`, exactly the same entry point a direct research
    request uses) -- it never creates, grants, or drives that task itself. Its resolution is read only from
    `ResearchService.describe()`, never guessed;
  * a **synchronous** step (`project_status`) records a bounded result the caller already computed through
    that capability's own existing read method (`ProjectController.getLatestProjectRun`), with no new task
    and no approval;
  * budgets (`MAX_STEPS=20`, `MAX_CHILD_TASKS=10`, `MAX_PLANNER_CALLS=20`) and a repeat-capability loop
    guard pause (`budget_exhausted` / `loop_detected`) rather than silently widening a limit or guessing a
    step;
  * every mutating call is `expected_revision`-fenced under a row lock (`SELECT ... FOR UPDATE`), and an
    expired orchestration (`expires_at`, 30-minute TTL) refuses a new step.
* `app/api/orchestration_routes.py`: `POST /orchestrations`, `GET /orchestrations/latest|{id}`,
  `POST /orchestrations/{id}/planner-call|advance|resume|finish|stop`. No route accepts a path, URL,
  command or arbitrary tool payload; `advance` takes only a closed `capability_id` plus exactly one of
  `task_id` (task-backed) or `resolved_summary` (synchronous).
* `src/main/agent/orchestration-planner.ts` (`OrchestrationPlanner`): structurally identical to
  `research-planner.ts` -- one capability id per call, closed JSON schema, no free field. Not in
  `PRIVATE_TASK_CLASSES`: its context is controller-authored step summaries only, never a private
  capability's own raw evidence.
* `src/main/services/orchestration-coordinator.ts` (`OrchestrationCoordinator`): the "observe -> choose one
  capability -> controller validates -> capability runs through its normal boundary -> persist result ->
  re-plan" loop, bounded client-side at 20 iterations (defensive; the server's own budget pauses first in
  the ordinary case). `dispatch()` is the one closed place a capability id becomes a request to that
  capability's own existing entry point; anything outside its two named branches is refused
  (`orchestration_refused`), never silently skipped.

## Deliberate S2 scope

Only `public_research` and `project_status` are composed in this slice -- the two capabilities that need no
additional trusted-ref plumbing (an objective string is sufficient; neither needs a pre-established document
or desktop reference). `document_read`, `document_compare`, `desktop_observe`, `desktop_reason` and
`account_read` remain real, catalog-valid ids that the engine is already built to accept generically, but
their concrete capability handlers are not yet wired. This is a considered scope decision, not an oversight:
composing them needs a "trusted available-refs" mechanism (showing the planner which pre-approved documents/
surfaces it may reference by id) that is real, separable work, planned before S3 needs the same mechanism for
its own effectful compositions.

No IPC channel, preload method or renderer UI exists yet: the typed request interpreter still answers
`orchestration_unavailable` for a general request (S1's behavior, unchanged). Reachability from a real UI,
resumability across a restart, and pause/manual-handoff presentation are Milestone 11 S4's job
("unified task cockpit"), not S2's.

## Adversarial review

One fresh Claude agent reviewed the complete staged diff independently. It traced the "approval is never
skipped" property end to end rather than trusting docstrings: `dispatch()`'s `public_research` branch calls
`createResearchTask` (confirmed in `agent-tasks.ts` that this only creates a task and grants nothing), then
`advanceOrchestration(..., { taskId })`; Python's `advance()` reads the task's actual grant state via
`ResearchService.describe()` and pauses (`approval_required`) whenever it is missing or `PENDING`, clearing
`available_capabilities` to empty. It independently verified `test_a_freshly_created_unprepared_task_pauses_
for_approval` and `test_resume_settles_the_step_once_the_task_is_answered_and_never_before` are not vacuous
-- the latter walks the *entire* real research boundary (`prepare` -> `confirm` -> `execute_step` ->
`record_answer`) before the orchestrator is allowed to see a resolved step.

It also verified: budgets/loop-guard/revision/expiry all check under the same row lock before any write
(optimistic concurrency is real, not decorative); the TypeScript side re-validates every capability id at
three independent layers (wire parsing, planner schema, coordinator dispatch); `dispatch()` fails closed for
any capability outside its two named branches (proven by a coordinator test offering `document_read` as
"available" and asserting refusal); a failed/unavailable planner never advances durable state further; and
the mechanical `conftest.py`/migration-test changes are exactly what they claim (excluding tables that
genuinely do not exist yet at an older migration revision under test).

**Findings: none at reportable severity.** Two informational observations were noted and did not warrant a
code change:

1. `OrchestrationService._view()`'s `assert record is not None` (Python `assert` is strippable under `-O`)
   is unreachable with a false condition in production -- no delete endpoint exists for `orchestrations`,
   and every caller either just inserted the row in the same transaction or confirmed it moments earlier on
   the same connection -- and matches the existing `assert isinstance(exc, ...)` pattern already used
   throughout `app/api/errors.py`'s own exception handlers.
2. `_read_task_backed_resolution`'s read happens outside the write transaction that later inserts/resolves
   a step, so the value can be a few milliseconds stale. This can only make the orchestration pause *more*
   conservatively than strictly necessary (never less), and self-corrects on the next `resume()` call --
   not a security issue.

## Tests

* `services/agent/tests/test_orchestration_domain.py` (16 tests): catalog identity against the pinned
  TypeScript spelling, no-go-fragment scan, composed-is-a-strict-subset, objective/capability-id validation,
  `bounded_summary` normalization (matching `context-builder.ts`'s own control-character convention), result
  handle formatting.
* `services/agent/tests/test_orchestration_service.py` (20 tests, real PostgreSQL): create/describe/latest;
  closed-catalog and uncomposed-capability handling; the synchronous `project_status` path (approval-free,
  no task); the task-backed `public_research` path end to end (unprepared -> `AWAITING_APPROVAL` ->
  real `prepare`/`confirm`/`execute_step`/`record_answer` -> `resume` settles `SUCCEEDED`); a declined scope
  settling `FAILED` rather than hanging forever; cross-orchestration task-linking exclusivity; the
  repeat-capability loop guard; the step budget (seeded via direct SQL, since only 2 composed capabilities
  makes the loop guard fire first in ordinary use -- documented, not hidden); `finish`/`stop`/expiry
  fencing; the planner-call budget.
* `src/main/agent/orchestration-planner.test.ts` (13 tests): schema/injection resistance -- a capability
  outside the closed catalog, a real catalog id outside the orchestration's own currently-available set, and
  every extra field (path/URL/command/approval/task_id) are all refused.
* `src/main/services/orchestration-coordinator.test.ts` (8 tests): closed dispatch, approval-pause dispatch
  for `public_research`, synchronous dispatch for `project_status`, planner-stop, planner-failure handling,
  resume-then-continue, the bounded loop, and refusal of a capability with no dispatch handler.
* `src/main/services/orchestration-wire.test.ts` (12 tests): the runtime response is untrusted until parsed
  -- a capability id outside the catalog anywhere in the response is rejected, a malformed/missing required
  field is rejected outright, error-code projection is total.

## Validation

* `uv run mypy app tests`: clean (308 files).
* `uv run pytest -m "not browser and not desktop_uia"`: 2,527 passed; the only failure is the pre-existing,
  documented environment baseline (`test_booking_routes_without_a_worker_answer_503`, unrelated to this
  diff -- this machine's `services/agent/.env` sets `LUMI_PUBLIC_INSPECTION_HOSTS`).
* `npm.cmd run typecheck`: clean.
* `npx vitest run`: 2,534 passed, 22 skipped; the only 3 failing files are the pre-existing, documented
  machine-specific baselines (`real-inference.test.ts`, `tokenizer-pack.test.ts`,
  `accessibility.test.tsx`'s scam-card CSS assertion), unrelated to this diff.
* `npm.cmd run build`: main/preload/renderer all build clean.
* `src/shared/agent-runtime-contract.json` regenerated (`python -m app.api.contract --write`) to add the two
  new error codes; the diff is exactly those two additions, verified with `git diff --stat`.
* `pytest -m desktop_uia`, browser suites, `npm.cmd run eval`, `npm.cmd run package:dir`: not re-run for a
  slice that touches no desktop/browser/eval/packaging code; will be re-run at the M11 final audit.

## What S2 does not do (by design)

Fourteen of the sixteen S1 catalog capabilities are not yet composed (documented above). No IPC/preload/
renderer surface exists for orchestration; a general request still answers `orchestration_unavailable`
through the typed interpreter. No effectful capability may be chosen (S3's job). Finer-grained loop
detection (materially-equivalent state, A-B-A oscillation) beyond the simple repeat-capability guard is
deferred to Milestone 11 S5, which owns generality hardening explicitly.
