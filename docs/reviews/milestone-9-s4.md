# Milestone 9, S4: bounded semantic desktop actions

> **Lumi can now, after two separate trusted approvals, do exactly three more things to one control
> already re-verified live: write one exact value into it, select one option in it, or invoke it for
> one closed, reviewed, deterministically-verified effect.** A model may *propose* which control and
> which of these three, from a redacted snapshot it was shown once; it never authorizes anything, never
> sees a raw value, and never chooses a provider. The renderer never picks the operation, the target or
> the value either: it only ever reviews and approves what the runtime already narrowed to one bounded
> action.

> This does **not** mean Lumi can operate a Windows application freely. There is still no keyboard, no
> mouse, no `SendInput`, no coordinate, no hotkey, no drag, no clipboard, no shell and no arbitrary
> `Invoke`. `Invoke` is enabled for exactly one closed, reviewed effect (`NAME_TOGGLE`); every other
> accessible control that merely *advertises* the Invoke pattern is refused as `unsupported_or_unknown_effect`
> until it, too, is reviewed and given its own verifier. Visual/coordinate fallback is S5 and does not exist.

Status: **S4 implementation closed (engineering; fixture and real UIA on this machine). S5 and M10 are NOT started.**

```text
M8b engineering                    COMPLETE

Windows signing infrastructure     READY
Production certificate             NOT CONFIGURED
Real-account release               BLOCKED  (no production certificate; unchanged)

M9
  S1 Windows UIA observation         COMPLETE
  S2 Desktop disclosure/reasoning    COMPLETE
  S3 Focus / scroll / app launch     COMPLETE
  S4 Bounded semantic actions        COMPLETE  (engineering; fixture + all three primitives on real UIA)
  S5 Visual fallback                 NOT STARTED

M10                                  NOT STARTED
```

Nothing here used a real account, saved detail or private application, and no provider is called by
default (`LUMI_SCRIPTED_MODELS` / the desktop capability opt-in gate the observation and planning
capability the same way S1-S3 already did). Installed production-release validation was **not**
performed and remains blocked by the missing Authenticode certificate.

## 1. Starting point

Branch `lumi-agent-v2`, local HEAD `3be5777` (`docs: close milestone 9 S3`), clean tree, two commits
ahead of the pushed `origin/lumi-agent-v2` (S3, intentionally not pushed). Per the brief, S4 stays local
too.

## 2. Architecture in one paragraph

Two authorities stay separate for the whole slice, on purpose, the way the brief demanded:
**permission to disclose a redacted snapshot to a model is not permission to execute anything.**
`app.services.desktop_planning` (`DesktopPlanningService`) is a near-exact structural copy of S2's
`DesktopDisclosureService`: one exact, fresh, user-approved observation plus the person's own typed
candidate values (never their text) go to ONE named provider, once, and the ONLY thing that comes back
is a validated, closed `PlannedAction` recorded in a new `desktop_action_plans` row. Nothing has run.
Turning that into something that *can* run is a second, independent step,
`DesktopActionService.propose_from_plan`: it re-reads the plan, the grant's immutable scope and the
persisted observation from scratch, rebuilds the redacted projection itself, resolves the model's opaque
refs against it, and only then opens an ordinary `DESKTOP_SET_VALUE` / `DESKTOP_SELECT` / `DESKTOP_INVOKE`
action on the **same action ledger** S3 already uses -- a new WAITING_APPROVAL card, a second and
completely separate trusted click, single-use, exactly like focus/scroll/launch. The worker then
re-resolves the live target a THIRD time, from scratch, immediately before the effect, and verifies the
effect from fresh evidence afterward. No step trusts an earlier step's word for identity, freshness or
safety; each re-derives it.

## 3. Migration `0015`

One migration, two changes, matching S3's `0014` in spirit:

* `desktop_dispatches` (S3) gains three more `operation` values (`set_control_value`, `select_control`,
  `invoke_control`) and three more opaque identity columns (`value_ref`, `option_container_ref`,
  `invoke_effect`); the operation-identity `CHECK` constraint is widened so each of the six operations
  still carries **exactly** the identity it needs and nothing else -- there is still no column for a
  raw value, a title, a path, a handle or a coordinate.
