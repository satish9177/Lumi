# Milestone 10: final cross-slice review

> **Cross-app orchestration is not cross-app authority.** Across five slices Lumi gained approved documents, one controlled download with no-overwrite placement, registered project runs, a cross-app preparation workflow that stops before submit, and one shared recovery boundary. No slice's approval implies another's, and no uncertain effect can be repeated by switching task, executor, provider, model or route.

```text
Baseline       eda16206ba2227b4035e7c81fe6da80a77d6c396 (M9 final, = main)
S5 commits     9ada712 feat(agent): close cross-executor recovery boundaries (M10 S5)
               42a1c3c docs: close milestone 10 S5
Audit fix      d00a4aa fix(agent): harden milestone 10 cross-executor boundaries
Final docs     this commit (docs: close milestone 10 final review)
Migrations     0017-0020 (S1-S4). S5 and the audit added none.
```

```text
M10  S1 approved documents + file broker   COMPLETE
     S2 downloads + placement              COMPLETE
     S3 project recipes                    COMPLETE
     S4 cross-app preparation              COMPLETE
     S5 cross-executor recovery            COMPLETE
M10 engineering implementation            COMPLETE
M10 final cross-slice audit               COMPLETE
main                                      NOT MERGED (separate decision)
Windows signing infrastructure            READY
Production certificate                    NOT CONFIGURED
Real-account release                      BLOCKED
```

## 1. Capability matrix

| Slice | Adds | Authority (each separate) | Never |
| --- | --- | --- | --- |
| S1 | M10 file roots (native folder dialog + native confirmation), handle-verified reads, bounded stdlib extraction in a contained helper, local compare, ONE provider disclosure of redacted excerpts | root READ; `document_disclose` (one provider, one model, one attempt, no failover; native confirmation since the audit) | write, copy, move, rename, delete; a path to the renderer |
| S2 | one download to Lumi's quarantine, then an atomic no-overwrite same-volume rename into one approved name | `file_transfer` grant (native confirmation) funding two single-use steps | overwrite, execute, auto-open, re-download after uncertainty |
| S3 | `npm run <script>` of a registered project from a registered recipe, suspended into its own Job Object | project registration, recipe registration and per-run start (each a native warning dialog) | shell of Lumi's own, model-authored command/args, install, Git, killing anything outside the run's job |
| S4 | download -> placement -> extraction -> optional provider disclosure -> per-detail adoption -> M8 exact form preparation, stop before submit | each step keeps its own approval; adoption is an exact approval with a native dialog; lineage decided by the database | submit, Enter, click, upload, navigate; provenance laundering |
| S5 | the closed effect registry, the shared cross-executor lock, generic-route closure, bounded read-only booking reconciliation, Acceptance F | none new: it only constrains when existing effects may start and how they are settled | repeat or bypass an uncertain effect |

## 2. Boundaries, as they stand at the end of M10

* **Authority separation.** Every grant lookup filters by kind; step authorizations re-check kind, revision, scope digest, expiry, action revision, proposal digest and runtime generation; at most one live grant per (task, kind). Both audit passes checked the eleven authorities (file root, document disclosure, transfer grant, transfer step authorization, project registration, project-start approval, adoption approval, account-read grant, form-plan grant, booking approval, desktop action approval) pairwise: none satisfies another's lookup.
* **Filesystem / root boundary.** Validated relative names; lstat walk refusing reparse points; handle-verified final path, identity, link count and hash; placement relative to a held parent handle, no-replace, same volume only; only pdf/docx/txt by signature.
* **Document privacy.** `document_private` excerpts, identifiers redacted, ONE provider and model chosen in main, one attempt, no failover, projection digest re-checked; since the audit the disclosure is confirmed by a native dialog and a stopped workflow's documents step can open no new one.
* **Download boundary.** GET only through the reviewed guard, redirects re-validated, quarantine first with a `started` marker before any request, evidence-based reconciliation with a tombstone fence, Mark-of-the-Web kept, never opened.
* **Project execution boundary.** Fixed argv under pinned Program Files Node.js, environment built from nothing, refused `.npmrc`/workspaces/hidden hooks, hashes re-checked before spawn, suspended start with durable (pid, creation time), readiness only when the run's job owns the port, Stop only ends that job.
* **Cross-app provenance.** `document_extracted` or `provider_derived` only; values read from the document, never a provider's quote; composite foreign keys pin lineage; a form step reads only its own live workflow's values.
* **Cross-executor effect locking.** One registry; every path into `EXECUTING` takes the same lock; global tier (booking, project run) blocks every keyed effect while in flight or unresolved, including an unowned `OUTCOME_UNKNOWN` project run; generic routes are inert.
* **Booking reconciliation.** Read-only `lookup_booking`, bounded, durable, one in flight; FOUND resolves the same action; NOT_FOUND is a failure only where the reviewed site declares absence authoritative AND the commit can no longer happen; a retry is a new action with a new approval.
* **Stop semantics.** Revokes future dispatch and open grants, rejects pending approvals, stops owned runs; never marks an in-flight or unknown effect failed, never compensates, never deletes a download or placed file, never erases keys, dispatches or evidence. Since the audit a stopped workflow's child steps take no new authority at all.
* **Startup recovery.** Idempotent and generation-safe: unfinished attempts -> `OUTCOME_UNKNOWN`; `RECONCILING` -> `OUTCOME_UNKNOWN`; orphaned lookups closed; pre-registry unresolved effects keyed (fail-closed); project runs and stranded start actions settled from evidence only.

