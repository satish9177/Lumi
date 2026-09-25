# Milestone 12: trusted resource composition and general assistant completion

> **General planning is not general authority.** M11 gave the orchestrator a closed capability catalog and a
> durable graph, but composed only three capabilities that never needed to name *which* document, account,
> desktop window or project among several -- there was always at most one obvious target. M12 adds the one
> thing M11 explicitly deferred: a controller-issued, durable **resource-ref registry**, so the planner can
> say "capability X, on resource r3" without ever seeing a path, a URL, a native handle or an account
> identity, and without ever being trusted to have picked correctly -- the controller checks that
> independently, every time.

M12 is the final architecture/capability-composition milestone. After it, the plan is real product usage, UX
refinement, bug fixing, signing/release work and evidence-driven capability additions -- not a Milestone 13
that reopens boundaries this milestone deliberately leaves closed.

```text
M12 - trusted resource composition and general assistant completion

S1  trusted available-refs foundation                     the registry; no new capability composed
S2  documents + controlled download composition            inspect/read/compare/download/place
S3  account read + real manual handoff                      account_read; manual_handoff_required becomes real
S4  desktop + app + project-stop composition                 desktop_observe/reason/safe_action, app launch, stop
S5  form/workflow composition                                 form_prepare, workflow_prepare; STOP BEFORE SUBMIT
S6  completeness / general assistant eval / final audit       report catalog honestly; two final cross-slice passes
```

Each slice ships as its own implementation, adversarial review, targeted validation, documentation and
commit, in order, exactly like M9/M10/M11. If a hard requirement cannot be met without weakening an existing
boundary, M12 stops at the last completed slice and documents the remaining capability as deliberately
unavailable, with its reason.

M12 does not add: general shell/PowerShell/cmd/terminal typing, arbitrary executable launch, raw
mouse/keyboard/coordinate control, arbitrary filesystem access, generic upload/form-submission/send/purchase,
dependency installation, Git mutation, or CAPTCHA bypass. `security boundary > capability count` throughout.

## Why a resource registry is the right primitive

M11's three composed capabilities (`public_research`, `project_status`, `project_start`) never needed the
planner to choose *among* several documents, accounts, desktop windows or projects -- `dispatch()` in
`orchestration-coordinator.ts` always resolves "the" research task, "the" registered project. The next eight
capabilities this milestone may compose all have the opposite shape: there can be several approved documents,
several registered projects, several desktop windows. A planner cannot be given a raw path/URL/HWND to name
one (that would hand it exactly the authority `agent-capabilities.ts` already refuses to grant), and it cannot
be trusted to reconstruct one from prose. The fix used throughout M1-M10 for this exact problem is an opaque,
controller-issued reference the model can cite but never mint -- `observationId`, `elementRef`, `dataRef`,
`fileResultId`. M12 S1 generalizes that pattern one level up, into the orchestration graph itself.

## S1 -- trusted available-refs foundation

**Ships:** a durable, controller-owned resource registry over the existing orchestration graph; a closed
resource-kind enum; per-orchestration ownership and cross-orchestration isolation; expiry/consumption/
freshness plumbing; a closed capability x resource-kind compatibility matrix (empty for every capability
composed so far -- extended only by the slice that actually composes a consumer); planner-schema support for
selecting resources by ref; and the M11-residual fix of moving `orchestration_planning` into
`PRIVATE_TASK_CLASSES` before any privacy-sensitive capability is composed, backed by a structural regression
test rather than a comment.