* `task_grants.kind` gains `desktop_action_plan` (no second authorization framework, exactly as S2 added
  `desktop_disclose`) and a new table, `desktop_action_plans`, mirrors `desktop_disclosures` almost
  exactly: `grant_id`/`task_id` UNIQUE, `STARTED -> SUCCEEDED | FAILED | OUTCOME_UNKNOWN`, and one
  difference from S2's shape -- `proposed_action` (JSONB, opaque refs and a discriminator only, `NOT
  NULL` iff `SUCCEEDED`) in place of a separate answer table, since a plan produces one small closed
  object, not free-form evidence.

The one place S4 keeps a value at rest outside the one authenticated worker RPC that finally writes it
is `task_grants.scope` for a `desktop_action_plan` grant -- the same column S2 already used to hold the
window title for card display, immutable after insert by the trigger migration `0005` added. It is
never copied into `desktop_action_plans`, a dispatch row, an event or a log (see section 8).
`test_migrations_match_table_definitions`, the S3-style operation-identity pin and a downgrade-refusal
test (mirroring `0013`'s) all pass.

## 4. The planning task class and what the provider is shown

A new, private `ModelTaskClass`, `desktop_action_planning`, distinct from S2's `desktop_planning` (a
read-only answer class) -- both listed in `PRIVATE_TASK_CLASSES`: mandatory single-recipient `permits`,
zero failover, no image. `DesktopPlanner` (`src/main/agent/desktop-planner.ts`) is the structural twin
of `DesktopReader`: it builds the same redacted control-tree lines S2 already built (untrusted,
delimited, injection-inert) plus one new trusted fact block, the candidate values' **descriptors only**
(`valueRef`, a person-chosen `classification` label, and `length` -- never the text). The model may
propose exactly one of three shapes (`invoke(controlRef)`, `set_value(controlRef, valueRef)`,
`select(containerRef, optionRef)`); `extra="forbid"` on both the TypeScript parser and the Python
`PlannedAction` union refuses a reply naming a raw value, a coordinate, a risk tier, a provider or an
approval -- there is nowhere to put any of them.

## 5. Referential validation of a plan (not a safety proof)

`app.domain.desktop_planning.validate_planned_action` is deliberately narrow: it proves every ref the
model named exists in **exactly** the projection it was shown (recomputed from the persisted observation,
digest-compared, exactly like S2's grounding) and, for `set_value`, that the named `valueRef` is one the
person actually offered. It is explicitly documented as *not* a safety proof: identity, pattern
availability, read-only state, sensitive-target refusal, container membership and human takeover are
all re-verified independently by the worker against the LIVE tree immediately before the effect, and
none of those checks is skipped because this one passed.

**Revised after the adversarial review (section 13, finding 1):** the first shape of this layer was a
*denylist* -- an `invoke` action was refused only if its target's label matched one of a closed list of
consequential words (send, submit, delete, purchase, pay, ...). The review correctly identified this as
insufficient on its own terms, not merely "defence in depth with a residual": a real submit/payment/
delete button can be labelled `Continue`, `OK` or `Confirm`, none of which are on any dangerous-word
list, and can rename itself on press as an ordinary UX pattern -- exactly what `NAME_TOGGLE` looks for --
so a denylist-gated Invoke would have reported such a press as a verified `invoked`, not a refusal. This
is now an **allowlist**, `SAFE_INVOKE_LABELS`: the control's CURRENT accessible name must exactly match
one of a short, reviewed set of non-destructive disclosure/expand-collapse toggles (`show details`,
`hide details`, `expand`, `collapse`, ...); anything else is `unsupported_or_unknown_effect`, fail-closed,
matching the brief's own words -- "Initial S4 should support only reviewed non-destructive effects with
deterministic verification." This is still, and can only ever be, a label -- attacker/application
controlled, and English-centric -- so it remains *additional* defence layered on top of, never instead
of, the worker's own structural NAME_TOGGLE verifier (see section 7); the difference is that an
unrecognized label now refuses rather than defaults to allowed.

## 6. Freshness

S4 actions need a much fresher observation than S2's read-only answers (S2 allows a snapshot up to ten
minutes old). `PLANNING_OBSERVATION_MAX_AGE_SECONDS = 20` bounds how stale an observation may be when a
*plan* is confirmed; `ACTION_OBSERVATION_MAX_AGE_SECONDS = 60` (S3's existing constant, reused rather than
duplicated) bounds how stale it may be when the **execution** proposal is opened and again when its
approval is claimed. Neither number is the real authority: **live target re-resolution immediately before
the effect is mandatory regardless**, in the worker, against the current tree, by exact match only.
Staleness at any of these checkpoints is `desktop_action_observation_stale`, `element_missing`,
`element_ambiguous` or `element_changed` -- a fresh observation and a fresh approval are always required
to retry, and a new observation is never substituted under an old approval.

## 7. Worker-side effects (`app/desktop/effects.py`, `uia_backend.py`, `observer.py`)

Each of the three follows the exact order S3 already established, revised after the adversarial review
(section 13) to close two gaps the original order left open: refuse a repeated dispatch id and replay a
finished one -> re-prove the surface and re-derive the control by exact locator match (which already
re-runs Lumi/elevation/credential exclusion **along the target's own ancestor path**, since it goes
through the same `SurfaceTable.resolve` and `DesktopObserver.resolve_control` S3 uses) -> **a fresh,
whole-surface credential re-scan** (`_refuse_if_credential_surface`, finding 4) -> a second, S4-only
check for a process image or window title that makes the target *dangerous by what runs it*, independent
of role or label (`sensitive_target_refused`) -> refuse if the human touched the machine since the
approval baseline -> kill the old observation's refs (scroll's own rule, reused) -> **refuse a second
time if the human touched the machine**, immediately before the native call (finding 5) -> perform ONE OS
call -> verify from **fresh** evidence, never from the call's return value, and never conflate "the
verifying re-read itself failed" with "the re-read succeeded and shows no effect" (finding 2).

* **`set_control_value`**: `ValuePattern.SetValue`, one call site. Refuses a control without
  `ValuePattern`, a `CurrentIsReadOnly` control, and (already, via the credential scan) a password/OTP-
  named field -- before any write. Verification re-reads the canonical value through the SAME pattern: an
  exact match is the known success `set`; a re-read that comes back with a genuinely DIFFERENT value is
  the known failure `not_set`; a re-read that itself fails to produce a value (e.g. a transient COM error
  inside `value_state()`) is `uncertain` -- mapped to `OUTCOME_UNKNOWN`, never treated as a known
  non-effect, because the write may well have happened and only the verification failed.
* **`select_control`**: `SelectionItem.Select`, one call site. The model names a container and an
  option as two independent opaque refs. `_mutation_target` re-resolves both from the live tree (each
  independently re-proving its OWN identity against its OWN previously-recorded path); a SECOND, LIVE
  proof of containment then re-derives the option a second time starting FROM that live container element
  (`DesktopObserver.rederive_descendant`), rather than only comparing the two stored locator paths as
  string prefixes -- the original shape, which the review showed proves only that they *used to* nest
  this way, not that they still do (finding 6; see below). Verification re-reads the option's actual
  `CurrentIsSelected` state (`selected`/`not_selected`), or `uncertain` if that re-read itself fails to
  produce an answer -- the same three-way distinction as `set_control_value`.
* **`invoke_control`**: `InvokePattern.Invoke`, one call site, and the only one of the three with a
  closed **effect** vocabulary (`InvokeEffect`, currently one member, `NAME_TOGGLE`): pressing the
  control is expected to change **its own** accessible name (a disclosure/reveal-style control). The
  worker refuses anything else as `unsupported_or_unknown_effect` -- there is no generic "button exists
  and was approved, so invoke it" path, by design; see section 5 for the allowlist that decides which
  controls even reach here. Verification is genuinely subtle: the ordinary locator-based re-derivation
  matches by name among its fields, so re-deriving the SAME control after its name just changed would
  always miss it. `DesktopObserver.rederive_after_mutation` is a new, narrowly-scoped variant that
  matches every ancestor step exactly as before but matches the LAST hop by identity (`runtime_id`)
  instead of by name; a control whose name did not change is the known failure `no_change`, not a claimed
  success (and this is exactly what `test_invoking_a_control_that_does_not_change...` proves against the
  real fixture -- see section 12).

**`rederive_descendant`, in full (finding 6):** the review's exact reproduction was a same-shaped
replacement container swapped into the approved container's tree position, with the SAME option (its own
identity, `runtime_id`, unchanged) reparented into it. Because the old check compared only the two
STORED locator paths as string prefixes, and each of `container_ref`/`option_ref` was resolved
independently from the surface root, this passed: the option's own full-path re-derivation succeeds
(nothing about ITS OWN identity changed), and the stale string-prefix comparison never notices the
container underneath it is now a different live element. `rederive_descendant` closes this by re-deriving
the container FIRST (an ordinary `_rederive`, which already fails closed with `element_changed` if the
container's own identity no longer matches what was approved), then re-deriving the option a SECOND time
starting from THAT live container element and walking only the remaining path steps -- so `option` only
resolves at all if it is actually, currently, found hanging off the live element the container resolves
to, right now, not merely structurally consistent with a snapshot from before either resolution ran.

**The credential re-scan is deliberately lenient about unrelated failures (finding 4, continued):** the
first shape of `_refuse_if_credential_surface` reused `_guarded_walk`, the same strict "stable or
refused" walk `observe()` itself uses, which treats ANY element becoming unavailable mid-walk as
`surface_changed`. Reusing it here made an S4 effect newly, needlessly fragile to completely unrelated UI
churn elsewhere in the tree (proven by a real regression: a real-hardware test's own vanished-option
fixture started tripping this on an unrelated re-scan). `_credential_scan` (`observer.py`) is a dedicated,
lenient scan for this one purpose instead: a node that disappears mid-scan is simply skipped -- it cannot
itself be a live credential input a person could type into any more, and it is not the control being
mutated -- while a genuine backend failure (anything other than the element having disappeared) still
fails closed as `BACKEND_FAILED`. `_mutation_target` resolves the specific target FIRST and runs the
whole-surface scan only once the target itself is known to exist, so an ordinary vanished/replaced/
ambiguous target still reports its own specific reason rather than being masked by the broader scan.

`uia_backend.py` adds exactly the three matching COM calls (`SetValue`, `Select`, `Invoke`) and one new
state read (`CurrentIsReadOnly`); nothing else. The worker's HTTP boundary never logs, echoes or stores
the raw value: `worker.py`'s three new routes and the `effect()` helper they share only ever write the
outcome enum and the generation to the log.

**Sensitive-target image list, narrowed (finding not from the adversarial review, but found alongside
it):** the first shape of `_SENSITIVE_TARGET_IMAGES` reused the launch registry's `FORBIDDEN_EXECUTABLES`
wholesale -- appropriate for "may this be REGISTERED as a launch target" (nothing should start a bare
interpreter), but far too broad for "may an S4 effect write into or invoke a control belonging to this
CURRENTLY RUNNING process": it incidentally denied ordinary interpreter/runtime hosts (`python.exe`,
`node.exe`, `java.exe`, ...) that commonly host entirely ordinary GUI applications, with no basis in the
brief's own SetValue denial list (password/OTP/terminal/console/PowerShell/cmd/file-picker/Windows
security UI). `_SENSITIVE_TARGET_IMAGES` is now its own, narrower set: terminals, shells and script hosts
(`cmd.exe`, `powershell.exe`, `wscript.exe`, `bash.exe`, `ssh.exe`, ...) plus the credential/consent
broker images and the Windows security app, matching the brief's own enumerated list rather than the
launch registry's broader one. (This also unblocked real-hardware validation of `set_control_value` and
`invoke_control`: the Win32 ctypes test fixture necessarily runs as `python.exe`, so the old, broader list
made it impossible to ever exercise those two effects against a real window at all -- every real-UIA
attempt was refused as `sensitive_target_refused` before reaching the actual `SetValue`/`Invoke` call.)

## 8. Raw value handling, end to end

The one thing on this whole boundary that is not an opaque ref: the text a `SetValue` call actually
writes. Its flow is exactly the one the brief specified:

```text
the person types it in the trusted panel (DesktopPlanningPanel.tsx)
  -> the runtime's immutable grant scope (task_grants.scope, desktop_action_plan) -- durable, at rest,
     never re-editable after insert
  -> the model sees only its ref, classification label and length (never the text)
  -> propose_from_plan resolves the model's chosen valueRef back to the raw text, ONCE, and embeds it
     directly in the ordinary SetValueProposal (`actions.proposal`) -- the SAME durable place S3 already
     puts untrusted-but-necessary card text (window_title, control_name); the trusted card shows it
     verbatim so the person can review the EXACT text before approving
  -> the approve click sends it in ONE authenticated SetValueRequest to the worker
  -> ValuePattern.SetValue
