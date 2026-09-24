# Milestone 11: general task orchestration

> **General planning is not general authority.** A planner may decide "the next useful capability is
> `document_read`." It may never decide that therefore a path may be read, a page may be opened, or an
> effect may execute. Every M1-M10 approval, grant, effect lock and disclosure boundary stays authoritative.
> M11 adds a coordinator over capabilities that already exist. It is not a new privileged executor.

Status: **not started**. This plan covers all five slices; each ships as its own implementation, adversarial
review, validation, documentation and commit, in order. If a hard requirement cannot be met, M11 stops at the
last completed slice.

```text
M11 - general task orchestration

S1  general request routing + closed capability catalog     vocabulary only, no execution
S2  durable read-only orchestration                          compose read-only capabilities, one step at a time
S3  effectful capability composition                          request (never auto-approve) existing effects
S4  unified task cockpit + pause/resume/manual handoff        one coherent view, durable resume
S5  generality evals + final cross-slice audit                 prove breadth, not just jobs
```

This plan refines the M11 entry implied by `docs/plans/general-computer-use-architecture.md` (a single
planner, deterministic authority, no new orchestration platform) against the concrete M1-M10 implementation.
It does not redesign M1-M10; it composes what M1-M10 already built.

## Why M11 is safe to build at all

Every M7-M10 planner already follows the same shape: **one step at a time**, a closed JSON schema with no
field for a raw value/path/URL/selector/coordinate, opaque refs that only resolve inside their own task and
generation, and a hard separation between *trusted facts* (what Lumi's own records say) and *untrusted
content* (what a page/document/desktop control says). `research-planner.ts`, `desktop-planner.ts` and
`form-planner.ts` are three independent, already-reviewed implementations of exactly this pattern. M11's
orchestration planner is a fourth: it chooses one *capability id* from a closed, app-authored catalog, never
a URL, a path, a command or an approval. The catalog is data the controller owns; the model can select from
it and can never add to it.

The M10 final review already proved the two invariants M11 depends on:

* every grant/approval lookup is filtered by an exact `kind` clause, so no authority is interchangeable with
  another (`docs/reviews/milestone-10-final.md` §2, §3 Pass A);
* the cross-executor effect lock (`action_effect_keys`, `app/domain/effects.py`) already blocks a *new* keyed
  effect while an old one is unresolved, from any task, any executor, any route. M11 adds no second lock and
  no bypass: an orchestration step that proposes an effect tool goes through the exact same
  `ActionService._claim_effect_keys` choke point as a direct call does.

M11's only genuinely new surface is: (1) a new closed intent class and capability catalog (S1); (2) a new
durable graph table that records *which capability was chosen and what its result handle is*, never raw
authority (S2); (3) a new planner task class that requests, never grants, an existing effect (S3); (4) a
cockpit view and resume path over that graph (S4). None of these can create an approval, a grant, a
disclosure or an effect key on their own.

## S1 - general request routing + closed capability catalog

**Adds (TypeScript only; no Python/runtime change, no migration):**

* `orchestrated_task` as a new closed intent in `task-request-interpreter.ts`'s `INTERPRETATION_SCHEMA`,
  alongside `appointment_plan`, `clinic_info`, `public_research`, `status`, `check_booking`, `cancel_task`,
  `remember_preference`, `conversation`. Its payload is exactly one bounded string, `objective` (same shape
  as `research`'s `RESEARCH_SCHEMA_PROPERTIES.objective`: no URL, selector or script field). Interpretation
  is classification only: `interpretAgainst` returns `{ kind: 'orchestrated_task', objective }` and performs
  no side effect, exactly like the existing `research` branch before `deps.research` is called.
* A new `Interpretation` variant `{ kind: 'orchestrated_task'; objective: string }`, handled in `process()`
  the same way `research` is handled: if no orchestration capability is wired (`deps.orchestration` is
  undefined, which is what S1 ships with), it answers a stable `orchestration_unavailable` result and
  performs no side effect, the same shape `RESEARCH_UNAVAILABLE` already uses. S2 is what wires
  `deps.orchestration` to something real.
* `src/shared/agent-capabilities.ts` (new): the closed, controller-authored capability catalog. Each entry
  is `{ id, description, inputClasses, outputClasses, requiresApproval, mayDiscloseToProvider, hasSideEffects,
  blockedByEffectLock }`. The model never adds an entry; it can only be shown the ids and descriptions the
  catalog already has, exactly the way `RESEARCH_PLANNER_RULES` shows `RESEARCH_OPERATIONS` and nothing else.

**Catalog (S1 ships the table; nothing in it is wired to execute anything until S2/S3):**

| id | maps to (already reviewed) | side effects | approval | disclosure |
| --- | --- | --- | --- | --- |
| `public_research` | M7b research task | none (browsing is read-only by policy) | scope grant, once | possible (page text to planner/answer models) |
| `inspect_public_page` | M7a page inspection | none | exact, per page | possible |
| `account_read` | M8a authenticated_read task | none | account-read grant | possible |
| `document_read` | M10 S1 document task (extract/local compare) | none | file root READ | none by itself |
| `document_compare` | M10 S1 local + optional provider compare | none | `document_disclose` grant for the provider path | possible |
| `download_document` | M10 S2 `transfer_download` | yes (quarantine write) | `file_transfer` grant + step auth | none |
| `place_downloaded_file` | M10 S2 `transfer_place` | yes (file appears in an approved folder) | same `file_transfer` grant, second step auth | none |
| `desktop_observe` | M9 S1 UIA observation | none | existing observation scope | none |
| `desktop_reason` | M9 S2 `desktop_read` disclosure | none | `desktop_disclose` grant | possible |
| `desktop_safe_action` | M9 S3 `DESKTOP_FOCUS` / `DESKTOP_SCROLL` | yes (focus/scroll; R1, no data mutation) | exact, per action | none |
| `launch_registered_app` | M9 S3 `DESKTOP_LAUNCH` | yes (process start) | exact, per launch | none |
| `project_status` | M10 S3 project run status | none | none beyond existing project registration | none |
| `project_start` | M10 S3 `project_start` | yes (supervised process tree) | exact, per run, native warning | none |
| `project_stop` | M10 S3 stop | yes (ends the run's job) | none beyond owning the run | none |
| `form_prepare` | M8b S5 / M10 S4 form preparation | none (stops before submit) | account-read + planning + manifest grants | possible |
| `workflow_prepare` | M10 S4 whole cross-app workflow, used as ONE composed step | yes (the sum of its own child steps' effects) | every child step keeps its own approval | possible |

Deliberately **not** in the catalog, matching M10's own hard no-go list plus M9's mutation boundary: shell,
terminal, arbitrary executable/argument, arbitrary filesystem write/delete, generic upload/submit/send/
purchase, Git mutation, dependency install, raw mouse/keyboard/coordinate click, and the M9 S4 bounded
mutations (`DESKTOP_SET_VALUE`/`DESKTOP_SELECT`/`DESKTOP_INVOKE`). The last exclusion is deliberate and
narrower than the M9 boundary itself: M9 S4 already reviewed those three primitives as *individually*
sound, but composing them through a *second*, more general planner multiplies the surface a model can
misuse one of them from (a general objective is a much larger space than "the plan the user just approved
for this one window"). M11 does not re-open that boundary; a future milestone can, with its own review, once
S1-S5 exist to evaluate the actual composition risk against.

**S1 tests** (`task-request-interpreter.test.ts`, `agent-capabilities.test.ts`, new):

* existing `appointment_plan`, `clinic_info`, `public_research`, `status`, `check_booking`, `cancel_task`,
  `remember_preference`, `conversation` requests parse identically to before (regression, not new coverage);
* a general document/project/mixed request parses to `orchestrated_task` with a bounded objective;
* a reply naming an intent outside the closed enum is refused (existing `parseInterpretation` behavior,
  re-asserted for the new branch);
* a reply that adds a field the schema does not declare (a URL, a path, a command, a capability id) is
  refused whole, not stripped;
* the capability catalog: every entry's `id` is unique, every boolean field is present (no capability may be
  "probably fine" by omission), and a test walks the M10 no-go list and asserts none of those strings is a
  catalog id;
* malformed provider output and provider unavailability fall back exactly like every other intent (the
  existing deterministic-rules path), with `orchestrated_task` reachable from the rules fallback too so an
  unpackaged/no-key build still classifies general requests instead of silently downgrading to conversation.

## S2 - durable read-only orchestration

**Adds:**

* Migration `0021` (current head `0020`): `orchestrations` (id, objective, status, revision, current step
  index, pause reason, budgets consumed, created/updated/expiry) and `orchestration_steps` (id,
  orchestration_id, sequence, capability id, status, result handle, child task id/kind if any, created_at).
  No raw path, URL, selector, coordinate or credential column exists on either table, mirroring `workflows`
  (M10 S4)'s own shape.
* `app/domain/orchestration.py`, `app/services/orchestration.py`, `app/repositories/orchestration.py`,
  `app/api/orchestration_routes.py` (`/orchestrations*`), following the `workflows.py` /
  `WorkflowService` / `workflow_routes.py` shape exactly: **a deterministic controller, not a planner.** It
  holds no tool of its own; it creates child tasks in the capability the plan named and hands each one to the
  service that already owns that authority.
* `src/main/agent/orchestration-planner.ts`: the one-step-at-a-time planner, structurally identical to
  `research-planner.ts` — closed schema, no free field, refs only, a redacted-summary untrusted section — but
  choosing one *capability id* from the S1 catalog instead of a browse operation.
* `orchestration_planning` task class in `model-contracts.ts` / `model-router.ts`'s `DEFAULT_ROUTES`, private
  or not per the data it is shown (see "Model routing" below); a fresh task class, not reused from any
  existing one, so its own budget and disclosure policy are independently tunable.
* Result handles: `research_result:r1`, `document_result:d1`, `account_result:a1`, `desktop_result:u1`,
  `project_status:p1`. Each is a controller-authored, bounded summary (never the underlying private content)
  stored on the `orchestration_steps` row; the planner's next call is shown handles and summaries, never the
  child task's own private evidence.

**S2 ships read-only capabilities only:** `public_research`, `account_read`, `document_read`,
`document_compare`, `desktop_observe`, `desktop_reason`, `project_status`. Choosing one of these creates (or
reuses) the underlying child task exactly the way a user would through its own direct UI, including that
child task's own approval/grant/disclosure requirements — the orchestrator does not skip them. Every existing
approval a capability needs is still shown to the user, still trusted-UI, still per M1-M10's own rules.

**Budgets** (persisted per orchestration, conservative initial values in the spirit of M7b's research budget):
20 steps, 10 child tasks, 20 planner calls, 30 minutes wall time, 5 pause/resume cycles. Two identical
consecutive step choices at the same material state, or three non-progressing steps, pause the orchestration
(`PAUSED`, reason `loop_detected` or `no_progress`) rather than looping; exhausting a budget is `PAUSED`, not
a silently widened limit.

**S2 acceptance (read-only; deliberately spans multiple domains, not jobs):**

* A: "Research the Lumi GitHub repository and summarize it." → `public_research` → grounded answer.
* B: "Compare these two approved documents and explain the differences." → `document_read` ×2 →
  `document_compare` → answer.
* C: "Look at this open desktop application and tell me what state it is in." → `desktop_observe` → optional
  `desktop_reason` → answer.
* D: "Check whether my registered Lumi project is currently running." → `project_status` → answer.

No mutation occurs in any of A-D.

## S3 - effectful capability composition

**Adds no new effect primitive.** The orchestrator may *select* `download_document`,
`place_downloaded_file`, `project_start`, `project_stop`, `launch_registered_app`, `desktop_safe_action`,
`form_prepare` or `workflow_prepare`; selecting one only opens that capability's own existing card. It never
auto-approves, never aggregates several selections into one approval, and never retries an uncertain effect
through a different capability or a different task.

* The orchestration controller's step-execution path for an effect capability calls exactly the same service
  method a direct UI action would (`TransferService.create`, `ProjectService.start`, `DesktopActionService`'s
  registered-launch path, `WorkflowService` for `workflow_prepare`), so it inherits that capability's own
  approval requirement, effect-key derivation and effect-lock check for free. The orchestrator adds no
  parallel authorization path.
* `EFFECT_TOOLS` (`app/domain/effects.py`) is unchanged. An orchestration step that proposes an effect tool
  is claimed through the same `ActionService._claim_effect_keys` every other caller uses; `effect_locked`
  pauses the orchestration (reason `effect_locked`) rather than trying a different capability.
* The orchestrator understands `OUTCOME_UNKNOWN` / `RECONCILING` / `BLOCKED` as controller-reported step
  states that mean *wait, reconcile, or hand off* — never as a signal to pick a different capability to
  "make progress." This is enforced structurally: an unresolved effectful step is the only step the planner
  is shown as pending: no capability list is offered while it is outstanding.

**S3 acceptance:**

* E: "Open VS Code and start Lumi, then tell me when it is healthy." → `launch_registered_app` (its own
  approval) → `project_start` (its own separate approval) → `project_status` → answer. A repeat request finds
  the same live run; no duplicate server (reusing M10 S3's own guarantee).
* F: "Download this approved PDF, save it in my approved Documents folder, then summarize it." →
  `download_document` approval → quarantine → `place_downloaded_file` approval → `document_read` → answer.
  No arbitrary file write (reusing M10 S2's own path-safety and no-overwrite guarantees).
* G: "Use this approved document to help prepare this form." → `document_read` → `workflow_prepare`
  (composing M10 S4's own adoption/manifest approvals) → **stop before submit**. Submission count: 0.

## S4 - unified task cockpit + pause/resume/manual handoff

* A single orchestration panel listing each step's status, a controller-authored human-readable description,
  whether approval is required, a provenance label and a result summary — never a raw secret/private value
  beyond what that step's own trusted approval UI already shows.
* `manual_handoff_required` becomes a first-class pause reason (CAPTCHA, login, unsupported control, an
  ambiguous resource the controller cannot resolve on its own). After the user presses Continue, the
  orchestrator re-observes; it never assumes the handed-off action happened.
* Resume reloads the durable `orchestrations`/`orchestration_steps` rows, never chat history, and
  revalidates every referenced child task/grant/handle for freshness before continuing — the same
  freshness discipline M9 S3's 60-second scroll-proposal window and M10's disclosure re-checks already use.
* Stop: stops future scheduling, calls each in-flight child task/step's own existing stop/cancel path (M10
  S3's job-scoped Stop, M10 S4's `WorkflowService.stop`), preserves evidence, and never fabricates a
  rollback or compensating effect (same rule M10 S5 already established for `Stop`).

## S5 - generality evals + final cross-slice audit

At least 12-20 deterministic tasks spanning research, documents, accounts, desktop, projects and one or two
job-flavored tasks (not the majority), plus recovery/effect-lock/resume scenarios per the acceptance
catalogue in the M11 brief. Metrics: task completion, correct capability selection, malformed-planner-output
rate, planner call count, approval count, manual handoff count, stale-step rejections, effect-lock blocks,
recovery success, wrong-capability attempts, provider disclosure count. Two independent fresh-Claude passes
over the whole M11 diff (Pass A: authority/privacy/provider routing/lineage/prompt injection/resume;
Pass B: scheduler/recovery/loops/budgets/effect-lock/races/restart/stale state/duplicate effects/generic-route
bypass), each finding independently re-verified against the code before any fix, matching the M9/M10 audit
process exactly.

## Model routing

New task class `orchestration_planning`, kept separate from `intent_extraction`/`research_planning`/
`authenticated_planning`/`desktop_action_planning`/`document_compare`. Planning is read-only by
construction (it chooses a capability id, never touches private content itself), so provider fallback may be
permitted by the router's ordinary policy *unless* a given call's context contains private material — in
which case the caller passes the same one-recipient/zero-failover `permits` rule the five existing private
classes already use. In practice: an orchestration step over `public_research`/`desktop_observe`/
`project_status` results carries only controller-authored summaries (never account-private or desktop-private
text), so those planner calls stay outside `PRIVATE_TASK_CLASSES`; a step whose only available handle is an
`account_read` or `desktop_reason` result is shown that handle's redacted summary only, the same projection
its own private planner already produces, never the underlying private answer. No model decides whether an
approval exists, whether an effect may execute, risk classification, disclosure permission, path validity,
native identity, `OUTCOME_UNKNOWN` retryability, absence authority, effect-key derivation, grant validity or
task ownership — all of those stay deterministic, exactly as every prior milestone requires.

## Generic route safety

M10 S5 closed the generic ledger routes to registered effect tools (`effect_route_refused`). M11 adds no
competing generic route: the orchestration API accepts only `{orchestration_id, revision,
controller-issued step ref}`, never an arbitrary tool name or JSON payload. Advancing an orchestration always
resolves to one of the S1 catalog's closed capability ids, checked against the catalog server-side; a
provider-invented capability id is rejected before any child task is touched.

## Compatibility

Every direct M1-M10 flow keeps working unchanged: public research, document compare, project recipes,
desktop actions and form preparation remain directly reachable without going through an orchestration.
`docs/reviews/milestone-10-final.md`'s contract/fingerprint baselines are unaffected because M11 touches no
M1-M10 request/response shape; it only adds new tables, a new route family and a new planner.

## Validation per slice

Targeted tests per slice; at the end of M11: `uv run mypy`, `uv run pytest -m "not browser"`,
`pytest -m desktop_uia`, the relevant browser suites, `npm.cmd run typecheck`, `npx vitest run`,
`npm.cmd run build`, `npm.cmd run eval` and `npm.cmd run package:dir`. Production signing remains
**NOT CONFIGURED** and real-account release **BLOCKED**; M11 changes neither.

## Hard no-go list (unchanged from M10, restated because M11 composes across it)

Arbitrary shell/PowerShell/cmd, terminal typing, model-authored commands/executables/arguments, dependency
installation, Git mutation, arbitrary filesystem writes/root escape/overwrite/recursive delete, executing or
auto-opening downloaded/macro content, private document leakage, cross-task file/provider/browser authority,
duplicate project runs or external effects after uncertainty, effect-lock bypass through another executor or
another capability, generic upload/submit/send/purchase, raw mouse/keyboard/coordinate control, and — new to
this list, specific to M11 — capability aggregation: no sequence of catalog selections may combine into an
authority the catalog does not grant any single entry.