**Deliberately does not compose a new capability.** `public_research`, `project_status` and `project_start`
keep exactly their M11 behavior; the only externally visible change is that a fresh `resources` resource
(`research_result_ref` / `project_status_ref`) is now minted alongside each one's existing bounded summary,
and that none of the three capabilities accepts a resource as input yet (an attempt to supply one is refused
`resources_not_supported`, proving "the planner can see a ref" never by itself grants "the planner may use it
here," even for a resource the current orchestration genuinely, freshly owns).

### Resource kinds (closed)

```text
public_url_ref          research_result_ref     account_context_ref     account_result_ref
document_ref            document_result_ref     transfer_ref
desktop_target_ref       desktop_snapshot_ref    desktop_result_ref
app_ref                  project_ref             project_status_ref
form_target_ref          form_result_ref         workflow_ref
```

All sixteen are declared now (mirroring how M11 S1 declared the full sixteen-capability catalog while
composing three), because each one already names a real, already-reviewed M1-M10 output or input class
(`agent-capabilities.ts`'s own `inputClasses`/`outputClasses`). Only two are minted by S1:
`research_result_ref` (from `public_research`) and `project_status_ref` (from `project_status`/
`project_start`, matching their existing shared `_OUTPUT_CLASS` grouping). Each later slice mints and, where
it composes the matching capability, consumes the kinds it actually needs; a kind gains a *consumer* only in
the slice that reviews that composition.

### Resource record

`orchestration_resources` (migration `0023`): id, orchestration_id (owner, RESTRICT), a per-orchestration
opaque `ref` (`r1`, `r2`, ... unique within the orchestration, meaningless across orchestrations), `kind`
(closed CHECK), `producing_step_id` (which step minted it), `parent_resource_id` (self-referential lineage,
nullable), `revision`, `privacy_class` (`public` / `private` / `none`, closed CHECK), a controller-authored
`safe_label` (bounded, template-only text -- never the underlying page/document/account/desktop content),
`single_use`, `consumed_at`, `expires_at`, `binding_digest` (nullable; unused by S1's two kinds, reserved for
a later slice's re-observability check), timestamps.

### Ownership, opacity, no minting outside the controller

* Every resource is bound to exactly one orchestration, one kind, one controller-issued `ref`. A lookup is
  always scoped to `(orchestration_id, ref)`, so a ref from another orchestration simply does not resolve --
  there is no code path that looks up a ref by itself.
* The model never mints a ref: `ORCHESTRATION_PLANNER_SCHEMA` gains an optional `resources` array of plain
  strings the model may *cite*; the controller resolves each one against the current orchestration's own
  registry and refuses anything it does not recognize, own or currently permit for the chosen capability.
* The renderer never mints a ref either -- there is no route that creates one outside `_commit_step`'s own
  mint call, itself gated by the existing revision-locked write path every other mutation already uses.

### Compatibility matrix, and "authority from visibility"

`CAPABILITY_RESOURCE_REQUIREMENTS: dict[str, tuple[str, ...]]` names, per capability, the exact ordered
resource kinds it accepts. S1 declares all three of today's composed capabilities as accepting **zero**
resources. `advance()` therefore refuses `resources_not_supported` for any non-empty `resources` list on any
capability composed so far -- including a resource the calling orchestration freshly, legitimately owns. This
is the strongest form of the milestone's core invariant (planner can see ref `r1` != planner may use `r1`
here) available before a real consumer exists, and it is what S1's adversarial tests exercise end to end
through the live planner loop, not only at the repository layer.

### Freshness, lineage, consumption

* A resource independently expires (`expires_at`) on top of the orchestration's own TTL/liveness gate that
  already fences every write; S1's two kinds set no independent expiry (their content does not go stale
  faster than the orchestration itself does), but the column exists so a later slice's re-observable kinds
  (a desktop snapshot, an account observation) need no further migration.
* `parent_resource_id` records lineage for a resource derived from another; S1 mints no derived resource, so
  it is always null today, but the column and its self-referential FK exist for S2's download -> place ->
  extract chain.
* `single_use` + `consumed_at` support a resource that may be spent exactly once (a transfer authorization);
  S1's two kinds are reusable reads, so `single_use` is `false` for both.

### Planner privacy, made structural

`orchestration_planning` moves into `model-router.ts`'s `PRIVATE_TASK_CLASSES` in this slice, before any of
S2-S5 composes a capability whose result could carry account-, document- or desktop-private content into the
orchestrator's own planning context. `OrchestrationPlanner` now pins its call to exactly one deterministic
recipient (the first configured provider for `orchestration_planning`, in route order) and never fails over,
matching every other private planner's "one recipient, zero failover" rule. A new structural test computes,
from the capability catalog itself (`mayDiscloseToProvider && resultPrivacyClass === 'private'`), the set of
capabilities whose disclosure could carry private content, asserts that set is non-empty (so the test is not
vacuous against today's catalog), and asserts `orchestration_planning` is in `PRIVATE_TASK_CLASSES` -- so a
future catalog edit that adds such a capability without this membership already holding fails a test, rather
than depending on a slice author remembering a residual note.

### S1 attacks (all fail closed)

Model invents `r99`; model cites another orchestration's `r1`; an expired resource; a wrong-kind resource; a
stale revision; a resource whose producing step/task was cancelled or the orchestration stopped; a raw path,
URL or selector smuggled alongside `resources`; the renderer submitting a raw resource id shaped like a UUID
instead of an opaque ref; the same resource cited against a capability requiring a different kind; provider-
or page-derived text that merely *looks like* `r1`. Full detail and verified evidence: `docs/reviews/milestone-12-s1.md`.

## S2 -- documents + controlled download composition

Planned to compose `inspect_public_page`, `document_read`, `document_compare`, `download_document`,
`place_downloaded_file` behind the S1 registry. **Shipped: `document_read` and `document_compare`**, via
`document_ref` (an approved M10 file root entry, added through a trusted action) and `document_result_ref`
(an extraction result). Document text does not automatically enter the orchestration planner's own context --
only a controller-authored, numeric-only result descriptor does (a character count, an overlap percentage),
per S1's `resultPrivacyClass` classification. `inspect_public_page`/`download_document`/
`place_downloaded_file` (which would need `public_url_ref`/`transfer_ref`) are deliberately deferred: they
need a trusted "URL as planner-selectable input" mechanism and a download-destination convention this pass
does not build, and building it hastily alongside documents was judged worse than shipping documents
reviewed and deferring the rest honestly. Full design, scope rationale and acceptance in
`docs/reviews/milestone-12-s2.md`.

## S3 -- account read + real manual handoff

Compose `account_read` via `account_context_ref` (from the existing authenticated browser/profile grant, not
a new "orchestrator account grant"). Make `manual_handoff_required` reachable for real (login, CAPTCHA,
MFA, unsupported control, ambiguous target) with a controller-authored handoff object; Continue always
re-observes and revalidates rather than assuming the handoff succeeded. No CAPTCHA automation. Detail in
`docs/reviews/milestone-12-s3.md`.

**Shipped:** `account_read` composed exactly as planned; `manual_handoff_required` reachable via the
existing, unchanged `login_required`/`account_changed`/`account_identity_unknown`/`left_site_scope` pauses
(MFA folds into `login_required` through the existing one-time-code/WebAuthn credential-surface signals; a
CAPTCHA-guarded sign-in likewise, since it already shows a credential surface). `unsupported_control` and
`ambiguous_user_choice` are **not** separately implemented: nothing in the existing, already-reviewed
authenticated-read detection distinguishes either today, and inventing new detection to manufacture coverage
was explicitly out of scope for this slice -- only states the existing implementation can detect reliably are
composed. Continue re-observes through one real, forced step every time (`AgentTaskController
.continueAccountRead`), never assumes success, and a fresh-review pass found and fixed two High findings
(an account-private answer leaking into the orchestration-planning model's own context, and a premature
Continue over-revoking a still-healthy grant) before this closed.

## S4 -- desktop + app + project-stop composition

Compose `desktop_observe`, `desktop_reason` (preserving M9's one-snapshot/one-provider/no-broadened-disclosure
rule), `desktop_safe_action` (focus/scroll only, matching the current catalog -- not the M9 S4 bounded
mutations, which stay excluded per `agent-capabilities.ts`'s own documented boundary), `launch_registered_app`
and `project_stop`, via `desktop_target_ref`, `app_ref` and `project_ref`. Detail in
`docs/reviews/milestone-12-s4.md`.

**Shipped:** all five composed exactly as planned. `desktop_safe_action` needed one small, deliberate schema
extension -- `OrchestrationPlanner` gained a closed, three-value `"operation"` sub-choice (`focus`/
`scroll_down`/`scroll_up`), valid only for this capability, since the existing `{action, capability,
resources, reason}` schema had no other way to say which of the catalog's own two safe actions was meant; the
scroll target control itself is still chosen entirely by trusted code, never the planner. A fresh-review pass
found and fixed two High findings (untrusted window-label text reaching the planner's trusted context with no
rule against following it; a `project_ref` registration missing a fresh liveness re-check other resource
kinds already had) and identified one accepted residual, written up rather than silently overridden:
`project_stop` has no approval gate specific to the orchestrated path, matching the direct one-click Stop
button's own `requiresApproval: false` catalog characterization from M11 S1, but reachable without any human
moment dedicated to that decision -- fixing it cleanly would mean either relaxing the durable graph's
one-task-one-step invariant or revisiting an M11 S1 catalog flag, neither of which this slice's reviewed scope
covers.

## S5 -- form/workflow composition

Compose `form_prepare` and `workflow_prepare` once lineage has been proven end to end. Inputs are trusted
refs only; **submission count stays zero** for orchestrated form preparation, with no alternate desktop/
browser route around that limit. Detail in `docs/reviews/milestone-12-s5.md`.

## S6 -- completeness, general assistant eval, final audit

Reports the catalog honestly: composed vs. deliberately unavailable, with a reason for each unavailable
entry. Two independent fresh-Claude cross-slice passes (Pass A: authority/lineage/privacy/injection/replay/
handoff/resume; Pass B: scheduler/loops/budgets/effect-lock/races/restart/stale-refs/duplicate-effects/Stop).
Full validation matrix (`mypy`, `pytest -m "not browser"`, `pytest -m desktop_uia`, targeted browser suites,
`npm.cmd run typecheck`, `npx vitest run`, `npm.cmd run build`, `npm.cmd run eval`, `npm.cmd run package:dir`).
Production certificate remains **NOT CONFIGURED**; real-account release remains **BLOCKED**; M12 changes
neither. Detail in `docs/reviews/milestone-12-s6.md` and the cross-slice `docs/reviews/milestone-12-final.md`.

## After M12

Stop architecture expansion. No Milestone 13. Next: real daily usage, UX refinement, latency/cost work, bug
fixing, capability quality, product packaging, the Authenticode certificate, signed-install validation, user
testing. A new low-level capability is added later only when actual usage demonstrates a repeated need.
