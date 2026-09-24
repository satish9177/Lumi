# Milestone 11 S1: general request routing + closed capability catalog

> S1 establishes vocabulary only. No capability is executed because an intent model mentioned it, and
> nothing here is new authority: every M1-M10 approval, grant, disclosure boundary and effect lock is
> untouched.

Status: **COMPLETE**. This slice is TypeScript-only: no Python/runtime change, no migration (current head
stays `0020`).

## What was built

* `orchestrated_task`, a new closed intent in `task-request-interpreter.ts`'s `INTERPRETATION_SCHEMA`,
  alongside the existing `appointment_plan` / `clinic_info` / `public_research` / `status` /
  `check_booking` / `cancel_task` / `remember_preference` / `conversation`. Its payload is exactly one
  bounded string (`orchestration.objective`, 1-500 chars, no control characters), parsed by
  `orchestrationObjectiveFromWire` in `plan-wire.ts` -- structurally identical to `researchObjectiveFromWire`,
  the closest existing sibling.
* `parseInterpretation` re-validates the closed key set for `orchestrated_task` at two independent layers,
  matching every other intent: the top-level `allowed` map (`['intent', 'orchestration']`) and
  `orchestrationObjectiveFromWire`'s own `onlyKeys(args, ['objective'])`. A model reply cannot add a
  `capability`, `url`, `path`, `command`, `selector` or `approve` field and have it survive parsing --
  see the "adversarial review" section below for how this was verified.
* `looksLikeOrchestration` in `rule-interpreter.ts`: a narrow, distinctive-noun-only cue set (document/pdf/
  docx/resume-file, compare+file-word, download+file-word, project/VS Code + start/running/status/stop,
  "this open application/window", prepare/fill + form) that classifies general document/project/desktop/
  mixed requests as `orchestrated_task` from the deterministic English-rules fallback, reachable even with
  no model configured. Checked before `check_booking`/`status` so "check whether my project is running"
  is not mistaken for a booking check.
* `src/shared/agent-capabilities.ts`: the closed, controller-authored capability catalog -- 16 entries
  (`public_research`, `inspect_public_page`, `account_read`, `document_read`, `document_compare`,
  `download_document`, `place_downloaded_file`, `desktop_observe`, `desktop_reason`, `desktop_safe_action`,
  `launch_registered_app`, `project_status`, `project_start`, `project_stop`, `form_prepare`,
  `workflow_prepare`), each describing description/input-output classes/`requiresApproval`/
  `mayDiscloseToProvider`/`hasSideEffects`/`blockedByEffectLock`. It is **inert**: nothing outside its own
  test file references it yet. S2 is what wires a planner to choose from it.
* `process()` in `task-request-interpreter.ts` claims `orchestrated_task` unconditionally and answers a
  fixed `ORCHESTRATION_UNAVAILABLE` result (`orchestration_unavailable`, a new closed `AgentErrorCode`)
  before reaching the controller. No task is created, no capability runs, and the request never falls
  through to the realtime conversation's legacy tools.

## Deliberately not in the catalog

Matching M10's hard no-go list and M9 S4's own mutation boundary: shell/terminal/command execution,
arbitrary filesystem writes, generic upload/submit/send/purchase, Git mutation, dependency installation,
raw mouse/keyboard/coordinate control, and the semantic desktop mutations themselves
(`DESKTOP_SET_VALUE`/`DESKTOP_SELECT`/`DESKTOP_INVOKE`). A test in `agent-capabilities.test.ts` walks every
catalog id against a list of forbidden-fragment spellings and asserts none match.

## Adversarial review

One fresh Claude general-purpose agent reviewed the complete staged diff independently, with no context
beyond the plan doc and the code itself (this project's "Claude-only" review policy). It traced:

* the schema/parsing boundary end to end (`parseInterpretation` -> `orchestrationObjectiveFromWire`),
  confirming the hand-written parser never trusts the JSON Schema and independently rejects every extra
  field;
* the catalog's runtime integrity (every id matches its record key; `isAgentCapabilityId` uses `Set.has`,
  not property/`in` lookup, so `__proto__`/`toString`-style probing does not apply);
