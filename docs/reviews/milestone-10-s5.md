# Milestone 10, S5: cross-executor recovery and the consequential test action

> **An uncertain consequential effect cannot be repeated or bypassed by switching task, executor, provider, model or route.**

> One closed effect registry now decides, for every effect-bearing tool, which keys it holds, what may settle it and whether "not found" proves anything. Every path into `EXECUTING` takes the same database lock. The generic ledger routes can no longer mint, start or settle an effect. A booking whose answer was lost in a hard kill comes back `OUTCOME_UNKNOWN`, blocks every other consequential action in every task, and is settled only by a read-only lookup.

Status: **S5 COMPLETE (engineering).** The final whole-M10 audit is `milestone-10-final.md`.

```text
Starting SHA   72f0738c6aa62ed1f6275149759fa6ef4d5adb7c (docs: close milestone 10 S4)
Migration      NONE. `action_effect_keys` (0018) already allows every kind S5 uses, and its key-shape
               CHECK fits every key the registry derives (pinned by a test). Head stays 0020.
```

Only synthetic data and the reviewed local booking fixture were used. No real booking was made.

## 1. The effect registry (final shape)

`app/domain/effects.py` is closed code. Nothing in it can be supplied by a model, a provider, the renderer or a generic caller.

| Tool | Kind | Keys (derived from the persisted proposal) | Evidence and read-only operations | Absence |
| --- | --- | --- | --- | --- |
| `commit_booking` | `external_mutation` (global) | `booking:site:<site>` (an unparseable site: `booking:site:unparsed`) | the site's own `lookup_booking` by the reference derived from the action id | **per reviewed site** (`app.domain.sites`); unknown sites never |
| `transfer_download` | `download` + `file_create` | `transfer:source:<sha256(url)>`, `file:create:<root>:<sha256(fold(name))>` | quarantine markers, manifest, hash | authoritative only after reconciliation's own tombstone |
| `transfer_place` | `file_create` | `file:create:<root>:<sha256(fold(name))>` | file index at the destination and in the quarantine | authoritative (atomic same-volume rename) |
| `project_start` | `project_run` (global) | `project_run:project:<projectId>` (the controller supplies it from the confirmed grant; the registry checks it is exactly one key of the registered kind) | run row, (pid, creation time), the run's own job | **never** (only "never resumed" proves no project code ran) |
| `DESKTOP_SET_VALUE`, `DESKTOP_SELECT`, `DESKTOP_INVOKE` | `desktop_mutation` | `desktop:mutation:all` (one key, mirroring M9's one-unresolved-mutation-blocks-all rule) | the person's own look (M9 `reconcile`) | never inferred; settled only by the person's attestation |

* `resolve_effect_keys(tool, proposal, supplied)` is the only way keys come into being. Supplied keys must equal the registry's (derivable tools) or be exactly one per registered kind (project start); an unregistered tool can hold none. Anything else is `EffectKeysError`, fail-closed, before any write.
* `fold(name)` is `upper()` then `casefold()`: it joins every pair NTFS's upcase table joins (`Resume.pdf` / `resume.PDF`, dotless `ı` / `I`) and only ever over-locks. ASCII keys are unchanged from S2.
* `GLOBAL_TIER = {external_mutation, project_run}`, unchanged from the reviewed plan.

## 2. One lock, every path

`ActionService._claim_effect_keys` runs inside the claiming transaction of **every** path into `EXECUTING`: `start_attempt` (exact approvals: booking), `begin_exact_execution` (desktop mutations), `start_scoped_attempt` (transfers, project start). It:

1. resolves the keys from the persisted proposal (a pre-S5 row gets them now; stored keys must equal the registry's);
2. takes the transaction-scoped advisory locks: ONE global lock first -- **exclusive** for a claim holding a global-tier key, **shared** otherwise -- then the keys, sorted;
3. refuses (`effect_locked`) if another action holding one of the keys is `EXECUTING`, `OUTCOME_UNKNOWN` or `RECONCILING`, or if ANY global-tier action is in one of those states, or if any project run is `OUTCOME_UNKNOWN` (alive but unowned after a restart, or a refused liveness check) even though its start action resolved.

`settle_exact_approval` (which records SUCCEEDED without executing) refuses every effect tool. A refusal rolls back the whole claim: no attempt, no key rows, no consumed approval.

**Tightened, not weakened:** before S5 the global tier blocked only on `OUTCOME_UNKNOWN`/`RECONCILING`; it now also blocks while a booking or a project start is `EXECUTING`, and the shared/exclusive global lock means two concurrent claims can never both miss each other's uncommitted attempt. Ordinary claims on different keys still run side by side. Order everywhere: task row -> global advisory -> sorted key advisories -> grant/authorization rows.

**What is deliberately not keyed** (it cannot repeat any of these effects): read-only work (inspection, research, account reading, booking search/lookup), provider disclosures (each its own exact, single-use approval), the network-frozen local form fill and handover (M8 S6, zero network), desktop focus, scroll and registered launch (M9 S3), and workflow adoption (a database-only value). These run while a booking is unresolved. See residual 4.

## 3. Generic routes hold only inert actions

* `POST /tasks/{id}/actions` refuses every registered effect tool in any spelling (`effect_route_refused`, reason `use_booking_route` / `use_transfer_route` / ...), every other controller-owned tool name (`lookup_booking`, `inspect_public_page`, research and account-reading tools; S1-S4 refusals unchanged), and ANY proposal on a controller-owned task type (`file_transfer_task`, `project_run_task`, `document_task`, desktop task types) so no step's idempotency key can be squatted.
* `/actions/{id}/attempts`, `/attempts/finish`, `/reconciliation` and `/reconciliation/finish` refuse every effect tool. A generic "reconciliation finished: FAILED" was the route by which a non-authoritative absence could have become a safe retry.
* The booking keeps its reviewed path: generic `approval-request`/`approve`/`reject` (main's card), `browser-execution` and `browser-reconciliation`.
* `TransferService` and `ProjectService` match a step action by tool name as well as idempotency key.

Electron main never called the generic proposal route (its allowlist has only `GET /tasks/{id}/actions`), and it is unchanged apart from the new error codes' messages.

## 4. Booking Acceptance F

`tests/test_acceptance_f_booking_recovery.py` (real subprocess runtime, real browser worker with Chromium, the reviewed fixture site; TerminateProcess, no shutdown hook):

```text
approve (booking/prepare -> approve) -> browser-execution -> fixture creates the booking -> response held
-> hard-kill runtime AND worker -> assert both dead
-> restart:   1 action, 1 attempt, 1 consequential dispatch, original ids, action + task OUTCOME_UNKNOWN,
              no approval left, site booking_count = 1
-> attack:    same task prepare -> 409; original: generic attempt -> 422, re-approval -> 409,
              browser re-execution -> 409; generic POST commit_booking -> 422 effect_route_refused;
              NEW task, freshly approved, other slot -> 409 effect_locked, its approval withdrawn,
              no attempt, no dispatch; site submissions unchanged
-> restart again (3rd process): nothing recovered twice (one action.outcome_unknown event)
-> restart (4th) -> read-only lookup -> FOUND -> SAME action SUCCEEDED, same single attempt,
              1 consequential + 1 read-only dispatch, booking_count = 1, submissions = 1
```

The mirror scenario loses the response **before** the fixture commits: after the hard kill and restart the action is `OUTCOME_UNKNOWN`, the lookup returns NOT_FOUND, the fixture's reviewed declaration makes that authoritative, and the action becomes `FAILED`. The consumed approval can never be reused; the retry is a NEW action in the same task with a NEW exact approval, and the site ends with exactly one booking, made by the new approval.

**Non-authoritative absence** cannot be produced by the fixture (its declaration says absence is a fact, on purpose). It is proven against the same `reconcile_booking` code with a scripted read-only lookup: NOT_FOUND on an undeclared site stays `OUTCOME_UNKNOWN` and keeps the global lock; a lookup that fails to answer never clears uncertainty.

**Absence is fenced (review finding 1).** Even on the fixture, NOT_FOUND counts as authoritative only when the commit can no longer happen: every consequential dispatch either has the worker's own final answer, or went to a worker generation that is not the one answering now (that worker died with its browser). A commit whose answer the runtime lost while the same worker is still alive stays `OUTCOME_UNKNOWN`.

**Bounded.** Reconciliation is user-triggered only. Per action: two immediate lookups, then 15 s, 30 s, 60 s, 120 s, 300 s, 600 s; at most 8 in any rolling day; one lookup in flight at a time. The count is read and the lookup recorded in one transaction under the task row lock (durable, so a restart does not reset it). A refusal (`reconciliation_limited`) never changes the action. Startup closes lookups a dead runtime left open.

## 5. Other executors

* **Project runs.** Unchanged S3 evidence rules (never resumed -> FAILED, recorded and gone -> ENDED_WITH_RUNTIME/SUCCEEDED, alive -> OUTCOME_UNKNOWN), now composed with the lock: an uncertain start blocks a booking until `recover`/`reconcile`; a run alive but unowned after a restart blocks every keyed effect and no second server can start; reconcile is refused while the start is still `EXECUTING` in this runtime (finding 8). A missing process, a missing response or a failed health check never starts a second run.
* **Transfers and placement.** S2 semantics unchanged: never re-downloaded, evidence from quarantine, manifest, hash, size and destination identity, no second filename, no overwrite. An unresolved booking blocks both steps before any request or rename; two spellings of one Windows name are one destination across tasks.
* **Desktop mutations.** M9's own rule stays (an unresolved mutation blocks every desktop action). The ledger key adds the same rule across tasks and executors, an unresolved booking blocks an approved mutation before the worker is touched (whatever provider/model planned it), and the card stays answerable.
* **Cross-app workflow.** Its download and placement are refused like any other while a global-tier effect is unresolved.

## 6. Stop, cancel, startup

* **Stop/cancel** closes the task and withdraws open grants (unchanged per executor). It never marks an in-flight or unknown effect FAILED, never compensates, never deletes a download or placed file, and keeps every key and dispatch. A booking's own cancel is refused while its outcome is unknown. A read-only reconciliation already running may still record its verdict after Stop; the task stays CANCELLED.
* **Startup** (idempotent, generation-safe, one event per recovery): unfinished attempts -> `OUTCOME_UNKNOWN` (closing their dispatches); `RECONCILING` -> `OUTCOME_UNKNOWN`; open booking lookups closed; unresolved effect actions without keys keyed from the registry (a row whose keys cannot be derived gets conservative `unparsed:<kind>:<tool>` keys and can never start again); project runs settled from evidence. Restarting twice creates no second attempt, event or key row.
* **Durability.** The lock is the ledger: action status plus `action_effect_keys` plus `project_runs`. There is no in-memory lock state; it survives runtime, worker, Electron and provider restarts and task recreation.

## 7. Independent adversarial review (fresh Claude reviewer; no external model)

Read-only, ran nothing against the database. The lead verified every finding against the code.

| # | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| 1 | Medium | Authoritative absence had no in-flight fence: a commit whose answer the runtime lost (connection reset, runtime timeout below the worker's) could still be running in the SAME live worker when the lookup said NOT_FOUND -> FAILED -> a new approval books again -> the original lands. | `_commit_is_settled`: NOT_FOUND is authoritative only when every commit dispatch has the worker's own answer or went to a dead worker generation; otherwise `OUTCOME_UNKNOWN`. Regressions for both branches. |
| 2 | Low | A booking approval refused by the lock stayed APPROVED; after the first booking was found, one more click in the other task booked a second time on consent given while the first was unknown. | `execute_booking` withdraws (REJECTs, reason `effect_locked`) an approval the lock refused; a fresh card is needed. Regression; Acceptance F asserts it. |
| 3 | Low | Parallel reconcile calls on a RECONCILING action could all pass the lookup count before any recorded its dispatch. | Count and insert under the task row lock in one transaction; one lookup in flight at a time; startup closes orphaned lookups. Regressions. |
| 4 | Low | Idempotency-key squatting: a runtime-token holder could propose an inert action under `transfer-place`/`project-start` on a controller task and have the service reconcile or refuse around it. | Generic proposals refused on controller task types; services match tool name as well as key. Regression. |
| 5 | Low | Desktop launch/focus/scroll and the local form fill are not keyed. | Documented (section 2, residual 4): none can repeat a keyed effect; the plan's "desktop route" now reads "desktop mutation route". |
| 6 | Low/Note | Canonicalisation: `casefold` differs from NTFS upcase (dotless ı); a re-registered root gets a new id; 8.3 short names. | Name folding is `upper().casefold()` (regression). Root id and 8.3 names documented as residual 3; destination-absent checks and the no-overwrite atomic rename still refuse any real collision. |
| 7 | Note | The startup backfill failed open for an underivable row. | Conservative `unparsed:` keys of the tool's kinds (regression). |
| 8 | Note | Project `reconcile` could run in `_start`'s launch window and leave inconsistent run evidence (no duplicate run: the EXECUTING start still blocked). | Reconcile refused while the start is `EXECUTING`. |
| 9 | Note | The registry's desktop rule said "absence never" while M9 accepts the person's "failed"; an `assert` used as a control. | `human_attestation=True` on the desktop rule; the assert is an explicit check. The exhausted-budget liveness cost is by design (residual 2). |

## 8. Validation

All runs after the review fixes unless stated.

* `uv run mypy`: clean (338 files).
* `uv run pytest -m "not browser and not desktop_uia"` (known `test_booking_routes_without_a_worker_answer_503` environment baseline deselected): **2483 passed**. New: `test_effect_registry.py` (30), `test_effect_registry_review.py` (9), and S5 sections in the transfer, project, desktop-action and workflow suites. Acceptance C (`test_acceptance_c_open_editor_and_start_project.py`) is in this run and passed.
* Browser (real Chromium): research (M7), authenticated read and acceptance (M8a), form observation/planning/preparation and the local draft (M8b), booking, M3 lost-response hard kills, booking preparation, M6 acceptance, downloads, the combined S4 workflow (**submission counter 0**) and **Acceptance F**: **198 passed, 6 skipped** (environment-gated), 0 failed.
* `pytest -m desktop_uia`: 47 passed, 1 skipped, 1 failed -- the known environment baseline `test_the_fixture_appears_with_an_opaque_ref_and_no_native_identity` (live window titles on this desktop contain `python.exe` and backslashes); unrelated to S5.
* `npm.cmd run typecheck`, `npm.cmd run build`: clean. `npx vitest run`: 2460 passed, 23 skipped, and only the three known machine baselines fail (`real-inference`, `tokenizer-pack`, `accessibility`).
* `npm.cmd run eval`: **118/118**.
* `npm.cmd run package:dir`: **exit 0 -- PASS on this run**, a change from the S1-S4 baseline. Every bundle check passed (the S5 marker check included; migrations through `0020`), and `dist/agent-runtime/python/python.exe -c "import unicodedata"` now succeeds although `unicodedata.pyd` is unchanged and still unsigned: the Windows Application Control block that caused ENVIRONMENT-BLOCKED no longer reproduces on this machine. Nothing in Lumi weakened it, and nothing was self-signed (`Lumi.exe` is `NotSigned`; electron-builder's "signing" lines are no-ops with no certificate configured).

## 9. Residual risks

1. **Only the fixture declares authoritative absence.** Every real site is non-authoritative by default; a real-site booking whose answer is lost stays `OUTCOME_UNKNOWN` until a lookup finds it, and there is no manual "I checked, it did not happen" path for bookings (desktop mutations have one, M9).
2. **Liveness.** An unresolved global-tier effect blocks every keyed action, on every executor, until reconciled; an exhausted lookup budget can hold that for up to a day, and an unowned live project process until it exits. Deliberately conservative.
3. **Key identity.** Placement keys use the root id: revoking and re-registering the same folder, or an 8.3 short name, gives another key for the same destination. The destination-absent check and the no-overwrite atomic rename still refuse the second file.
4. **Unkeyed low-consequence effects** (desktop focus/scroll/launch, the frozen local form fill, provider disclosures) are not blocked by an unresolved booking. None can repeat a keyed effect; each keeps its own exact approval.
5. **Worker-generation fence.** A dead worker's in-flight request that the site receives only much later is not modelled; the fixture declaration (single writer, visible to the next lookup) is what makes absence a fact there.
6. Carried forward unchanged: S1-S4 residuals; production certificate NOT CONFIGURED; real-account release BLOCKED.
