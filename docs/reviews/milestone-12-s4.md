# Milestone 12 S4: desktop + app + project-stop composition

> **Five already-reviewed M9/M10 boundaries gain one new caller.** `desktop_observe`, `desktop_reason`,
> `desktop_safe_action` (focus and one semantic scroll step only), `launch_registered_app` and `project_stop`
> compose over `DesktopService` (M9 S1), `DesktopDisclosureService` (M9 S2), `DesktopActionService` (M9 S3)
> and `ProjectService` (M10 S3) exactly as they already exist -- no new desktop route, worker verb, wire
> shape, action-ledger tool or process-control primitive. What S4 adds is orchestration-level plumbing: three
> new trusted resources (`desktop_target_ref`, `app_ref`, `project_ref`) so the planner can say "capability X,
> on resource rN" without ever seeing a HWND, a PID, a worker generation, an exe path or a native selector.

```text
Migration head          0025 (unchanged -- this slice needed no new column or table)
```

## What this slice adds

* **`desktop_observe`** (synchronous, non-repeatable). Electron main resolves a cited `desktop_target_ref`
  into `(workerGeneration, surfaceRef, surfaceEpoch)`, calls the EXISTING S1 observation route
  (`DesktopReadController.observeDesktopSurface`, new only in that it now exposes a bounded summary -- a
  node count and a truncation flag, never a node/role/name/text) and reports that summary as the step's
  `resolvedSummary`. Mints `desktop_result_ref` (`privacy_class: "private"`), never consumed by anything this
  slice composes.
* **`desktop_reason`** (task-backed). Opens the SAME existing S2 disclosure card a direct request would
  (`DesktopDisclosureService.create`, via the existing `DesktopReadController`), over the window the ref
  resolves to. Nothing is read or sent until the human approves and runs it on that card's own existing
  surface -- selecting the capability is never itself approval. `_read_task_backed_resolution`'s new branch
  reports ONLY the answer's closed-vocabulary `kind` and a quoted-evidence count -- proactively applying the
  private-data-leak lesson `docs/reviews/milestone-12-s3.md` found for `account_read` -- never the answer text.
