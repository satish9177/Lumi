# Milestone 12 S1: trusted available-refs foundation

> **Visibility is not authority.** A planner may now be shown resource refs (`r1`, `r2`, ...) alongside the
> capability catalog it already had. Seeing one is never, by itself, permission to use it with any particular
> capability -- today, with zero capabilities yet composed to accept one, that boundary is total: citing any
> resource against `public_research`, `project_status` or `project_start` is refused outright, even a
> resource the citing orchestration genuinely, freshly owns.

```text
Baseline               774d71ad5cd79236d8be00f767a544eb86777cf8 (M11, = main)
Migration head          0023
```

S1 ships the resource-ref registry itself and composes no new capability. `public_research`, `project_status`
and `project_start` keep exactly their M11 behavior; the only externally visible change is that each now also
mints a controller-authored resource (`research_result_ref` / `project_status_ref`) alongside its existing
bounded result summary, and that none of the three accepts a resource as input.

## What this slice adds

* **Migration `0023`** (`orchestration_resources`): a durable, controller-owned registry. Every row is bound
  to exactly one orchestration and one closed `kind` (16 kinds declared, matching every M1-M10 input/output
  class the full M11 S1 capability catalog already names); a lookup is always scoped to
  `(orchestration_id, ref)`, so a ref from another orchestration cannot resolve here by construction.
  `producing_step_id` and a self-referential `parent_resource_id` record lineage (unused by S1's two kinds,
  reserved for S2's download -> place -> extract chain); `single_use`/`consumed_at` support future spend-once
  kinds; `expires_at` supports future independently-expiring kinds; `binding_digest` is reserved, unused.
* **`app/domain/orchestration_resources.py`**: the closed kind vocabulary, `CAPABILITY_OUTPUT_RESOURCE`
  (which composed capability mints which kind), and `CAPABILITY_RESOURCE_REQUIREMENTS` (which composed
  capability accepts which kinds as input -- today, for all three, the empty tuple). Pure validation only:
  `validate_resource_refs` (bounded, ref-shaped, deduplicated), `safe_label` (bounded, control-stripped
  template text), `next_ref` (deterministic, controller-only).
* **`app/repositories/orchestration_resources.py`**: `mint`, the one scoped `get`, `available` (excludes
  consumed/expired), `consume` (atomic compare-and-swap, mirroring every other single-use claim in this
  codebase).
* **`app/services/orchestration.py`**: `advance()` gains an optional `resources` parameter, validated and,
  for a non-empty capability requirement, resolved/kind-checked/consumed inside the same transaction and row
  lock `_live_running()` already takes before any step is written. `_commit_step()` and `resume()` both mint
  the producing capability's own output resource, from a template-only safe label
  (`f"{capability} result (step {sequence})"`) that never carries the capability's own result content.
* **`src/main/agent/orchestration-planner.ts`**: `ORCHESTRATION_PLANNER_SCHEMA` gains an optional `resources`
  array of plain strings; `parseOrchestrationDecision` resolves each against the orchestration's own
  freshly-computed `availableResources` set, refusing anything not currently offered (invented, stale,
  cross-orchestration, consumed, or expired -- all of which simply do not appear in that set).
* **`orchestration_planning` is now private.** Moved into `model-router.ts`'s `PRIVATE_TASK_CLASSES` ahead of
  any privacy-sensitive capability composition, per the M11 final review's explicit precondition.
  `OrchestrationPlanner` now pins its call to the first configured provider for the class and never fails
  over. A new structural test (`src/main/models/orchestration-planning-privacy.test.ts`) derives the
  requirement from the capability catalog's own `mayDiscloseToProvider` + new `resultPrivacyClass` field
  (`account_read`, `desktop_reason`, `document_compare`, `form_prepare`, `workflow_prepare` are `'private'`)
  rather than a hand-maintained list, and fails if that set is ever empty (so the test cannot pass vacuously).

## Design decisions and their reasoning

* **No capability accepts a resource yet.** `CAPABILITY_RESOURCE_REQUIREMENTS` maps all three composed
  capabilities to `()`. This was a deliberate scope decision: giving `project_status`/`project_start`
  multi-project selection is a real, separate feature nobody has asked for and the current
  `OrchestrationCoordinator.dispatch()` design (M11) already assumes a single registered project/recipe by
  resolving it entirely outside the planner's control. Composing a genuine consumer is S2's job (documents).
  The mechanism is instead proven end to end -- minting, ownership, freshness, kind-checking, consumption --
  through direct repository/service tests and through the live `advance()` path's `resources_not_supported`
  refusal, which is exercised with a real, fresh, orchestration-owned resource, not only a synthetic one.
* **Consumption timing.** A cited single-use resource is consumed when the step is *committed* (linked),
  not when it eventually succeeds. This matches the architecture: for a task-backed capability, the caller
  (Electron main) has already performed the real-world action needing that resource's authority (e.g.
  creating a child task through the target capability's own boundary) before calling `advance()`; `advance()`
  only records that linkage. See "Documented residuals" below for the one forward-looking consequence.
* **Safe labels are separate from result summaries.** The pre-existing `result_summary`/`resultSummary`
  field (already reviewed in M11, already bounded) is untouched and still carries the richer per-step
  narrative the planner and cockpit see. The new `safe_label` on a *resource* is a stricter, second,
  template-only field with zero per-instance variability beyond the capability id and step sequence --
  deliberately more conservative than what M11 already allowed, because a resource's label is meant to
  survive being shown across multiple future planner calls, not just narrated once.

## Adversarial review

One fresh, independent Claude review (general-purpose agent, no prior session context) traced all nine
invariants below against the actual code, not this document. **No High or Medium findings.**

1. No authority from visibility -- confirmed sound.
2. Cross-orchestration isolation -- confirmed sound (`get()` has no unscoped variant).
3. The model can never mint a ref -- confirmed sound (`mint()` has exactly two call sites, both fed only
   controller-computed/static values; `safe_label` is pure template text).
4. Freshness/consumption correctness under concurrency -- confirmed sound: resolution and consumption happen
   strictly after `_live_running()`'s row lock, in one transaction, with validation fully separated from
   consumption (nothing is partially consumed on a later validation failure).
5. Planner-side defense in depth -- confirmed sound (`availableResources` is recomputed fresh every loop
   iteration in `OrchestrationCoordinator.run()`, never cached).
6. `orchestration_planning` privacy migration -- confirmed sound (`isPrivate` breaks after one attempt
   structurally, independent of `permits`; `recipients()` and the `permits` closure derive from the same
   pure `primaryRecipient()` call).
7. Resource state-code HTTP mapping -- confirmed sound (409 vs. 422 matches the existing
   `task_kind_mismatch`/`orchestration_not_found` convention).
8. Migration/schema soundness -- confirmed sound (FK `ondelete="RESTRICT"` throughout, consistent with the
   rest of the schema; CHECK constraints pinned by tests against both the TS and migration spellings).
9. Code quality -- one informational nit (`AgentOrchestrationView.resources` is typed optional in TS even
   though the real wire parser requires it; the reviewer noted this is "actually the safer of the two
   behaviors" and left it as a documented, deliberate choice rather than a defect).

**Documented residual, carried into S2+:** the reviewer flagged (Low/Informational, unreachable today) that
consuming a single-use resource at citation time rather than at eventual step success means a future
capability composing a single-use kind (e.g. a `transfer_ref`) through an approval-gated, task-backed
capability that can later be declined/fail/expire would burn that resource even though nothing useful
happened. Not exploitable today (every composed capability's requirement is `()`, so nothing is ever
consumed), but the slice that first wires a single-use resource into a task-backed capability (S2's
`download_document` -> `place_downloaded_file` chain is the most likely candidate) must explicitly design for
this rather than inheriting S1's citation-time consumption unreviewed.

## Validation

* `uv run mypy app tests`: clean (311 source files).
* `uv run pytest -m "not browser and not desktop_uia"`: 2,554 passed, 386 deselected; the only failure is the
  pre-existing, documented environment baseline
  (`test_booking_routes_without_a_worker_answer_503`, `services/agent/.env`'s
  `LUMI_PUBLIC_INSPECTION_HOSTS`), unrelated to this diff.
* `uv run pytest tests/test_orchestration_service.py tests/test_orchestration_domain.py tests/test_orchestration_resources_domain.py`:
  63 passed (includes the new `TestResourceRegistry` class exercising every S1 attack listed in
  `docs/plans/milestone-12.md`'s S1 section against a real PostgreSQL database).
* `uv run pytest tests/test_migration_0013.py tests/test_migration_0015.py tests/test_migration_0017.py tests/test_migration_0018.py tests/test_migration_0019.py tests/test_migration_0020.py tests/test_schema.py`:
  17 passed (each updated to also exclude `orchestration_resources` when standing at a pre-`0023` revision).
* `npm.cmd run typecheck`: clean.
* `npx vitest run`: 2,570 passed, 22 skipped; the only three failing test files are the pre-existing,
  documented, machine-specific baselines (`real-inference`, `tokenizer-pack`, `accessibility` scam CSS),
  unrelated to this diff.
* `npm.cmd run build`: main/preload/renderer all build clean.
* `pytest -m desktop_uia` and `npm.cmd run package:dir` are deferred to the M12 final validation pass
  (S1 touches no desktop/UIA or packaging-relevant code), matching M9/M10/M11's own per-slice practice.

## What S1 deliberately does not do

No capability is newly composed. No document, account, desktop or project resource is ever produced (only
`research_result_ref` and `project_status_ref`, from the three capabilities already composed in M11). No
renderer/cockpit UI changes (that is S4's job). No change to any M1-M10 approval, grant, disclosure or effect
lock. Production certificate remains **NOT CONFIGURED**; real-account release remains **BLOCKED**.
