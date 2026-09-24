# Milestone 11 S5: generality evals + final cross-slice audit

> Two independent fresh-Claude passes over the complete M11 diff (045c036..lumi-m11, all five slices)
> found the authority model sound end to end and one confirmed, reproduced correctness bug -- now fixed.

Status: **COMPLETE**. No migration (S5 adds eval coverage and one service-layer fix, no schema change).

## Generality eval suite

Added a 20-case `orchestration` category to `scripts/run-evals.mjs`, the same deterministic, CI-safe
harness every prior milestone's eval cases live in (`docs/EVALS.md`) -- each case is a named, existing
automated test, not a new bespoke framework. Milestone 11's actually-composed capability set is
`public_research`, `project_status`, `project_start` (S1-S4 review docs document this as a deliberate
narrowing from the plan's full 16-entry catalog); breadth here is therefore proven by correct **refusal**
of every uncomposed, unknown, or malformed choice, not by executing domains (documents, accounts, desktop)
the orchestrator does not yet reach. The cases cover every metric the plan names for S5:

* **Task completion** across all three composed capabilities.
* **Correct capability selection**: dispatch always calls that capability's own real entry point.
* **Wrong-capability attempts**: a real-but-uncomposed id pauses (`capability_unavailable`); an unknown id
  is refused outright (`capability_unknown`).
* **Malformed-planner-output rate**: non-object/malformed JSON and any extra field (path/URL/command/
  approval) refused.
* **Planner call count / budget**: `MAX_PLANNER_CALLS` counted and paused at the bound.
* **Approval count**: both task-backed capabilities pause for their own card, never auto-approved.
* **Stale-step rejections**: a stale revision is refused; re-choosing an already-succeeded capability
  pauses as a loop, never a second step.
* **Effect-lock blocks**: a new regression test (`orchestration-coordinator.test.ts`) proves a
  `project_start` refused by the capability's own effect lock/one-live-run guarantee surfaces that exact
  refusal through the orchestrator, never bypassed or retried a different way.
* **Recovery**: resume re-checks durable state rather than re-choosing; an ambiguous run outcome pauses
  with its own honest `outcome_unknown` reason; and (added mid-slice, see below) resume refuses rather than
  reviving an expired orchestration.
* **Provider disclosure count**: the planner is shown only trusted, controller-authored facts, never a raw
  private value.

`manual_handoff_required` has no eval case: it is schema-only as of M11 (migration `0022`, S4), reachable
by no composed capability -- its count is honestly 0, not simulated.

Fixed a latent bug in the harness itself while wiring these cases in: its pytest matcher checked
`moduleName.endsWith(classname)`, which can only ever match a top-level test function (`classname ==
module`) and silently never matched a test defined inside a class. Every eval case added before this one
happened to reference module-level functions, so the bug never surfaced; the orchestration suite's tests
(mostly defined inside `Test*` classes, following this codebase's own convention) are the first to need it
fixed. Corrected to `classname === module || classname.startsWith(module + '.')`.

**139/139 eval cases pass** (`node scripts/run-evals.mjs`), up from the pre-M11 baseline of 118/118.

## Final cross-slice audit

Two independent fresh Claude agents, each with no shared context, reviewed the complete M11 diff
(`git diff 045c036..lumi-m11`, the base commit before M11 started through the current tip) end to end,
matching the M9/M10 audit process exactly.

### Pass A: authority, privacy/data lineage, provider routing, prompt injection, resume

Traced all three composed capabilities from "planner names this id" to "the capability's own service
method executes" and confirmed no aggregation, no skipped approval, no new authority at any hop, including
under the cockpit's Continue/Stop actions and under a race (every mutating write is revision-fenced under
`SELECT ... FOR UPDATE`). Confirmed the planner is shown only controller-authored bounded summaries, never
raw private content, for all three composed capabilities. Confirmed `orchestration_planning` is correctly
absent from `PRIVATE_TASK_CLASSES` given today's composed scope. Confirmed the planner's closed-schema
enforcement is structural (re-validated in code, not model-trusted) even against an adversarial result
summary. Independently re-verified `resume()` never trusts caller-asserted state, always re-reading live.

**No findings in scope.** One forward-looking, non-blocking note for whichever future slice composes a
privacy-sensitive capability (`account_read`/`desktop_reason`): nothing today structurally *ties*
`orchestration_planning`'s private-class status to the composed set, so that future slice must explicitly
move the task class into `PRIVATE_TASK_CLASSES` -- pure code-review discipline today, worth capturing as a
precondition in whichever plan composes those capabilities. One minor, non-security dead-code observation:
`agentCapabilityLines()` is exported but never called (the planner shows bare ids, not descriptions) --
fail-safe (can only make choices worse, never grant excess authority), not fixed in this slice.

### Pass B: scheduler, recovery, loops, budgets, effect-lock, races, restart, stale state, generic-route bypass

Traced `project_start`'s effect-key claim to confirm it is the same single `ActionService._claim_effect_keys`
choke point every other caller uses, verified the one-live-run-per-project partial unique index at the
database level (not just trusted from docs), and confirmed budgets are enforced server-side (a direct HTTP
call bypassing the TypeScript coordinator still hits the same checks). Confirmed the loop guard's one
accepted residual (a `FAILED` step can be retried as a fresh step with a fresh approval; finer-grained
"three non-progressing steps" detection remains deferred, as S2/S4 already documented) is acceptable to
ship given budgets bound the worst case and the closed-enum planner has no paraphrase-evasion surface.
Confirmed the generic orchestration routes reject every constructed bypass attempt (an out-of-catalog id,
a real-but-uncomposed id, a task-type mismatch, and a client-supplied `resolved_summary` trying to fake a
task-backed capability's outcome) at the correct layer.

**One confirmed bug, reproduced against a real Postgres database:** `OrchestrationService.resume()` never
checked orchestration liveness/expiry, unlike every other write path (`advance`, `record_planner_call`,
`finish`, `_pause`), which all funnel through `_live_running()`. This meant a `PAUSED` orchestration whose
30-minute TTL had elapsed while waiting on the user could still be revived to `RUNNING` by `resume()` --
after which the very next call (`advance`/`record_planner_call`/`finish`) would immediately refuse
`orchestration_expired`, leaving the orchestration stuck in an undocumented, dead `RUNNING` state reachable
only by Stop. Not an authority or effect-lock bug -- no capability executes wrongly, no budget widens -- but
a real robustness gap a user returning to a paused task after a long gap would hit as a confusing error.

**Fixed**: `resume()` now checks `repository.is_live(orchestration_id)` in two places -- once immediately
after confirming the orchestration is `PAUSED` with a resumable reason (fail-fast, before any capability
read, matching `_live_running`'s own shape), and again inside the final locked write, immediately before
`resume_running()`, since `resume()`'s capability reads are real I/O that could themselves cross the TTL
boundary between the first check and the write. Both raise `OrchestrationRefusal("orchestration_expired")`,
the same code every other expired-orchestration path already produces. New regression test:
`test_resume_refuses_once_the_orchestration_has_expired_rather_than_reviving_it`
(`services/agent/tests/test_orchestration_service.py`), added to the eval suite above.

No other finding in Pass B's scope: effect-lock/races/duplicate-effects, restart/in-memory-state safety,
`outcome_unknown` honesty, and generic-route closure all checked out clean under direct code tracing. One
non-reportable dead-code note: `OrchestrationRepository.fail()` has zero callers.

## Validation

* `uv run mypy app`: clean (179 source files).
* `uv run pytest -m "not browser and not desktop_uia"`: 2,535 passed; the only failure is the pre-existing,
  documented environment baseline (`test_booking_routes_without_a_worker_answer_503`), unrelated to this
  diff.
* `npm.cmd run typecheck`: clean.
* `npx vitest run`: 2,552 passed, 22 skipped; the only 3 failing test files are pre-existing, documented,
  machine-specific baselines (two missing a local vision-model asset file, one a CRLF/LF line-ending
  mismatch), unrelated to this diff.
* `npm.cmd run build`: main/preload/renderer all build clean.
* `node scripts/run-evals.mjs`: **139/139** eval cases pass.
* `pytest -m desktop_uia`, browser suites, `npm.cmd run package:dir`: not re-run; M11 touches no
  desktop-UIA, browser-worker, or packaging code.

## What M11 ships as, honestly

Three of sixteen catalog capabilities are composed (`public_research`, `project_status`, `project_start`);
thirteen remain real-but-uncomposed, refused honestly (`capability_unavailable`) rather than executed or
guessed. `manual_handoff_required` exists in the schema (migration `0022`) but is driven by no capability
yet. Stop stops future scheduling only, deliberately not cascading into a linked child task's own state
(documented in the S4 review as a narrower, more conservative reading of the plan's Stop line, consistent
with Milestone 10 S5's own established Stop rule). Two independent audits found the authority model itself
sound across all five slices; the one bug either pass found was a resume/expiry robustness gap, now fixed
with a regression test. See `docs/plans/milestone-11.md` for the full plan and the per-slice review docs
(`milestone-11-s1.md` through `-s4.md`) for what each slice actually built versus deferred.

**M11 is not merged to main.** Per the milestone's own instruction, main is left untouched; `lumi-m11`
carries the complete, audited milestone.