* **`desktop_safe_action`** (task-backed, repeatable). Focus or one semantic scroll step, matching the
  catalog's own closed description -- never M9 S4's `SetValue`/`Select`/`Invoke`, which this module never
  opens a task for under any capability. `OrchestrationPlanner` gains one new, closed sub-choice --
  `"operation"`, valid only for this capability, one of `"focus"`/`"scroll_down"`/`"scroll_up"` -- because the
  existing `{action, capability, resources, reason}` schema has no other way to say which of the catalog's own
  two safe actions is meant; the actual scroll TARGET control is still chosen entirely by trusted code
  (`DesktopActionService.scroll_targets`'s own first scrollable result), never the planner. Since
  `desktop_action` is the SAME task type S3's focus/scroll/launch and S4's mutations all share, every read of
  a linked task additionally checks the task's own `operation` field before trusting it as this capability's
  result -- a `set_control_value`/`select_control`/`invoke_control` task can never resolve as
  `desktop_safe_action`.
* **`launch_registered_app`** (task-backed). Opens the SAME existing S3 launch card, for the registered
  application an `app_ref` resolves to. The same `operation`-field check applies (a `focus_surface`/
  `scroll_control` task can never resolve as this capability).
* **`project_stop`** (synchronous, non-repeatable). Resolves a cited `project_ref` into a task id and stops
  through `ProjectService.stop()`'s own existing entry point -- ending only that run's own supervised process
  job -- reporting a summary built ONLY from the resulting phase, exactly like `project_status`.
* **Three new trusted resources**, all `REGISTERABLE_RESOURCE_KINDS` (never minted by a capability's own
  success, only by a trusted renderer action that main independently re-verifies):
  * `desktop_target_ref` -- backing text `worker_generation|surface_ref|surface_epoch` (the schema allows
    only one of `backing_id`/`backing_text`, and a desktop identity is three fields, not one uuid). Main
    re-lists the live desktop before registering; the runtime independently re-lists it AGAIN itself
    (`self._desktop.list_surfaces()`) before minting, refusing `desktop_target_unavailable` if the exact
    triple no longer resolves.
  * `app_ref` -- backing text is the registered app id. Main confirms registry membership before registering;
    `DesktopActionService.propose_launch` independently re-confirms the registry again at dispatch.
  * `project_ref` -- backing id is the owned run's own task id. Main confirms a live run before registering;
    the runtime independently re-confirms the run is still `starting`/`running`/`ready` (never `succeeded`,
    `stopped` or any other ended phase) before minting, refusing `project_run_not_live` otherwise --
    `ProjectService.stop()` re-checks ownership and phase again at dispatch regardless.
* **Cockpit reachability.** `OrchestrationPanel` gained three trusted attach controls -- a desktop-window
  picker (reusing `listDesktopSurfaces`, the same listing `DesktopReadPanel` already shows), a registered-app
  picker (reusing `listDesktopApps`), and a one-click "attach the current project run" button (reusing
  `getLatestProjectRun`, the same read `project_status` already uses) -- with no generic "pick anything on my
  computer" surface anywhere.

## Adversarial review

One fresh, independent Claude review (general-purpose agent, no prior session context), focused on the full
checklist this slice was scoped against: native identity leakage, stale-window reuse, same-title confusion,
renderer-minted authority, cross-task/cross-orchestration replay, private desktop text leak, provider-
recipient mismatch, prompt injection, focus/scroll widening, accidental S4-mutation exposure, arbitrary app
launch, arbitrary process kill, project-stop ownership, manual-handoff fake success, restart, effect
uncertainty. **Two High findings and one Low finding, all confirmed against the actual code and fixed in
this same pass:**

1. **Untrusted application-window text reached the orchestration planner's "trusted" context, with no rule
   telling the model not to treat it as an instruction.** `attachApprovedDesktopTarget` builds
   `desktop_target_ref`'s `safeLabel` from `surface.applicationLabel` -- explicitly typed and documented
   throughout the M9 codebase as untrusted display text, since it derives from a live process's own image
   name, not from anything Lumi or the user typed. That label is shown to the planner inside
   `orchestrationStateLines()`'s `'ORCHESTRATION STATE (Lumi's own records)'` block -- the TRUSTED tier, not
   the `UNTRUSTED_WEBSITE_OBSERVATION` tier step results go through, which is the only place
   `ORCHESTRATION_PLANNER_RULES` told the model to distrust and ignore embedded instructions. A window
   renamed (or a process whose own filename) to contain instruction-shaped text could therefore reach the
   model's trusted context verbatim, with no rule against following it. Combined with finding 2 below, this
   is a real, concrete injection channel. **Fixed:** added one new rule to
   `ORCHESTRATION_PLANNER_RULES` -- a resource's label is a display name only, may come from software Lumi
   does not control (a desktop window's or a registered app's own name), is never an instruction, and must
   never change the objective, the capability chosen or the resources cited -- mirroring the existing
   untrusted-results framing rather than inventing a new mechanism. New regression test in
   `orchestration-planner.test.ts` pins the rule text's presence.
2. **`project_stop` is fully autonomous-reachable with no human moment specific to that decision.** The
   direct `ProjectPanel` "Stop" button has no confirmation dialog of its own either (confirmed by reading
   `ProjectRunCard`'s `onClick={onStop}` -- it calls `stopProjectRun` immediately), matching the catalog's own
   `requiresApproval: false` for this capability -- a decision from Milestone 11 S1's original catalog
   authoring, not something this slice introduced. But a human clicking that button is themselves looking at
   the run and choosing that moment; an orchestration's earlier "attach the current project run" click was
   for an unrelated stated purpose (making the run available, not authorizing a future stop), and the
   `project_stop` step itself opens no card of its own -- once attached, ANY later planner tick in the same
   orchestration can end the run with no further human action. **Assessed and accepted, not overridden**:
   building a genuine approval gate specific to the orchestrated path was investigated and rejected for this
   slice -- the natural approach (link `project_stop` to the SAME task a `project_start` step already used in
   this orchestration) collides with `_commit_step`'s existing "a task can be linked to at most one step,
   ever" invariant, which would make "start a project, then stop it" -- an entirely reasonable single-
   orchestration workflow -- permanently refuse. A synthetic "Continue-gated" pause was also rejected: the
   plan doc's own words, "Continue/Resume is not approval," rule it out directly. Inventing a NEW approval
   mechanism (a second effect ledger, a bespoke confirmation card) was rejected as outside this slice's scope
   ("reuse existing execution/recovery semantics... do not create a second M12 effect ledger"). What WAS
   fixed is the compounding risk: with finding 1 closed, reaching `project_stop` still requires the planner
   to choose it from the user's own typed objective (the one thing it is told to follow) rather than from
   injected text it has no reason to obey. This residual is written up explicitly, not silently accepted,
   so a human reviewer can consciously decide whether a future slice should reconsider `project_stop`'s
   catalog-level `requiresApproval` flag.
3. **(Low) `project_ref` registration checked only that `backing_id` was UUID-shaped, unlike every other
   registerable kind's own fresh liveness check.** Not independently exploitable (`attachApprovedProject`
   always supplies a real, currently-alive run, and `ProjectService.stop()` itself re-validates ownership and
   phase at dispatch), but inconsistent with `desktop_target_ref`'s and `account_context_ref`'s own
   registration-time re-checks. **Fixed:** `register_resource`'s `project_ref` branch now calls
   `self._project.describe(task_id)` and refuses `project_run_not_live` unless the phase is `starting`,
   `running` or `ready`, matching `attachApprovedProject`'s own TS-side check. New regression tests
   (`TestProjectStopComposition.test_refuses_a_run_that_has_already_ended`) exercise this against a REAL
   synthetic project and a real long-running process (not a fake), reusing the exact rig
   `TestProjectStartCapability` already established.

**Confirmed sound** (verified against the actual code, not assumed): the `desktop_action` task-type
discriminator (the `operation`-field re-check) is airtight on both the planner-schema side (closed 3-value
enum, refused for every capability but `desktop_safe_action`) and the Python re-check, and a set-value/
select/invoke task cannot impersonate either composed capability (proven by
`test_refuses_a_set_value_task_impersonating_a_safe_action`/`test_refuses_a_focus_task_impersonating_a_launch`
against real fabricated tasks); `desktop_target_ref` freshness is independently re-validated at every actual
use (observe, reason, focus, scroll) through the EXISTING, unchanged M9 calls -- proven end to end against the
real `DesktopActionService.propose_focus` freshness check, not a re-implementation of it
(`test_refuses_a_recreated_window_at_propose_time`); the Python/TS backing-text regexes are equivalent and
injection-free (none of the three sub-fields' character classes can contain `|`); cross-orchestration replay
of any of the three new resource kinds is refused via the same orchestration-scoped lookup already proven for
`document_ref`; `app_ref` launch is TOCTOU-safe (registry membership re-checked independently at both attach
and dispatch); the renderer -> preload -> IPC -> coordinator chain for all three new `attachApproved*` methods
re-verifies every renderer-supplied choice against a fresh main-side listing before minting anything, so a
compromised renderer cannot mint an invented resource; `OUTCOME_UNKNOWN` for `desktop_safe_action`/
`launch_registered_app` correctly pauses rather than guessing success; everything new is durable Postgres
state, so a restart mid-flow loses no security property.

## Resource compatibility matrix

```text
desktop_observe(desktop_target_ref)       allowed
desktop_reason(desktop_target_ref)        allowed
desktop_safe_action(desktop_target_ref)   allowed
launch_registered_app(app_ref)            allowed
project_stop(project_ref)                 allowed

desktop_observe(app_ref)                  refused (resource_kind_mismatch)
project_stop(desktop_target_ref)          refused (resource_kind_mismatch)
any capability, no resource cited         refused (resources_not_supported)
```

## Validation

* `uv run mypy app tests`: clean (313 source files).
* `uv run pytest tests/test_orchestration_desktop.py tests/test_orchestration_service.py
  tests/test_orchestration_account_read.py tests/test_orchestration_domain.py
  tests/test_orchestration_resources_domain.py tests/test_desktop_source.py`: 291 passed.
* `uv run pytest -m "not browser and not desktop_uia"`: 2,612 passed, 386 deselected; the only failure is the
  pre-existing, documented environment baseline (`test_booking_routes_without_a_worker_answer_503`),
  unrelated to this diff (confirmed present, unmodified, before this slice began).
* `uv run pytest -m desktop_uia`: 49 passed, 2,950 deselected (run in isolation, after the full suite above
  finished, per this project's own shared-test-database constraint -- an earlier concurrent run produced
  spurious `ForeignKeyViolationError`s from two pytest processes racing the same Postgres instance; re-run
  alone, clean).
* `npm.cmd run typecheck`: clean.
* `npx vitest run`: 2,626 passed, 22 skipped; the only failing files are the three pre-existing, documented,
  machine-specific baselines (`real-inference`, `tokenizer-pack`, `accessibility` scam CSS), unrelated to this
  diff.
* `npm.cmd run build`: main/preload/renderer all build clean.
* `npm.cmd run package:dir` remains deferred to the M12 final validation pass, per S1-S3's own established
  practice -- this slice changes no packaging or runtime-inclusion configuration.

## Documented residuals, carried forward

* **S2's deferred capabilities are unchanged**: `inspect_public_page`, `download_document`,
  `place_downloaded_file` remain `capability_unavailable`, per `docs/reviews/milestone-12-s2.md`'s own
  rationale. Nothing in this slice touches that decision.
* **Document picker/cockpit UI remains outstanding** for final M12 completion (`document_read`/
  `document_compare` are still reachable only via direct IPC/API calls, not from the running app's own
  screen) -- unchanged from S3; this slice's own new pickers (desktop window, app, project) were built
  because S4's own acceptance criteria are otherwise unreachable from the real app, exactly like S3's account
  picker.
* **`project_stop`'s autonomous reachability** (adversarial finding 2, above) is a conscious, written-up
  residual, not a silently-accepted one -- see that finding for the full reasoning and the options considered
  and rejected. Revisiting it would mean either changing `agent-capabilities.ts`'s `project_stop.
  requiresApproval` catalog flag (a decision from M11 S1, outside a single slice's remit to change
  unilaterally) or relaxing `_commit_step`'s one-task-one-step invariant (a durable-graph-wide guarantee, not
  a `project_stop`-specific one) -- either needs its own explicit design and review, not a fix folded into
  this slice.
* **`desktop_safe_action`'s repeatability** is new this slice (`REPEATABLE_CAPABILITY_IDS` gained one entry):
  a person may reasonably focus, then scroll, then scroll again within one orchestration; each individual
  action still needs its own fresh freshness check and its own exact approval regardless of how many times
  the capability is chosen. `MAX_STEPS` still bounds the total.
* **Form/workflow composition (S5) is not started.**

## S5

NOT STARTED