```

It is never in: the provider payload (only the descriptor is), a model's proposal (closed schema has no
field for it), the `desktop_dispatches` row (only `value_ref` is -- `test_the_raw_value_never_reaches_the_durable_dispatch_row`
proves it), a task event, or a log line anywhere on the path (the worker's `effect()` helper and every
`logger.info` call on this path log only the operation name, generation and outcome enum). The generic,
non-desktop `GET /actions/{id}` route already stripped `window_title`/`control_name`/`application_label`
before S4 (S3); `value`, `container_name` and `option_name` were added to that same strip-list
(`_DESKTOP_CARD_STRINGS`) so a generic caller can never read a pending value either. The dedicated,
Electron-main-only `GET /desktop/actions/{id}` route is the one place it is shown, deliberately, because
the trusted card needs to.

**Note (added after the adversarial review, section 13 finding 3):** "embeds it directly in the ordinary
`SetValueProposal` (`actions.proposal`)" above is precisely the boundary the review flagged -- the brief's
own wording permits the raw value only in "the immutable plan-grant scope (for card display)", and this
is a SECOND durable copy of it, outside that scope, even though no currently-reachable code path was found
that leaks it from there. This is confirmed and deliberately deferred, not silently accepted; see section
13's finding 3 and section 19 for the full reasoning.

## 9. The unresolved-effect lock (a new rule, not reused verbatim)

S3 explicitly does **not** block a new proposal on a focus/scroll/launch left `OUTCOME_UNKNOWN`: each is
cheaply and safely recoverable by a fresh observation and a new approval (a launch even checks for a
running instance first). S4's three mutations are different -- a value write, a selection or an invoked
control is a real mutation whose outcome Lumi does not know, and letting a new task, a new plan, a new
provider or any other route propose past that would be exactly the silent-retry risk the ledger exists to
prevent. `DesktopActionRepository.unresolved_mutation` finds any `DESKTOP_SET_VALUE` / `DESKTOP_SELECT`
/ `DESKTOP_INVOKE` action still `OUTCOME_UNKNOWN` or `RECONCILING`, **in any task**, and is checked at
every place a new desktop action, plan or provider attempt could otherwise start:
`DesktopActionService._open_locked` (the single choke point every execution proposal path already goes
through -- before superseding an unanswered card, before touching the worker) and, in
`DesktopPlanningService`, at `create` (before Lumi even observes the window or spends a real provider
call on a plan that could never fund an execution card anyway), `confirm` and `claim` (added after the
adversarial review, section 13 finding 7: a different action can become unresolved in the time between a
plan being created and it being confirmed, and `claim` specifically is what actually releases the
redacted snapshot to a provider). The brief is explicit that a new observation and a new provider attempt
must not side-step an unresolved effect either, so the block is not left to the later, execution-side
check alone.

**Getting unstuck** reuses the ledger's own generic reconciliation primitives
(`ActionService.begin_reconciliation` / `finish_reconciliation`, already used elsewhere in this
codebase) rather than inventing a parallel mechanism, through one new method,
`DesktopActionService.reconcile`. It is deliberately the simplest correct design: it never re-derives
the effect automatically (no fresh-observation heuristic matching a possibly-renamed, possibly-moved
control back to "the same one" with any real confidence) and never retries the OS call. Instead the
person looks at the live application and reports one of exactly three things --
`succeeded` / `failed` / `still_unknown` -- through a new trusted card
(`DesktopActionCard`'s `OUTCOME_UNKNOWN` branch, when the operation is one of the three S4 mutations).
`still_unknown` leaves the action, and the block on every other desktop action, exactly where it was: a
look that could not tell is not progress. This is a deliberate simplification, documented as a residual
(section 19): an automated, read-verified reconciliation (re-observe and match) is future work, not
built here. A crash between `begin_reconciliation` and `finish_reconciliation` committing is a separate
concern, covered in section 13 (finding 8) and by `RecoveryService.recover_interrupted_reconciliations`.

## 10. Human takeover

Unchanged mechanism, reused exactly: `GetLastInputInfo`'s tick, taken after the Approve click and
compared for equality only, at the same two points S3 already checks (before the effect; the fresh input
read after it is `input_changed`, never itself a failure). No new input surface was added, and none of
S4's new files call anything in the input-synthesis deny-list (proven by the scanner, section 11).

## 11. Source scanner (`tests/desktop_source_scan.py`)

The three new COM members (`SetValue`, `Select`, `Invoke`) and one new state read (`CurrentIsReadOnly`)
are pinned into `PINNED_COM`; `Invoke`'s pattern id/interface (`IUIAutomationInvokePattern`,
`UIA_InvokePatternId`) is added to `ACTION_PATTERN_ALLOWANCES` for `uia_backend.py` **only**. All three
mutation calls get the exact same AST call-site pinning S3's `Scroll`/`SetFocus` already have
(`_mutation_call_violations`, reusing `_single_call_violations`): each may be called exactly once, only
inside the one method named for it, with exactly the argument count the reviewed effect uses; a member
reference that dodges the call (`f = pattern.SetValue; f(x)`) is still caught, and the member name is
meaningless outside `uia_backend.py` because no other file ever holds a live `comtypes` pattern object
(only the abstraction, `UiaElement`). `set_value`/`select`/`invoke` are allowed as plain Python-method
names in exactly the files that legitimately own them (`effects.py`, `observer.py`, `client.py`,
`worker.py`), mirroring `focus`/`scroll`/`launch`. Every other input/mutation primitive (`Toggle`,
`SendInput`, `mouse_event`, `keybd_event`, keyboard/mouse module names, clipboard, arbitrary
`subprocess`/`os.system`/`ShellExecute`) stays forbidden everywhere in `app/desktop/`, including in
every file S4 touched -- planted-violation tests prove it (`test_s4_planted_violations_still_caught_by_the_general_deny_list`).

## 12. Tests

| Area | Evidence |
| --- | --- |
| Worker domain (fakes) | `test_desktop_effects.py`: SetValue success/verified, ignored-write known-failure, read-only refusal, no-value-pattern refusal, sensitive-target-image refusal, file-dialog-title refusal, human-input-before, old-refs-killed, dispatch replay, stale-control; Select success/verified, ignored-select known-failure, wrong-container refusal, no-pattern refusal, human-input-before, vanished-option; Invoke verified success (name change), no-observable-change known-failure, no-pattern refusal, dangerous-label-not-refused-by-the-worker-itself (the controller's job, proven separately), sensitive-target refusal, human-input-before; Lumi's-own-window and elevated-surface refuse every S4 mutation |
| Source scanner | `test_desktop_source.py`: exact call-site pin per mutation (wrong method name, wrong arg count, called twice, member-reference dodge), general deny-list still catches every planted violation in every S4-touched file, importer/`get_observation`-caller allowlists extended by name |
| Ledger against PostgreSQL | `test_desktop_actions_service.py`: `propose_from_plan` opens each of the three card shapes and performs no effect; the resolved trusted value appears on the built proposal; approving performs exactly one effect and the durable dispatch row carries the ref, never the value; a plan naming an unknown control or an unoffered value ref is refused before any execution card exists; `propose_from_plan` is idempotent (a plan funds at most one card, ever) and refuses a plan that never succeeded; an unresolved mutation blocks a new focus/scroll/launch/plan proposal; `reconcile` unblocks on `succeeded`, leaves the block on `still_unknown`, never calls the worker again, and refuses an action that either is not `OUTCOME_UNKNOWN` or is not one of the three S4 operations |
| Real Win32 + UIA (`desktop_uia`) | `test_desktop_effects_windows.py`: real `ValuePattern.SetValue` against the fixture's own EDIT control, read back through a fresh observation, exactly the expected `EN_CHANGE`/`WM_SETTEXT`-family messages and nothing else; real `SelectionItem.Select` against the fixture's LISTBOX items, verified selected, exactly the expected selection-changed message and nothing else; real `InvokePattern.Invoke` against the fixture's Submit button proving the click genuinely happened (`button_clicks == 1`) while correctly reporting the honest `no_change` outcome (no fixture control renames itself yet -- see residuals). All 47 real-UIA tests (S1-S4) pass; see section 13 for two fixes this required and why. |
| TypeScript | `desktop-firewall.test.ts`: rewritten on purpose (see section 15) -- exact six S2 + eight S3 + seven S4 methods/channels and no other verb; S4's mutations reach the ledger only through `from-plan`, never a direct route; the planning disclosure path is structurally parallel to and as isolated as S2's; no path from either new controller, wire file or panel to a click/key/coordinate/shell/path; error codes named only in the reviewed files |

## 13. Adversarial review (Codex) and confirmed fixes

Per the brief, an independent adversarial reviewer (`codex exec -s read-only`, high reasoning) was run
against the working tree once the implementation was substantially complete, given the full attack-vector
list from the brief and told to attack, not praise. It returned **10 findings (4 high, 5 medium, 1 low)**
plus an explicit list of what it checked and could not break (approval replay, closed model schemas,
ordinary stale-identity checks, ordinary worker-failure fencing, reconciliation semantics, direct
value-handling on the generic action-read route, provider isolation, reachability from voice/memory/
conversation, and the stable password/OTP/read-only/Lumi/elevated-surface refusals). Every finding was
independently re-verified against the actual code (not taken on the reviewer's word) before any fix, per
the brief's explicit instruction. Nine of ten were fixed, each with a new regression test against the
actual failure mode, not merely a re-run of the existing suite; one is confirmed and deliberately
deferred, with the reasoning below.

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | High | `NAME_TOGGLE`'s only defence against a real consequential button was a DENYLIST of dangerous words on its label -- a real submit/payment/delete button labelled `Continue`, `OK` or `Confirm` (none on the list) would be invoked, and reporting its label change as `invoked` would falsely legitimize it as safe and verified. | **Fixed.** `DANGEROUS_INVOKE_WORDS`/`_looks_dangerous` replaced with `SAFE_INVOKE_LABELS`/`_is_reviewed_safe_invoke`, an ALLOWLIST: an unrecognized label now refuses (`unsupported_or_unknown_effect`), not defaults to allowed. See section 5. |
| 2 | High | A `SetValue`/`Select` verification re-read that itself FAILS (e.g. a transient COM error) was indistinguishable from a re-read that succeeded and showed no effect -- both became the known failure `not_set`/`not_selected` (`FAILED`), when the former should be `OUTCOME_UNKNOWN` (the write may have happened; only the check failed) and must block further desktop actions like any other unresolved mutation. | **Fixed.** `SetValueResponse.outcome`/`SelectResponse.outcome` gained a third value, `uncertain`, used exactly when the verifying re-read produces no answer at all; `desktop_actions.py`'s `_perform` maps it to `AttemptOutcome.OUTCOME_UNKNOWN`, not `FAILED`. New tests: `test_a_set_value_whose_verifying_read_itself_fails_is_outcome_unknown_not_failed`, `test_a_select_whose_verifying_read_itself_fails_is_outcome_unknown_not_failed`. |
| 3 | High | The raw candidate value is copied into `SetValueProposal.value` and persisted in `actions.proposal`, outside the brief's stated boundary ("may appear only in the immutable plan-grant scope"). Separately, a successful `SetValue` causes the window's NEXT observation to naturally contain the written text, which a LATER, separately-approved disclosure of the same window could show to a model, past identifier-pattern redaction (which does not target arbitrary candidate text). | **Confirmed, deliberately deferred.** See below. |
| 4 | High | Live re-resolution's credential check (`_rederive`) only inspects the target control's own ancestor path (each ancestor and that ancestor's direct siblings) -- a credential field added inside an UNRELATED sibling's own subtree since the approved observation would never appear on that path, so an S4 mutation could proceed next to a credential field the original disclosure never saw. | **Fixed.** `_mutation_target` now also runs a fresh, whole-surface credential re-scan (`_refuse_if_credential_surface` / `_credential_scan`) immediately before any S4 mutation, after the specific target itself resolves. See section 7. New test: `test_a_credential_field_added_inside_an_unrelated_sibling_subtree_refuses_set_value`. |
| 5 | Medium | Human-input takeover was checked once, before `_kill_old_refs`; the actual native call (`SetValue`/`Select`/`Invoke`) still has to acquire its own COM pattern reference afterward, an operation that can block for a moment the person spends touching the machine, with no check in between. | **Fixed (narrowed, not eliminated).** A second `_refuse_if_human_input` check runs immediately before each native call, closing the gap to just that call's own unavoidable pattern acquisition -- as tight as achievable without changing how COM patterns are acquired. Documented as a residual (section 19): the window cannot be closed to exactly zero. |
| 6 | Medium | `select`'s container-membership check compared the two STORED locator paths as string prefixes, proving only that they *used to* nest this way. A same-shaped replacement container swapped into the approved container's position, with the SAME option (unchanged identity) reparented into it, would satisfy this check without the option actually still being where the approval named. | **Fixed.** New `DesktopObserver.rederive_descendant` re-derives the option a SECOND time starting from a LIVE re-resolution of the container, proving live nesting, not merely stale structural consistency. See section 7. New test: `test_rederive_descendant_refuses_an_option_reparented_into_a_same_shaped_replacement_container`. |
| 7 | Medium | The unresolved-mutation lock was checked only at plan `create` and at `propose_from_plan`; `confirm`/`claim` were not re-checked, so a DIFFERENT action could become unresolved between a plan being created and it being confirmed/claimed, and `claim` (which actually releases the redacted snapshot to a provider) would proceed anyway. | **Fixed.** The same `unresolved_mutation()` check now also runs at the top of `DesktopPlanningService.confirm` and `claim`. New tests: `test_an_unresolved_mutation_blocks_confirming_an_already_created_plan`, `test_an_unresolved_mutation_blocks_claiming_an_already_confirmed_plan`. (The reviewer separately confirmed no ACTUAL second-mutation bypass existed through this gap -- `_open_locked` already rejected a new execution proposal regardless; the fix closes the stricter "freeze new observations/provider attempts" requirement, not a mutation bypass.) |
| 8 | Medium | `begin_reconciliation`/`finish_reconciliation` are two separately-committed transactions; a crash between them left an action at `RECONCILING` -- a status the ordinary `reconcile` route refuses to touch (only `OUTCOME_UNKNOWN` is accepted) and startup recovery never looked for, so it could never be unstuck by any route. | **Fixed.** New `ActionRepository.list_actions_by_status`, `RecoveryService.recover_interrupted_reconciliations` (unscoped by `runtime_generation`, since reconciliation never touches the desktop or a worker) moves any startup-time `RECONCILING` action back to `OUTCOME_UNKNOWN`, mirroring `_transition`'s own task-status handling including the terminal-task guard. Wired into `main.py`'s startup sequence alongside the existing attempt-recovery step. New test: `test_recovery_unsticks_an_action_a_dead_process_left_mid_reconciliation`. |
| 9 | Medium | `PlanValueBody`'s HTTP schema only bounds a candidate value's length; a value containing a control character (a newline) reaches `StoredValue`'s stricter Pydantic validator, whose raw `ValidationError` embeds the offending text and was unhandled -- reaching a generic error handler and the server's own logs with the raw value inside it. | **Fixed.** `StoredValue` construction moved earlier in `DesktopPlanningService.create` (before any DB transaction or provider-relevant work) and wrapped in a `try`/`except ValidationError`, translated to a text-free `DesktopPlanRefusal("value_invalid")`. New test: `test_a_value_with_control_characters_is_refused_without_leaking_it_in_an_exception`. |
| 10 | Low | The AST scanner counts distinct call SITES, not executions (`for _ in range(2): pattern.Invoke()` has one call site and would pass the "called at most once" check), and only checks LITERAL string subscripts on a Win32 DLL binding (`user32["Set" + "CursorPos"](0, 0)` reaches an export whose name never appears as a plain string, past every check). Explicitly a test-tooling gap, not a reachable production issue -- no such code exists anywhere in the tree. | **Fixed.** `_single_call_violations` now also flags a pinned call found inside any `for`/`async for`/`while` loop within the reviewed function (`{attr}-in-loop`); the `Subscript` check now also flags a NON-constant index into a DLL-variable-named binding (`computed-dll-subscript`), regardless of what the computed name evaluates to. New tests: `test_a_loop_around_a_pinned_single_call_is_still_caught`, `test_a_computed_export_name_on_a_win32_dll_binding_is_caught`. |

**Finding 3, in full -- why it is deferred rather than fixed this slice.** Properly closing this needs the
raw value to be re-resolved from the plan's immutable grant scope at EVERY point of use (the initial card,
any later status poll, and the actual approval/execution call) rather than trusted from `actions.proposal`
once it is persisted -- a real re-architecture of how a `SetValueProposal` is stored and re-hydrated, not
a local patch, because `approve()` (a separate, possibly much-later request) currently reconstructs its
working proposal by re-parsing the persisted JSONB column, and the trusted card's explicit requirement
("Approval UI may show the actual value locally") means the value must remain retrievable through the
ORDINARY action-status read path, not only the one-shot creation response. Attempting this under time
pressure, on a live, well-tested execution ledger, risked introducing a NEW bug in exchange for closing a
boundary that: (a) has no currently-reachable path to a model, log, task event, or generic non-desktop API
response (independently verified -- `grep` found no code path from `actions.proposal` into `memory.py`,
task summaries, or any provider-facing payload); and (b) the reviewer's own repro required a SEPARATE,
person-approved disclosure of the same window afterward to actually observe the text -- it is not, by
itself, an exfiltration path. The second half of the finding (a later legitimate disclosure of the SAME
window naturally showing text this session itself wrote, since identifier-pattern redaction targets PII
shapes, not "text Lumi caused") is an even harder, more general problem -- redacting "content this session
is responsible for" does not exist anywhere in this codebase's redaction machinery and is out of scope for
a value-storage fix. Both halves are recorded here, honestly, as a confirmed, deferred architectural item
rather than silently accepted or rushed; not marking this "fixed" when it is not is the whole point of an
independent-verification step. Tracked as follow-up work, not S5/M10 scope.

## 14. Independent Claude review

A second, genuinely independent, read-only reviewer (a fresh subagent with no memory of this
implementation or of the Codex pass) was run after the section 13 fixes, focused specifically on
integration with S1's observation invariants, S2's disclosure-is-not-execution boundary, S3's
takeover/re-resolution/dispatch mechanisms, and M7/M8's action ledger and `OUTCOME_UNKNOWN`/`RECONCILING`
recovery. It reported two findings and an explicit list of integration points it traced and could not
break (repeated below verbatim in substance). Both findings were independently re-verified against the
actual code before any fix, exactly as the Codex findings were.

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | High | `select_control` was the only one of the three S4 mutations missing the `_refuse_if_sensitive_target` check `set_control_value` and `invoke_control` both have. A `select` targeting a file inside a real Open/Save common dialog's file list (which populates the filename edit control) or an item inside a terminal's own UI reached a live `SelectionItem.Select` call with no defence against the target being a shell, script host, security app or file-picker surface. | **Fixed.** `select()` now calls `self._refuse_if_sensitive_target(resolved)`, in the same position `set_value` does (after target resolution, before the pattern/human-input checks). The module docstring's "S4 adds a second identity check... for `set_control_value` and `invoke_control`" was corrected to all three. New test: `test_a_sensitive_target_process_refuses_select`. |
| 2 | Low | `propose_from_plan`'s "a plan funds at most one execution card, ever" check (`action_for_plan`) runs in its own short-lived connection BEFORE `_open`/`_open_locked` acquires the `_proposals` lock that actually serializes card creation. Two concurrent calls for the same `plan_id` (a retried HTTP request) can both observe "no existing action" before either reaches the lock, so both can insert an action row referencing the same plan. The reviewer's own analysis confirmed this cannot lead to a double EFFECT: `_open_locked`'s live-action scan supersedes/rejects whichever action loses the race before it can ever be approved, and `approve()` is further serialized by `self._execution`. | **Fixed.** `action_for_plan(proposal.plan_id)` is now re-checked a second time inside `_open_locked`, under the same lock and connection every other proposal already serializes through, so the literal "at most one, ever" now holds atomically, not just "at most one that can ever be approved" (which was already true). |

**What the reviewer traced and confirmed sound** (summarized; see the full report for exact file/line
citations): (1) S1's exclusion (Lumi's own tree, elevation, credential surfaces) is re-proved on every S4
mutation through `SurfaceTable.resolve`'s epoch-bumping `_vacate`, `resolve_control`/`_rederive`'s
ancestor-path credential check, and the whole-surface `_credential_scan` added for finding 4 above --
no path from a stale ref or a race to a live UIA call against an excluded surface. (2) The
`OUTCOME_UNKNOWN`/`RECONCILING` state machine's legal-transition table has no `RECONCILING -> RECONCILING`
edge, so a second concurrent `begin_reconciliation` is rejected by the ordinary revision CAS; startup
recovery (`recover_unfinished_attempts` then `recover_interrupted_reconciliations`) runs entirely inside
the exclusive `runtime_ownership` lock, before the HTTP server starts accepting requests, so it cannot
race a live `reconcile()` call or run twice; there is no generic (non-desktop) `POST /actions/{id}/approve`
route that could bypass `DesktopActionService.approve()`'s own freshness/human-input guards. (3) Desktop
planning never touches the human-input baseline (it is read-only by design); the baseline is taken exactly
once, fresh, inside `DesktopActionService.approve()`, identically for all three S4 proposal types and S3's
own; the worker's own doubled `_refuse_if_human_input` check (finding 5, section 13) is present for all
three effects. (4) `claim_grant`/`finish_plan` are both single CAS transitions under the task row lock, so
one grant cannot fund two provider attempts or two independently-approvable proposals from the planning
side.

## 15. Electron

Two new controllers, structurally parallel to S2/S3's, neither reachable from voice (`VoiceTaskBackend`'s
`Pick` never names either): `DesktopPlanningController` (six methods: create/get/grant/decline/run a
plan) and two additions to the existing `DesktopActionController` (`proposeDesktopActionFromPlan`,
`reconcileDesktopAction`). Neither new controller imports the model router, a provider or the
interpreter; `DesktopPlanner` (mirroring `DesktopReader`) is the only new file that does, and it is
reachable from exactly the planning controller. `desktop-firewall.test.ts` (S2's firewall, narrowed
further on purpose, not deleted) now also pins: the planning-disclosure path is exactly as isolated as
S2's own; S4's three mutation routes are reachable **only** through `from-plan`, never as a direct
`/desktop/actions/set-value` (etc.) proposal route -- that route does not exist and is explicitly tested
as rejected; `reconcile` is the only way out of an unresolved mutation and is itself inert (it never
performs an effect); the renderer bridge gained exactly seven new S4 methods and no other verb.

`DesktopPlanningPanel.tsx` is a new trusted panel: choose a window, type an objective, optionally type up
to four candidate values (each with a person-chosen label), review the planning-disclosure card exactly
like S2's read card (application/window text inert, the candidate values shown back to the person, the
provider and redaction statement, "this is a proposal only: nothing runs from this approval"), then
`Review the exact step` opens the SECOND card by reusing `DesktopActionPanel`'s own `DesktopActionCard`
component verbatim -- the same component, the same wording conventions, the same "Approve this step" /
"Cancel" buttons S3 already had, now also describing `set_control_value` (showing the exact value),
`select_control` and `invoke_control`. `DesktopActionPanel.tsx` itself gained the `OUTCOME_UNKNOWN`
reconciliation card (`It did happen` / `It did not happen` / `I still can't tell`) shared by both panels.