## 3. Final audit (two fresh, independent Claude passes; no external model)

Both passes were read-only, ran nothing against the database, and covered the complete `eda1620..HEAD` diff. The S5-specific adversarial review is in `milestone-10-s5.md`. The lead verified every finding against the code.

**Pass A -- authority / privacy / provenance.** No High.

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| A1 | Low | Stop/expiry did not fence a workflow's child tasks: a stopped workflow's documents step could open/claim a provider disclosure, its form step could get account reading back, and grant revocation ran best-effort after the Stop transaction. | **Fixed** (`require_step_live`): document task lock (new files, extraction, disclosure card, confirm, claim), transfer confirm/download/place, account-reading prepare/confirm refuse `workflow_not_active`; revoke and result recording stay possible. Regressions (3). |
| A2 | Low | The S1 document disclosure was approved in the renderer alone, unlike every other M10 approval. | **Fixed**: `confirmDisclosure` native dialog in main built from the runtime's card (vitest regression: a cancelled dialog sends nothing). M8 form and M9 desktop disclosures keep their reviewed renderer cards (unchanged, noted). |
| A3 | Note | Generic `POST /tasks` accepted controller-owned task types (not exploitable: each service needs its own rows). | **Fixed**: reserved. Regression. |
| A4 | Note | Generic `/tasks/{id}/cancel` leaves `document_disclose`/`file_transfer`/`project_run`/`form_prepare` grants ACTIVE. | **Documented**: every use re-checks the task is open, and the route is not in main's allowlist. Hygiene only. |
| -- | S4 residuals | Planner wording "saved details"; placed file not locked after import. | **Verified unchanged**: the planner gets masked previews only; the file's identity and hash are re-verified at import and every extraction. |

**Pass B -- execution / recovery / TOCTOU.** No High; no hard no-go item reachable.

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| B1 | Medium | A `project_start` action stranded `OUTCOME_UNKNOWN` by a crash between its attempt commit and the run's `action_id` link (or between ending the run and settling the action) could never be settled, holding the global tier -- every keyed effect on every executor -- for good. | **Fixed**: the start action is found by (task, key, tool), linked, and settled from the run's final evidence at startup and by `reconcile` (never resumed -> FAILED; pid recorded -> it ran -> SUCCEEDED). Regressions (2). |
| B2 | Low | `reconcile` could race an in-flight `_start` and record false "never resumed" evidence. | **Fixed**: refused while this runtime is starting the run. Regression. |
| B3 | Low | A run that already exited could be flipped back to RUNNING by the start's own bookkeeping. | **Fixed**: `only_if=("STARTING",)` on the RUNNING and pid writes. |
| B4 | Low | The quarantine sweep could delete the payload of a placement left unresolved by a crash before the row moved on. | **Fixed**: any transfer with a step action in flight or unresolved keeps its quarantine. Regression. |
| B5 | Low | Placement keys use the root id, so a re-registered folder gets another key. | **Documented** (S5 residual 3): the destination-absent check and no-replace rename still refuse any second file. |
| B6 | Note | Job containment is described too broadly: project code can reach out-of-process brokers (WMI, Task Scheduler, COM, shell handlers) that Stop does not reach, and same-user code can read the runtime's memory. | **Documented** (SECURITY.md): covered by the user-level warning on every project card; a restricted token is future work. |
| B7 | Note | `stop()` racing a natural exit appends a STOPPED event; a transfer reconcile crash can leave a resolved action with a stale row (no lock held). | **Documented**: timeline/liveness only. |

**Rejected findings:** none. Every finding was confirmed; the ones not fixed are documented above with their reason.

