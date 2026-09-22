# Milestone 9, final: whole-slice cross-slice security audit

> **This is the audit S1-S5 were each built to survive together, not separately.** Every prior
> `docs/reviews/milestone-9-s{1..5}.md` review found its own slice sound in isolation. This review's
> only question is the one none of them could ask alone: can something forbidden in one slice be
> achieved by combining capabilities from two or more slices?

Status: **M9 engineering implementation COMPLETE. M9 final cross-slice audit COMPLETE. M10 NOT
started.** This does **not** mean release validation is complete: production-signed installed
validation remains blocked by the missing Authenticode certificate, exactly as every prior slice
documented.

## 1. Audit baseline and scope

* Audit baseline SHA (last pushed head before S3-S5): `94ae3892756b747e08f6976a29d3222d60297eab`
  (`docs: close milestone 9 S2 desktop disclosure`).
* Final local SHA: recorded in the closing commit that adds this file (a document cannot contain
  the hash of its own commit; see the session's final report).
* Diff reviewed: `94ae389..HEAD` before this audit's own fixes (102 files, ~20,600 insertions: all
  of S3, S4 and S5), plus a full re-read of the pre-existing S1/S2 code where a cross-slice question
  required it (identity/epoch/exclusion machinery, the provider firewall, retention).
* Documentation was treated as a claim, not authority, throughout: every finding below was verified
  by reading the actual current code, not by trusting a prior review's own description of it.

## 2. S1-S5 capability matrix (final)

| Slice | Effect(s) | Authority | Provider contact |
| --- | --- | --- | --- |
| S1 | Semantic UIA observation only | none needed (read-only, local) | none |
| S2 | Exact one-observation text disclosure + read-only reasoning | `desktop_disclose` grant, single-use | one recipient/model, one attempt, no image, no failover |
| S3 | Trusted focus, semantic scroll (closed step), registered-app launch | ordinary `actions`/`approvals` row per effect, exact single-use approval | none (S3 never talks to a model) |
| S4 | Bounded `ValuePattern.SetValue`, `SelectionItem.Select`, reviewed `InvokePattern.Invoke` (`NAME_TOGGLE`) | `desktop_action_plan` grant (planning/disclosure) is NOT execution authority; a SECOND, separate `actions`/`approvals` row (from-plan only) authorises the effect itself | planning only: one recipient/model, one attempt, no image, no failover; execution itself never contacts a provider |
| S5 | One scoped client-area screenshot (`capture`); a SEPARATE, later disclosure of a BRAND NEW screenshot | `desktop_vision_capture` grant (local only, no recipient field exists on the scope) is NOT authority for `desktop_vision_disclose` (a separate grant, created only after capture SUCCEEDED) | disclosure only: one recipient/model, one attempt, no failover; the ONE task class (`desktop_vision`) in the whole router allowed to carry an image while private |

No slice's authority extends into another's: verified structurally (Pass A, item 8) that every
grant read is filtered by an exact `kind` clause, that the S3/S4 execution ledger cannot be reached
from a disclosure grant, and that a capture grant's scope has no `recipient`/`model` field to
interchange with a disclosure grant's.

## 3. Cross-slice review process

Two fresh, independent, initially read-only Claude reviewers (this project's own "Claude-only"
policy for M9 S2+; no external model was used) were run in parallel against the full 23-vector
attack list in the audit brief, weighted per the brief's own split:

* **Pass A** -- authority and approval-boundary correctness, database/recovery correctness,
  privacy/classification firewalls, cross-task effects.
* **Pass B** -- Windows/UIA mechanics, process/surface identity, human-takeover timing, visual
  geometry/DPI, TOCTOU windows around every native call, source-scanner/native-capability escape.

Every finding from both passes was independently re-verified against the actual current code by
the lead session before any fix, per the brief's explicit instruction -- none was taken on a
reviewer's word. Two findings from prior slices' own residual lists were also revisited in depth as
part of this pass (the S4 raw-value ledger residual, explicitly named in the audit brief; and the
SECURITY.md documentation gap below).

## 4. Findings and dispositions