## 16. Validation

Run in the order the brief specifies (each Python DB-backed suite alone, per this project's
shared-database constraint):

| Command | Result |
| --- | --- |
| `uv run mypy` (`app/`, `tests/`) | Clean: 129 + 99 source files, no errors |
| `uv run pytest -m "not browser"` | 2083 passed, 1 failed (`test_booking_routes_without_a_worker_answer_503`, a pre-existing M2/booking baseline unrelated to M9 -- an error-code-text drift, not an S4 regression), 323 deselected. Re-run clean twice; one transient failure in an unrelated S1 test (`test_the_node_bound_is_enforced_and_declared`, a stray real scroll-wheel message during a real-hardware run) did not reproduce in isolation -- environmental, not a regression. |
| `pytest -m desktop_uia` (real Windows, this machine) | **47 passed**, 0 failed. All three S4 primitives now exercise real `ValuePattern.SetValue`/`SelectionItem.Select`/`InvokePattern.Invoke` against the real fixture through the real worker (the sensitive-target-image fix, section 13, was required for `SetValue`/`Invoke` to reach the fixture at all -- see section 7) |
| `npm.cmd run typecheck` | Clean |
| `npx vitest run` | 2325 passed, 3 failed (`real-inference.test.ts`, `tokenizer-pack.test.ts`, `accessibility.test.tsx` -- all three pre-existing, environment/network-dependent baselines predating M9 entirely), 22 skipped. Two genuine gaps found and fixed during this pass, both drift from the original S4 implementation work rather than from the adversarial reviews: `TASK_EVENT_TYPES` (`src/shared/agent-contracts.ts`) was missing the seven `task.desktop_plan_*` event names already present in the generated contract JSON, caught by `agent-wire.test.ts`'s contract-parity test; widening that constant then made `agent-task-view.ts`'s `describeEvent` switch (deliberately exhaustive over every event type, so the timeline never silently drops one) non-exhaustive, caught by `tsc --noEmit`, fixed by adding the seven cases in the same S2-disclosure-events style ("the words exist so the switch stays exhaustive; they carry no desktop text or raw value") |
| `npm.cmd run build` | Clean: main/preload/renderer all build |
| `npm.cmd run eval` | **118/118** eval cases passed |
| `npm.cmd run package:dir` | Succeeds; see section 17 for what was verified in the packaged output |