## 4. Acceptance

* **Acceptance C** (`test_acceptance_c_open_editor_and_start_project.py`): registered VS Code launch path (M9 S3, with the per-user install still correctly refused) + an approved project recipe -> one owned Lumi run; a repeat request -> no duplicate server. Green in every full run after S5 and after the audit fixes (S5 and the audit touched project-run recovery, so it was re-run).
* **Acceptance F** (`test_acceptance_f_booking_recovery.py`, real subprocess runtime and worker, hard kill): one action, one attempt, one consequential dispatch, one external booking; restart -> `OUTCOME_UNKNOWN`; same task, new task, generic API, generic attempt and browser re-execution refused (a new task's refused approval withdrawn); a third process recovers nothing twice; read-only lookup -> the same action `SUCCEEDED`; zero duplicate effects. The mirror scenario (lost before commit) ends `FAILED` by the fixture's authoritative absence and is retried only by a new action with a new approval. Non-authoritative absence stays `OUTCOME_UNKNOWN` (service-level, scripted lookup).
* **Combined S4 prepare** (`test_workflow_acceptance_browser.py`, real headed Chromium): download -> placement -> extraction -> one provider comparison -> adoptions -> exact form preparation -> local fill; **submission counter 0**, and still 0 after Stop. Green after S5 and after the audit.

## 5. Validation (after the audit fixes)

* `uv run mypy`: clean (338 files).
* `uv run pytest -m "not browser and not desktop_uia"` (known `test_booking_routes_without_a_worker_answer_503` environment baseline deselected): **2491 passed**.
* Browser regressions (M7 research; M8a authenticated read + acceptance; M8b form observation/planning/preparation and local draft; booking; M3 lost-response hard kills; booking preparation; M6 acceptance; M10 downloads; M10 combined workflow; Acceptance F): **198 passed, 6 skipped** (environment-gated), 0 failed.
* `pytest -m desktop_uia` (after S5; the audit fixes touch no desktop code): 47 passed, 1 skipped, 1 failed -- the known environment baseline `test_the_fixture_appears_with_an_opaque_ref_and_no_native_identity` (live window titles contain `python.exe` and backslashes).
* `npm.cmd run typecheck`, `npm.cmd run build`: clean.
* `npx vitest run`: 2460 passed, 22 skipped; failing only the three known machine baselines (`real-inference`, `tokenizer-pack`, `accessibility`) plus, in one run made while the Python suite was loading the machine, `realtime.test.ts` (Realtime model selection), which is untouched by M10 and passes 8/8 on its own.
* `npm.cmd run eval`: **118/118**.
* `npm.cmd run package:dir`: **exit 0 -- PASS** (after the audit fixes; the bundle carries S5 and the audit code, migrations through `0020`). This is a change from the S1-S4 ENVIRONMENT-BLOCKED baseline: `unicodedata.pyd` is unchanged and unsigned but now imports, i.e. the Windows Application Control block no longer reproduces on this machine. Lumi did not weaken WDAC and nothing was self-signed (`Lumi.exe` is `NotSigned`). Production-signed validation remains BLOCKED (no certificate).

## 6. Documented residuals (carried into any later milestone)

1. Only the fixture declares authoritative booking absence; real sites stay `OUTCOME_UNKNOWN` until found, and bookings have no manual settlement path.
2. An unresolved global-tier effect blocks every keyed action on every executor (up to a day with an exhausted lookup budget, or until an unowned project process exits). Deliberately conservative.
3. Placement keys use the root id (re-registration, 8.3 names); the no-overwrite rename still refuses a second file.
4. Desktop focus/scroll/launch, provider disclosures and the frozen local form fill are not keyed (none can repeat a keyed effect).
5. Project code can escape its job through out-of-process brokers and read same-user process memory; the user-level warning is the control.
6. Generic task cancel leaves the newer M10 grants ACTIVE (fenced by task status; not reachable from main).
7. S1: the extraction helper has no network sandbox; main's dropped-file registration is trusted; the listing has a benign TOCTOU. S2: no sandbox against a same-user process inside the quarantine; a placed file keeps the quarantine's ACL; same-volume only. S3: npm runs script text through `cmd.exe` (hashed and shown); a same-user rewrite of `package.json` after spawn; an access-denied liveness check stays `OUTCOME_UNKNOWN`. S4: the planner's "saved details" wording; the placed file is not locked after import.
8. Production certificate NOT CONFIGURED; production-signed validation BLOCKED; real-account release BLOCKED. M10 changes none of these.