| # | Source | Severity | Finding | Disposition |
| --- | --- | --- | --- | --- |
| F1 | Pass B | **High** | `select_control` had no equivalent of `invoke_control`'s `SAFE_INVOKE_LABELS` allowlist -- only a referential check that the model's named refs exist in the approved projection. `SelectionItem.Select` is frequently activation, not mere highlighting (an HTML/Electron `<select>`-style `onchange`, or a command-palette-style "quick pick" list where selecting an entry runs it) -- exactly the class of risk S4's OWN adversarial review already named as the reason `invoke_control` needed an allowlist instead of a denylist, left unapplied to `select_control`. | **Fixed.** `SAFE_SELECT_CONTAINER_ROLES = {combo_box, radio_button}` (`app/domain/desktop_planning.py`), checked in `validate_planned_action`'s `SelectAction` branch: a `list`/`pane`-rooted (command-palette-shaped) container is refused as `unsupported_or_unknown_effect` before any execution card exists, the same failure mode and same layer Invoke's own check already lives at. Tests: `test_a_select_naming_a_list_rooted_container_is_refused_as_unreviewed`, `test_a_select_naming_a_combo_box_container_is_still_accepted` (false-positive guard) in `test_desktop_actions_service.py`, against a new fixture container (`u8`/`u9`) shaped like the concrete VS-Code-style attack. |
| F2 | Pass B | Medium | `focus()`'s single human-input check ran before a FRESH cross-process `root_for` COM round-trip and the native `SetFocus` call, both -- objectively wider than `scroll`/`set_value`/`select`/`invoke`'s own second checks, whose element/pattern is already held before their final check runs. | **Documented, deliberately not narrowed.** Closing it needs holding the acquired root across an added check, which conflicts with the source scanner's own `focus-target` pin (`.focus()` must be called in the exact shape `root_for(...).focus()`, specifically to make a held-reference dodge impossible). Loosening that pin under audit time pressure, for an already-bounded Medium gap, was judged riskier than the gap itself -- the same reasoning this project already applied to deferring S4's own raw-value residual rather than rushing a fix on a live, reviewed boundary. See `docs/SECURITY.md`'s S3 section and `test_root_for_is_reacquired_fresh_for_every_focus_and_the_second_call_is_not_a_reuse` (documents the exact shape of the gap with a regression-style test, does not close it). |
| F3 | Pass B | Low | The AST source scanner's `_is_constant` (any all-uppercase identifier is exempt from the forbidden-name check) exempted DEFINITIONS as well as references, so a cosmetically renamed method (`def INVOKE(self): ...`) could hide from the "definition" check, though not from the exact call-site/allowlist pins that would still catch the real, correctly-spelled primitive it would have to call to do anything. | **Fixed.** `_forbidden_name` gained an `exempt_constants` flag; `ast.FunctionDef`/`ast.AsyncFunctionDef`/`ast.ClassDef` names are no longer exempted (this codebase's own style never uses SCREAMING_CASE for a definition; only enum-member-style references legitimately do, and those are untouched). Tests: three new planted violations (`def INVOKE`, `def SET_VALUE`, `class SCROLL`) in `test_desktop_source.py`, verified against the full existing "allowed" snippet list (including real enum-member definitions like `class DesktopPattern(StrEnum): INVOKE = 'invoke'`) with no regression. |
| F4 | Pass B | Low (theoretical) | Raw COM vtable dispatch (`ctypes.cast` to a function-pointer array indexed by a numeric, reverse-engineered vtable slot, called via `WINFUNCTYPE`) is a class of bypass no name-based AST scanner can see, since it uses no forbidden literal name anywhere. | **Documented, not fixed.** Already implicit in the scanner's own docstring ("a tripwire and a pinned surface, not a proof"). Judged Low in practice: extremely unusual, conspicuous to any human reviewer, and requires deliberate reverse-engineering, not something that arises from an innocuous refactor. No practical, more-accidental-looking bypass was found after genuine effort. |
| F5 | Pass A | Medium | S5's vision-disclosure path re-exposes the same underlying concern S4's own deferred finding 3 named for text disclosure (a value Lumi itself wrote reappearing in a later, separately-approved disclosure of the same window) -- but with strictly LESS mitigation, since a screenshot carries no redaction pass at all (S2/S4 text disclosure at least applies identifier-pattern redaction; there is no OCR-plus-redact-plus-re-render pipeline for images, and none is planned as part of this capability). | **Confirmed, documented, deliberately deferred** -- not a bypass of any approval (it requires three separate human approvals in sequence: the original `SetValue`, a capture, and a disclosure, each showing what is about to happen), but a structural property of screenshots carrying no redaction at all. Closing it fully needs either withholding capture eligibility near a recent write to the same surface, or a real image-redaction pipeline -- both real design work, tracked as follow-up, not rushed into this audit. See `docs/SECURITY.md`'s S5 section. |
| F6 | Pass A | Low | S3/S4's `dispatch_id` and S5's `capture_id` shared one in-worker replay table (`_Dispatches`) keyed only by a bare UUID, distinguished per call site only by an `isinstance(replay, ExpectedResponse)` check with no explicit refusal on a mismatch -- a same-id-used-for-two-different-effect-kinds collision would silently fall through and let the SECOND effect run for real. Not attacker-reachable today (both ids are always fresh, unguessable, server-generated UUID4s, never renderer-supplied), but guarded only by that convention, not by code. | **Fixed.** `_Dispatches.begin()` now takes the caller's own expected response type and refuses (`DUPLICATE_DISPATCH`) on any type mismatch, fail-closed instead of fail-open, at all seven call sites (focus/scroll/launch/set_value/select/invoke/capture). Test: `test_the_same_dispatch_id_reused_for_a_different_effect_kind_is_refused_not_replayed_or_run` (reuses a completed focus dispatch's UUID as a capture id; asserts refusal and zero native capture calls). |
| F7 | This audit, item 15 (explicitly named in the brief) | High (pre-existing, confirmed by S4's own review) | The trusted `SetValue` text had a second durable copy in `actions.proposal` (the ordinary action-ledger JSONB column), outside the plan's immutable grant scope (`task_grants.scope`) the brief's own wording permits it in. No currently-reachable path to a model/log/task-event/generic-API-response was ever found (confirmed again, independently, by both this session and Pass A), but the architectural boundary was not closed. | **Fixed.** `SetValueProposal.value` is now `exclude=True` (never written to the durable ledger row at all -- only `value_ref` is); every read of a set-value action (initial card, a later status poll, and the `approve()` call that performs the write) re-resolves the exact text fresh from `task_grants.scope` via `DesktopActionService._resolve_set_value`, which fails closed (`desktop_action_invalid`) if the immutable grant cannot resolve the ref, rather than ever writing or displaying an empty value. See section 5. |
| F8 | This audit | Low (documentation) | `docs/SECURITY.md` had a dedicated trust-model section for S1, S2, S3 and S5, but was completely missing S4 (the first mutation-capability slice) -- the security document a reader would consult first did not describe the riskiest slice at all. | **Fixed.** Added a full "Desktop bounded semantic actions (Milestone 9 S4)" section, matching the style and depth of every other slice's section. |

**What both passes checked carefully and found sound** (summarized; see each pass's own report for
exact file/line citations, preserved in this session's transcript): grant-kind exhaustiveness and
non-interchangeability (every grant read is filtered by an exact `kind` clause; the CHECK constraint
across migrations 0013/0015/0016 is additive and closed); `task_grants.approval_input_tick` is
correctly S5-only by design, because S2's and S4-planning's `claim` steps never call the worker (no
native effect to protect against takeover for), while S3/S4's actual mutating effects use a
structurally different, independently-sound mechanism (`approve()` reads the baseline fresh, once,
synchronously, inside the same call that immediately claims and dispatches -- there is no separate,
later "claim" step for that ledger the way the grant model has, so the S5-class bug cannot occur
there by construction); provider/model identity is re-checked, not merely typed, at every hop from
card to claim for all three provider-contacting paths; startup recovery covers all five desktop
durable-attempt tables independently (`recover_unfinished_attempts`, `recover_interrupted_reconciliations`,
`desktop_disclosure_service.recover_started`, `desktop_planning_service.recover_started`,
`desktop_vision_service.recover_started`, the last of which covers both `desktop_captures` and
`desktop_vision_disclosures`); revoke/claim races are serialized by the task row lock plus a
status+revision compare-and-swap on every grant transition; a vision candidate structurally cannot
seed an S4 mutation input (no shared field, no code path, confirmed by grep on both sides); episodic
memory and the shared context builder have no path from a desktop task's own snapshot construction;
focus/surface substitution is defeated by `_focus_resolved` re-proving both the foreground HWND AND
its owning PID after the call; registered-app validation remains airtight against arbitrary
paths/shells/interpreters even combined with S4 mutations (VS Code's own command surfaces were
specifically checked and found unreachable through any currently-approved effect); Lumi
self-targeting exclusion is correctly re-derived fresh on every runtime restart (the runtime's own
lifecycle is hard-bound to Electron main's liveness via the parent watchdog and the kill-on-close
job, so no stale root-PID list can survive across a restart); `dpi.py`'s geometry/DPI arithmetic is
fail-closed on every branch traced, including negative-origin and off-every-monitor spoofed-data
cases.

## 5. S4 raw-value ledger residual: final disposition

**Fixed**, not merely documented. `services/agent/app/domain/desktop_actions.py`'s
`SetValueProposal.value` field gained `exclude=True`: `dump_proposal()` (used for the one and only
place `actions.proposal` is ever written) never includes it, so the durable ledger row now carries
only the opaque `value_ref`, exactly the same shape S3's other proposals already had for their own
display-only text. `DesktopActionService._resolve_set_value` (new) re-derives the exact trusted text,
every time it is needed -- the initial card, a later status poll via `GET /desktop/actions/{id}`, and
the `approve()` call that actually performs the write -- by following `plan_id` back to its
`desktop_action_plans` row and then to the plan's own immutable `task_grants.scope`
(`StoredValue.raw`), the ONE place S4 was always meant to keep this value at rest. The grant row is
never deleted and its `scope` is immutable-after-insert (the migration `0005` trigger), so this
resolves identically before or after the mutation has actually run. If it can ever NOT resolve (an
inconsistency that should not be reachable given the ledger's own invariants), the read fails closed
(`desktop_action_invalid`) rather than silently returning or writing an empty value.

Regression test: `test_the_raw_set_value_text_is_never_persisted_in_the_durable_action_row`
(`test_desktop_actions_service.py`) proves, against real PostgreSQL: the secret text is absent from
`actions.proposal` immediately after the card opens; a SEPARATE, later `service.get()` read still
returns the exact text (proving live re-resolution, not an accidental in-memory retention); the
approval performs the write with the correct text; and a THIRD read, after the effect has already
run and the grant is `COMPLETED`, still resolves correctly.

The second half of S4's original finding -- that a successful `SetValue` naturally makes its own
written text visible to a LATER, separately-approved disclosure of the same window, past
identifier-pattern redaction (which targets PII shapes, not "text this session caused") -- remains a
harder, more general problem this fix does not address (redacting "content Lumi itself is
responsible for" does not exist anywhere in this codebase's redaction machinery). It is unchanged by
this fix and remains recorded as a residual (see F5 above for its S5/vision-specific sibling, found
by this same audit).

## 6. Human takeover model (final)

One mechanism (`GetLastInputInfo`, an opaque tick, never input, no hook, no keylogger), applied
consistently: the baseline is always read AFTER the trusted click/confirm, never before, so the
approving click itself is never mistaken for takeover. Two structurally different but both-sound
timing models exist, verified in section 4 above: the ordinary `actions`/`approvals` ledger (S3
focus/scroll/launch, S4's execution proposals) reads the baseline fresh inside `approve()` itself,
with no separate later claim step; the `task_grants` model (S2 disclosure, S4 planning-disclosure,
S5 capture/disclose) persists the tick at confirm time (`approval_input_tick`, added by S5's own
finding 3) and a later `claim` reads that stored value, never a fresh one. Every effect that performs
a native Win32/COM call re-checks a SECOND time, immediately before that call, narrowing (never to
provably zero) the window to that one call's own unavoidable pattern/context acquisition -- except
`focus`, documented in F2 above as a wider, understood, deliberately-not-narrowed exception.

## 7. Approval-type separation (final)

Verified structurally, not just by convention: S2 text disclosure (`desktop_disclose`), S3
focus/scroll approval (ordinary `actions` ledger, no grant), S4 planning disclosure
(`desktop_action_plan`), S4 execution approval (ordinary `actions` ledger, reachable only via
`propose_from_plan`, never a direct route), and S5 image approval (`desktop_vision_capture` /
`desktop_vision_disclose`, two DIFFERENT kinds, neither substitutable for the other since
`CaptureGrantScope` has no `recipient`/`model` field at all) are five genuinely distinct authorities.
No code path was found, in either review pass or this session's own verification, that reads a grant
by id alone without also constraining `kind`, or that lets one authority fund an effect reserved for
another.

## 8. Provider routing (final)

Three provider-contacting paths exist (`desktop_planning` for S2, `desktop_action_planning` for S4's
planning, `desktop_vision` for S5's disclosure), each in `PRIVATE_TASK_CLASSES` (and, for S5, also
the sole member of `PRIVATE_VISION_TASK_CLASSES`): one recipient, one exact model, one provider
attempt, zero private failover, enforced by `ModelRouter`'s `permits` predicate plus the database's
own `UNIQUE`/terminal-state constraints, not merely typed. Provider/model identity is re-verified,
not re-typed, immediately before every claim (`canServe`) and again against the claimed grant's own
scope after. `desktop_vision` was confirmed, by grep, to be the only call site anywhere in `src/`
setting `taskClass: 'desktop_vision'`, and the only private class permitted to carry an image.

## 9. Effect recovery (final)

Every durable attempt table this milestone added is swept at startup, before the HTTP server accepts
requests, inside the exclusive `runtime_ownership` lock (so it cannot race a live call or run twice):
`recover_unfinished_attempts` (S3/S4 action-ledger attempts/dispatches) then
`recover_interrupted_reconciliations` (S4's `RECONCILING` state) then `desktop_service.sweep_expired`
(S1 retention) then `desktop_disclosure_service.recover_started` (S2) then
`desktop_planning_service.recover_started` (S4 planning) then `desktop_vision_service.recover_started`
(S5, covering both `desktop_captures` and `desktop_vision_disclosures` independently). Nothing here
retries an effect automatically; every recovery outcome is `OUTCOME_UNKNOWN`, requiring a fresh
observation and a fresh approval (S3/S4's read/write effects) or a human reconciliation report (S4's
three mutations specifically, via `reconcile`).

## 10. Visual isolation (final)

S5's capture pipeline was the most heavily re-verified area of this audit (three prior findings from
S5's own two review passes already hardened it: a fresh geometry re-read plus fingerprint comparison
immediately before the native call, a third human-input check closing the gap that fix itself
reopened, and a genuinely fail-closed `capture_scope_certain`). This audit's own re-verification
(Pass B, vector 22) traced every branch of `dpi.py`'s geometry/DPI arithmetic by hand and found no
degenerate-but-passing case, including negative-origin and off-every-monitor spoofed data. The one
genuinely new finding in this area (F5 above) is not about geometry or frame identity at all, but
about the complete absence of any redaction pass for pixels -- a structural property, not a bug in
the geometry pipeline, and is documented rather than code-fixed for the reasons given there.

## 11. Source scanner state (final)

Sound for what it is explicitly designed to be: "a tripwire and a pinned surface, not a proof" (its
own long-standing docstring, unchanged and still accurate). This audit closed one real precision gap
(F3: a SCREAMING_CASE definition could hide from the "forbidden definition" check) and confirmed one
theoretical, already-acknowledged limit (F4: raw COM vtable dispatch, invisible to any name-based
scanner by construction) without attempting to close it, judged not worth the risk of a rushed,
speculative structural rewrite for a vector requiring deliberate reverse-engineering rather than an
accidental refactor.

## 12. Validation matrix

Run in the order the brief specifies; each Python DB-backed suite alone, per this project's
shared-database constraint (never two DB-backed commands concurrently).

| Check | Result |
| --- | --- |
| `uv run mypy app tests` | Clean: 239 source files, no errors |
| `uv run pytest -m "not browser"` | 2201 passed, 1 failed, 323 deselected (8m19s). The one failure is the known pre-existing baseline `test_booking_routes_without_a_worker_answer_503` (this machine's `services/agent/.env` sets `LUMI_PUBLIC_INSPECTION_HOSTS`, documented since M9 S1/S2/S3/S4; unrelated to M9 and to this audit) |
| `pytest -m desktop_uia` (real Windows, this machine) | **49 passed**, 0 failed, on a clean re-run. A first full-matrix run (immediately after the large `not browser` suite, `npm run build` and `npx vitest run` had all just finished on this machine) showed 4 transient failures (later 3, all worker-generation-replacement/hang timing); every one of the 4-then-3 failing tests passed individually and the full 49-test suite passed cleanly end to end on the very next run with the machine otherwise idle -- machine-load-sensitive real-hardware timing, not a regression, matching the identical pattern S3/S4/S5 each already documented (real-window UIA tests are sensitive to ambient load and human input on this machine) |
| `npm.cmd run typecheck` | Clean |
| `npx vitest run` | 2366 passed, 1 failed, 23 skipped across 130 files (123 passed, 3 failed). The 3 failing files are exactly the pre-existing, machine-specific baselines documented since M8b S6 (`real-inference.test.ts` and `tokenizer-pack.test.ts` need local model files; `accessibility.test.tsx`'s scam-card CSS assertion) -- unchanged by this audit, which touched no TypeScript source |
| `npm.cmd run build` | Clean: main/preload/renderer all build |
| `npm.cmd run eval` | **118/118** eval cases passed |
| `npm.cmd run package:dir` | **ENVIRONMENT-BLOCKED**, confirmed identical to S5's own documented failure: the build, Chromium headless+headed launch verification and the bundled UIA backend import check all succeed; the byte-compile step (`compileall`) fails compiling a THIRD-PARTY file inside the embedded Python distribution's own `site-packages`. Directly confirmed the root cause is unchanged by running `dist/agent-runtime/python/python.exe -c "import unicodedata"` against this build's own bundled interpreter: `ImportError: DLL load failed while importing unicodedata: An Application Control policy has blocked this file.` -- this machine's Windows Application Control policy, unrelated to any code this project owns and untouched by anything this audit changed (no Python-distribution fetching, pruning or `build-agent-runtime.mjs` logic was touched). Application Control was not disabled, weakened or worked around |

**Browser regression suite:** re-run only if this audit's fixes touched shared (non-desktop)
architecture. The touched files this audit changed are `app/desktop/effects.py`,
`app/domain/desktop_actions.py`, `app/domain/desktop_planning.py`, `app/services/desktop_actions.py`,
`tests/desktop_source_scan.py`, `tests/desktop_fakes.py` and three desktop test files -- no browser,
booking or authenticated-reading code was touched, matching every prior slice's own precedent for
scoping this down with reasoning rather than by default.

## 13. `package:dir`

Same environmental blocker every S5 validation pass already recorded, re-confirmed rather than
re-litigated: this machine's Windows Application Control / endpoint-security policy blocks
`unicodedata.pyd`, a THIRD-PARTY stdlib extension module inside the embedded Python distribution's
own `site-packages`, from loading during the build's byte-compile step -- unrelated to any code this
project owns, not touched by this audit's fixes (none of which changed Python-distribution fetching,
pruning or the build script). Application Control was not disabled, weakened or worked around.

```text
package:dir:
  ENVIRONMENT-BLOCKED (unchanged from every S1-S5 validation pass on this machine)

packaged-runtime validation:
  PENDING compatible Windows environment/policy
```

## 14. Two-framework validation status

Unchanged from S3's own honest accounting: one deterministic Win32/ctypes fixture (all slices) plus
one real classic Win32 application, Character Map (S3's registered-launch real-hardware test only).
No second real, independently-authored application framework (a real Win32Forms/WPF/UWP/Electron
application, for instance) has been validated against any S4/S5 primitive on real hardware. This
audit did not add new real-hardware coverage (no code path it touched required it: F1's fix is a
planning-layer referential check, provable against fakes; F2 is documentation-only; F6's fix is
worker-internal bookkeeping, provable against fakes).

## 15. Production certificate status

Unchanged.

```text
Windows signing infrastructure    READY
Production certificate            NOT CONFIGURED
Real-account release              BLOCKED
```

This audit performed no packaging or signing work and does not change this status. Nothing here
self-signs, weakens, bypasses or otherwise "satisfies" this gate -- it remains open pending an
acquired, human-confirmed production certificate from a publicly trusted CA, exactly as every prior
M9 slice and the M8a/M8b signing work already documented.

## 16. Remaining residual risks (carried forward + this audit's own)

Every residual risk each of `docs/reviews/milestone-9-s{1..5}.md` already documented remains true and
is not restated in full here (see each slice's own §residual-risks section and the matching section
of `docs/SECURITY.md`). New or changed by this audit specifically:

* **F2** (documented, not fixed): `focus`'s human-input recheck gap is wider than every other
  effect's, by one fresh COM round-trip, for the scanner-safety reason given above.
* **F5** (documented, not fixed): a screenshot carries no redaction pass, so a window recently
  written to by an approved S4 `SetValue` can, through two FURTHER separate approvals, show that
  exact text to a vision provider.
* **F4** (documented, acknowledged limit, not a new residual): raw COM vtable dispatch remains
  outside what any name-based source scanner can see; unchanged from the scanner's own long-standing
  self-description.
* The second half of S4's original finding 3 (a written value naturally reappearing in a later TEXT
  disclosure of the same window, past identifier-pattern redaction) is unchanged by this audit's fix
  to the FIRST half (the durable second copy) -- see section 5.
* Everything else this audit checked in section 4's "found sound" paragraph is, as stated, sound; it
  is listed there rather than here because it is not a residual, it is a verified-closed question.

## 17. Completion

```text
M9 engineering implementation      COMPLETE
M9 final cross-slice audit         COMPLETE
```

This does **not** mean release validation is complete. Production-signed installed validation
remains blocked by the missing Authenticode certificate; real-account release remains **BLOCKED**;
M10 has **not** started.