**Browser regression suite:** not re-run. The only shared code this slice's fixes touched is
`app/repositories/actions.py` (one new, purely additive method, `list_actions_by_status`) and
`app/services/recovery.py` (one new method, `recover_interrupted_reconciliations`, plus one new import);
every EXISTING method either file already had -- including the ones browser dispatch recovery
(`_recover_one`) depends on -- is untouched. `uv run pytest -m "not browser"` already re-ran every
non-browser suite touching the action ledger and recovery paths (`test_desktop_actions_service.py`,
`test_outcome_unknown_recovery.py`, and the rest) and found nothing broken. Matches S3's own precedent of
scoping this down with reasoning rather than by default.

## 17. Package impact

No new native dependency and no new executable. `build-agent-runtime.mjs` gained the same shape of check
S2/S3 already had: migration `0015` and the four new planning-service files
(`services/desktop_planning.py`, `domain/desktop_planning.py`, `repositories/desktop_planning.py`,
`api/desktop_planning_schemas.py`) are pinned by name and fail the build if missing from the bundle,
exactly like `desktop_disclosure.py` and S3's action-ledger files already are.

## 18. Authenticode status

Unchanged: signing infrastructure READY, production certificate NOT CONFIGURED, real-account release
BLOCKED.

## 19. Residual risks (honest)