* the fail-closed property of the new `orchestrated_task` path in `process()` (model down, malformed JSON,
  an unknown intent, or extra fields all either throw -- caught by `.once()`'s catch as `REQUEST_FAILED`
  -- or fall through to the rules fallback; never to `conversation`, never to execution);
* the new `ORCHESTRATION_CUES` regex set by hand, against every phrase in the existing
  `src/shared/plan-wire.test.ts` `interpretByRules` suite, finding no false-positive overlap with
  appointment/clinic/status/check-booking/remember phrasing.

**Findings: none at reportable severity.** Two informational observations were raised, both addressed:

1. `ORCHESTRATION_CUES` has first-mover advantage over `check_booking`/`status` in `interpretByRules`.
   This is intentional (a code comment and a dedicated regression test,
   `does not mistake "check my project status" for check_booking`, already pin it) and the reviewer
   confirmed no existing fixture collides with it.
2. `agentCapability(id)` trusted its `AgentCapabilityId` parameter type instead of re-validating at
   runtime. Since a future S2/S3 caller will resolve a planner's own (untrusted) output through this
   function, it was hardened to accept `unknown`, call `isAgentCapabilityId` itself, and throw on anything
   that fails -- the same defense-in-depth style every other boundary in this codebase already uses.
   `agentCapability(id: AgentCapabilityId)` became `agentCapability(id: unknown)`; a regression test
   (`agentCapability re-validates at runtime rather than trusting its caller`) proves `'shell'`,
   `'toString'`, `'__proto__'`, `undefined` and `42` are all refused.

The lead session independently re-verified both by reading the changed code, not by trusting the
reviewer's description.

## S1 tests

`src/shared/agent-capabilities.test.ts` (10 tests) and `src/main/agent/task-request-interpreter.test.ts`
(29 tests), covering every item from the plan doc's S1 test list:

* existing `appointment_plan`, `clinic_info`, `public_research`, `status`, `check_booking`, `cancel_task`
  requests parse identically to before;
* a general document/project/desktop/mixed request classifies as `orchestrated_task` with the raw text as
  its bounded objective, from both the model schema and the deterministic rules fallback;
* a reply naming an intent outside the closed enum, an `orchestration` payload carrying a capability id,
  URL, path, command, selector or approval field, or a top-level field the intent does not declare, is
  refused whole (`parseInterpretation` throws);
* an empty, over-500-character or control-character objective is refused;
* the capability catalog: every id is unique and matches its record key, every descriptor has every
  required field explicitly, no id matches a no-go fragment, every effect-lock-bearing capability is
  marked as having a side effect, and the eight S2-read-only capabilities are marked as having none;
* `isAgentCapabilityId`/`agentCapability` reject near-miss spellings and non-string/prototype-probing
  input;
* malformed provider output (non-JSON, or JSON smuggling a `capability`/`path` field) and an unavailable/
  unconfigured router both fall back to the deterministic rules rather than executing anything or
  reaching `conversation`;
* `orchestrated_task` is `handled: true` from both `route()` and `submit()` (never `handled: false`, so it
  can never reach the legacy realtime conversation), the same request id is answered once however often it
  arrives, and ordinary conversation is still `handled: false`.

## Validation

* `npm.cmd run typecheck`: clean.
* `npx vitest run`: 2,501 passed, 22 skipped; the only 3 failing files are the pre-existing, documented
  machine-specific baselines (`real-inference.test.ts`, `tokenizer-pack.test.ts`, `accessibility.test.tsx`'s
  scam-card CSS assertion) -- unrelated to this diff, which touches no vision or CSS code.
* `npm.cmd run build`: main/preload/renderer all build clean.
* Python/`uv`/desktop/browser suites: untouched by this slice (no Python file changed) and not re-run.
* `npm.cmd run package:dir`: not re-run for a vocabulary-only TypeScript slice; will be re-run at the M11
  final audit per the plan doc.

## What S1 does not do (by design)

No durable orchestration exists yet. No capability in the catalog can be selected or executed. No new
route, IPC channel or Python endpoint was added. `orchestrated_task` always answers
`orchestration_unavailable` today; Milestone 11 S2 is what creates a durable orchestration graph and wires
a real one-step-at-a-time planner (structurally modeled on `research-planner.ts`) to choose from this
catalog.