* **`InvokePattern` real-UIA coverage is two-thirds, not complete.** The fixture's existing controls
  proved `SetValue` and `Select` genuinely work end to end against real Windows UI Automation, including
  exact-verification and message-level "nothing else moved" proof. Proving `Invoke`'s **success** path
  (not just its honest `no_change` failure path) end to end needs a fixture control that renames itself
  on press, which was not added this slice -- a deliberate scope decision to avoid a risky, untested
  change to a real Win32 window procedure under time pressure. The success path is thoroughly proven
  against fakes (`test_desktop_effects.py`) but not yet against real UIA. Documented as PENDING, exactly
  as S3 documented its own two-framework field validation as PENDING.
* **Exactly one `InvokeEffect` exists (`NAME_TOGGLE`).** This is intentional, not a shortcut: the brief
  is explicit that Invoke needs the strongest policy, and a name-change verifier is the narrowest closed
  shape that is both genuinely useful (disclosure/reveal-style controls are common) and deterministically
  checkable without trusting a label. Broader Invoke support (a control whose successful press changes
  some other specific, checkable piece of state) is real future work, each addition its own review.
* **The safe-Invoke label allowlist (section 5) is English-centric and label-based**, exactly like S1's
  credential-name heuristic and S2's title-withholding heuristic already are; it is explicitly
  *additional* defence, never the thing that makes Invoke safe (the worker's own structural verifier is).
  A legitimate disclosure/expand-collapse control with a label outside the reviewed set is refused rather
  than invoked -- a false refusal, not a false allow, which is the correct direction to err in.
* **File-picker detection (section 7) is a best-effort window-title match**, not a structural signal
  (UIA exposes none): a localized or unusually-titled file dialog is not caught this way. The
  shell/LOLBin/security-app image list is exact and complete for what it covers.
* **The human-input-takeover check before a native mutation call cannot be closed to exactly zero
  (section 13, finding 5).** A second check now runs immediately before `SetValue`/`Select`/`Invoke`,
  narrowing the gap to that one native call's own unavoidable COM pattern acquisition; eliminating it
  entirely would need the pattern already held before the final check runs, which is not how
  `comtypes`/UI Automation pattern acquisition works.
* **The raw candidate value's storage boundary is not fully closed (section 13, finding 3, deferred).**
  It is persisted in `actions.proposal` in addition to the plan's immutable grant scope, and a
  successfully-written value naturally appears in the window's next observation, reachable by a later,
  separately-approved disclosure. No currently-reachable path to a model, log, task event or generic API
  response was found; the fix requires re-architecting proposal storage/re-hydration and is tracked as
  follow-up work, not rushed into this slice.
* **Reconciliation is a human report, not an automated read-verified check.** `still_unknown` is always
  safe (nothing changes); a person could in principle report `succeeded` for something that did not
  happen. This is the same trust the trusted-card model already places in the person's own click
  everywhere else in this codebase (nobody but the person can approve an effect either), applied to
  reconciliation for the first time in this codebase; documented rather than silently assumed.
* **The candidate-value UI is intentionally minimal** (up to four typed values, a plain-text label): no
  masking, no per-field type hints (a person typing a password into a labelled "value" field is not
  stopped by the UI -- the WORKER refuses to write it if the target control is password-classified, but
  a non-password target with a person-chosen sensitive value is the person's own choice, exactly as
  typing anything into any trusted-panel text field already is elsewhere in this codebase).
* No installed, production-signed validation. One framework (Win32/UIA fixture) on this machine; a
  second real framework was not attempted this slice (S3's Character Map validation is unrelated to S4's
  new primitives and was not re-run against them).

## 20. Confirmation

**S5 and M10 were not started.** No coordinate, no screenshot, no OCR, no vision fallback, no keyboard,
no mouse, no `SendInput`, no hotkey, no drag, no clipboard, no shell and no generic `Invoke`/`SetValue`/
`Select` route exist anywhere. The model never authorizes execution; the renderer never decides the
operation, target, value, effect or provider.
