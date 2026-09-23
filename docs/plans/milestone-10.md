# Milestone 10: bounded cross-app tasks and project recipes

> **Cross-app orchestration is not cross-app authority.** A task may compose several separately
> authorized steps. It never gains arbitrary filesystem, shell, browser, desktop or provider
> authority because several systems take part.

Status: **S1-S5 engineering complete and the final cross-slice audit complete** (reviews: `docs/reviews/milestone-10-s1.md` ... `-s5.md`; final audit: `docs/reviews/milestone-10-final.md`). M10 started from `eda1620` (M9 merged to `main`). It is delivered as five
sequential security slices. Each slice gets its own implementation, adversarial review, validation,
documentation and commits. If a hard requirement cannot be met, M10 stops at the last completed
slice. No invariant is weakened to finish.

```text
M10 - bounded cross-app tasks and project recipes

S1  approved documents + file broker          read / extract / compare. No file mutation.
S2  controlled downloads + file placement      quarantine first, then atomic no-overwrite placement
S3  registered project recipes                 start / status / stop of a user-registered recipe
S4  cross-app preparation workflow             download -> inspect -> compare -> prepare -> zero submit
S5  cross-executor recovery                    one effect registry + booking crash recovery (acceptance F)
```

This plan refines the M10 entry in `docs/plans/general-computer-use-architecture.md` (sections I, J
and L). It does not redesign M1-M9.

## Where M10 runs, and why that differs from the architecture sketch

The architecture sketch puts file and recipe brokers in Electron main. After reading the code, this
plan puts them in the **Python durable runtime**, with main as the trusted-UI and provider boundary:

| Concern | Lives in | Reason |
| --- | --- | --- |
| Action ledger, effect locks, recovery | Runtime (PostgreSQL) | Already there. A file effect and its lock must commit in one transaction. |
| File roots, file identity, reads, placement | Runtime | Placement moves a file out of a quarantine the runtime owns. Keeping it next to the ledger lets one transaction bind an action to the file identity. |
| Document extraction | Runtime-launched **helper subprocess** | The helper receives bytes on stdin, never a path. It has a stripped environment, a job with memory and time limits, and no network use. |
| Downloads | Browser worker, into a runtime-configured quarantine | The browser worker is Python. Its reviewed network guard fetches the approved URL itself. |
| Project runs | Runtime | Windows Job Objects already exist here through `ctypes` (`windows_job.py`). Node has no Job API without a native dependency. |
| Trusted cards, native folder picker, provider calls | Electron main | Unchanged: provider keys and trusted UI stay in main. |

Main still never passes a raw path from the renderer. Absolute paths reach the runtime only from a
native dialog or the dropped-file store in main, both main-owned. The runtime never returns an
absolute path on any route.

## Authority composition (applies to every slice)

Each authority is separate. None implies another:

```text
file root READ     =/=  file root CREATE   =/=  MODIFY (represented, unused in M10)
download grant     =/=  file read          =/=  provider disclosure  =/=  form fill  =/=  submit
project registration =/= run approval      =/=  VS Code launch       =/=  dependency install / Git
```

There is **no** `computer_use_grant`. Each new authority is a narrow grant kind or tool on the
existing ledger: `task_grants` for reusable scope, exact single-use approvals for effects,
`step_authorizations` for steps inside a confirmed scope.

## Cross-executor effect locks (built in S2, completed in S5)

There is no separate lock database. A small ledger-side table, `action_effect_keys (action_id,
effect_key, effect_kind)`, is written in the same transaction that inserts an effect-bearing action.
A closed registry (`app/domain/effects.py`) derives the keys from the controller-built proposal.
Keys never come from a model or the renderer.

Before any attempt of a keyed action is created, the ledger takes transaction-scoped advisory locks
on the action's keys, in sorted order. It then refuses (`effect_locked`) if another action holding
any of those keys is `EXECUTING`, `OUTCOME_UNKNOWN` or `RECONCILING`. The check happens in every
task and on every executor. Two tiers:

* **Exact keys**, for material equivalence. Examples: `file:create:{rootId}:{casefolded name}`,
  `transfer:source:{digest of the canonical source URL}`, `project:{projectId}:run` and
  `booking:{site}`.
* **Global tier.** An unresolved effect of kind `external_mutation` (booking) or `project_run`
  blocks every new keyed consequential action on every executor until it is reconciled. The
  architecture's rule says: "Initially block all new consequential work while any such action is
  unresolved." Stop and reconciliation are never blocked.

Reconciliation stays read-only and is described per effect kind by the registry: correlation keys,
evidence source, allowed read operations, and whether absence is authoritative. A model can extract
candidate evidence. It never decides success, retry safety or authoritative absence.

## S1 - approved documents + file broker (read / extract / compare)

**Adds**
* `file_roots`: a runtime-owned M10 root, registered only through a trusted main flow (native folder
  dialog plus a trusted card). It has explicit booleans `can_read`, `can_create`, `can_modify`.
  `can_modify` must be false in M10. The root is bound to its canonical path, volume serial and
  directory file id, and a replaced root directory is `root_changed`. Legacy search roots (main's
  `lifelens-state.json`) are unchanged: search approval does **not** imply M10 read.
* `file_refs`: task-owned opaque file authority, of two sources:
  * `ROOT_FILE`: a root id plus a validated relative path, chosen from a bounded listing of a READ
    root;
  * `DROPPED_FILE`: exactly that file, handed over from main's dropped-file store. It never grants
    the parent directory, siblings or recursion.
  Every ref binds volume serial, file index, size, `mtime_ns` and SHA-256.
* `documents`: bounded extracted text, private and `untrusted_environment`, task-owned, with 24 h
  retention.
* Extraction helper (`python -m app.documents.helper`): text (UTF-8/UTF-16 with BOM), DOCX (stdlib
  `zipfile` and `ElementTree`), and PDF (a reviewed stdlib subset, see below). It runs with bounds
  on file size, pages, text bytes, time, zip members and expansion. A DOCTYPE or entity in XML is
  refused. Macro-enabled OOXML is refused. External relationships are counted and never fetched.
  Encrypted PDFs are refused. Nothing is executed, fetched or opened.
* Local structural compare of two documents (terms shared / only in A / only in B, sizes, headings).
* Optional provider compare: a `document_disclose` grant card names both documents, the exact
  bounded projection (fields and byte counts), the recipient, the model and the purpose. It allows
  one provider attempt with no failover (new private task class `document_compare`). The result is
  a closed schema whose every claim is grounded in a quote from the projection. The pattern follows
  M9 S2's claim-then-call ordering.

**Path safety.** Relative names are validated before any filesystem call. Refused: `..`, absolute
paths, drive letters, UNC, `\\?\` / `\\.\` device paths, `:` (ADS), reserved device names (with any
extension), trailing dot or space, control characters, and over-long components. Reads open the
file, then prove from the **open handle** that it is the right file:
* `GetFinalPathNameByHandleW` must lie under the root's current canonical path;
* `fstat` volume and index must equal the ref's;
* no reparse point on the file or any component from the root down (`lstat` walk with
  `FILE_ATTRIBUTE_REPARSE_POINT`).
Residual TOCTOU is documented: this is handle-verified reads, not an OS sandbox.

**PDF subset.** Classic xref and object streams; FlateDecode (bounded `decompressobj`); literal and
hex strings; `Tj` / `TJ` / `'` / `"`; simple line breaks from `Td` / `T*` / `Tm`; `ToUnicode`
`bfchar` / `bfrange`. Anything else is `unsupported_pdf` rather than a guess. There is no JavaScript,
no actions, no embedded files and no images.

**Excluded in S1:** copy, move, rename, overwrite, delete, download placement, any write.

## S2 - controlled downloads + file placement

* `file_transfer` grant (trusted card). It binds:
  * the source task and source session (`public_ephemeral`, a fresh context under the existing
    public/test-origin policy);
  * the exact source URL and origin;
  * a user-stated intent label;
  * the destination root (CREATE required) and a validated filename;
  * a size limit and allowed types (`pdf`, `docx`, `txt`);
  * `overwrite: false` and an expiry.
* Two steps, each its own action funded by a single-use step authorization from that grant:
  1. `transfer_download`: the worker's reviewed guard fetches the approved URL. There is no Chromium
     download manager and no `accept_downloads`. The worker writes into a **quarantine** it derives
     itself: `<quarantine_root>/<transferId>/`. It writes a `started` marker **before** any network
     request, streams to `payload.part`, then renames to `payload.bin` and writes a completion
     manifest (length, SHA-256). The quarantine is Lumi-owned, extension-less, never opened, never
     executed and never provider-readable by path. It is swept after 24 h unless placed.
  2. `transfer_place`: sniff the type by signature (never extension alone) and refuse executables,
     scripts, shortcuts and macro-enabled Office documents. Then atomic **no-overwrite** rename into
     the destination, relative to a held, verified parent-directory handle
     (`SetFileInformationByHandle(FileRenameInfo, RootDirectory=parent, ReplaceIfExists=FALSE)`).
     Cross-volume placement is refused (deferred). Mark-of-the-Web (`Zone.Identifier`, ZoneId 3,
     HostUrl) is written in quarantine and preserved by the same-volume rename.
* `file_transfers` manifest: safe metadata only, never an absolute path on any route or event.
* Recovery inspects evidence and never re-downloads:
  * **Download.** No `started` marker is authoritative no-effect (`FAILED`). A marker without a
    verified completion manifest stays `OUTCOME_UNKNOWN`. A verified manifest is `SUCCEEDED`.
  * **Placement.** Rename is atomic and preserves the file index. A destination holding the
    quarantined file's index is `SUCCEEDED`. The quarantine still holding it with no destination is
    `FAILED`. Anything else is `OUTCOME_UNKNOWN`.
* Effect locks start here: `transfer:source:*` and `file:create:*`.

## S3 - registered project recipes

* `projects`: a project root separate from file roots. It is registered through the native folder
  picker plus a trusted card that says: **"This recipe executes code from this project with your user-level permissions."**
* `project_recipes`, created only in trusted UI. It binds a revision and digest over:
  * the executable, `node.exe`, under an administrator-writable install root, with its file identity;
  * the fixed `npm-cli.js` and argument vector `[npm-cli.js, "run", <script>]`;
  * cwd equal to the project root;
  * the env allowlist (fixed values, secret-looking names refused);
  * `package.json` and lockfile hashes and the script text;
  * readiness (`http` port and path, or `exit_code`);
  * timeout and stop policy.
  Any change invalidates the recipe (`recipe_changed`) and requires trusted re-registration. A model
  may only pick a `recipeId` from the registered set.
* **Start** is an exact per-run approval: tool `project_start`, R3, with the warning shown again.
  1. Pre-check: hashes are current and dependencies are present (`node_modules` plus the script's
     binary). A missing dependency is `BLOCKED: missing_dependency`, with no install.
  2. `project_runs` row plus the attempt are committed.
  3. Spawn with `CREATE_SUSPENDED` and no shell, into a per-run Job Object (kill-on-close, **no**
     breakaway).
  4. Resume.
  5. Record pid and creation time.
* **Status** means the owned root is alive, the job's active process count, a bounded log tail
  (untrusted), and readiness. HTTP readiness also requires the listening socket's owning PID to be
  **in the run's job** (`GetExtendedTcpTable` plus `IsProcessInJob`). A foreign listener on the port
  is not the run.
* **Stop** is `TerminateJobObject` on that run's job only. It never kills by image name or bare PID.
* A run cannot outlive the runtime: the run job's only handle is the runtime's. After a restart,
  recovery proves the recorded (pid, creation time) is gone and marks the run `ENDED_WITH_RUNTIME`.
  If it is still alive, the run is `OUTCOME_UNKNOWN` and the project stays locked. No duplicate
  server can start.
* The environment is built from scratch. It never contains provider keys, `LUMI_*`,
  `DATABASE_URL`, cloud or Git credentials. Tests plant fake secrets and prove the child cannot see
  them.
* No `cmd`, PowerShell, `shell=True`, `.cmd`, terminal typing, `npm install`, `git`, or
  `package.json` / lockfile edits. Source scanners pin the single `Popen` call site and its argv
  shape.
* **Acceptance C** ("Open VS Code and start Lumi") is three separate effects: registered VS Code
  launch (M9 S3, unchanged), the approved project, and exact recipe execution. On this machine VS
  Code is a per-user install, which M9's registry correctly refuses. The launch half is exercised on
  the registered-launch path with fakes and a system application. The rule is not weakened.

## S4 - cross-app preparation workflow

A deterministic controller composes existing bounded capabilities: download → placement → extraction
→ compare → optional provider disclosure → candidate fields → trusted adoption → authenticated form
preparation (M8 S4-S6) → **stop before submit**. There is no new planner holding every tool.

* **Lineage.** One `workflows` row owns controller-created child tasks by role (`workflow_steps`).
  A document, transfer, disclosure, adopted value or browser session can only be consumed by a task
  in the same workflow, in the role that produced it.
* **Provenance.** Candidates are `document_extracted` (a local span) or `provider_derived` (must
  quote the disclosed projection). Neither is ever trusted automatically. Adopting one is an exact
  approval that creates a **workflow-scoped** protected value that remembers its source. The global,
  user-typed saved details from M8 are unchanged. M8's form planning reads the workflow-scoped
  values through the same digest-bound manifest.
* **Recipient binding.** Disclosure to a provider and a form's receiving origin are separate
  approvals. Neither implies the other.
* **Not added:** file upload, submit, Enter, or generic click. M8's freeze, protected-value firewall
  and exact approvals are reused unchanged. Expected submissions: **0**.

## S5 - cross-executor recovery and the consequential test action

* The effect registry gets its final shape: booking (`external_mutation`, lookup by reference,
  authoritative absence per site), transfers, placement, project runs and desktop mutations.
* The generic `POST /tasks/{id}/actions` route can no longer propose a registered effect tool. It
  was a same-task bypass of booking exclusivity.
* **Acceptance F:**
  1. Perform the fixture booking.
  2. The fixture confirms it.
  3. The response is suppressed and the runtime and worker are killed.
  4. After restart: one action, one dispatch, one effect, `OUTCOME_UNKNOWN`.
  5. Read-only reconcile: `FOUND` gives `SUCCEEDED`; authoritative absence gives `FAILED`;
     non-authoritative "not found" stays `OUTCOME_UNKNOWN`.
  6. While unresolved, the same task, a new task, the browser route, the desktop mutation route, a
     project recipe, the file broker, another provider and another model are all refused. (As built,
     S5: desktop focus/scroll/launch, read-only work and the frozen local form fill are not keyed; none
     can repeat a keyed effect. See `docs/reviews/milestone-10-s5.md`.)
* **Stop/cancel** revokes future dispatch, stops owned runs, and preserves evidence. It never marks
  an in-flight effect failed and never compensates.
* **Final audit.** Two independent Claude passes over `eda1620..HEAD`.

## Hard no-go list (M10 never closes if any path enables)

Arbitrary shell, PowerShell or cmd authority, terminal typing, and model-authored commands,
executables or arguments. Dependency installation and Git mutation. Arbitrary filesystem writes,
root escape, overwrite, recursive or permanent delete, and executing or auto-opening downloaded or
macro content. Private document leakage. Cross-task file, provider or browser authority. Duplicate
project runs or external effects after uncertainty. Effect-lock bypass through another executor.
Generic upload or submit / send / purchase.

## Validation per slice

Targeted tests per slice. At the end of M10: `uv run mypy`, `uv run pytest -m "not browser"`,
`pytest -m desktop_uia`, the relevant browser suites (M7 research, M8 authenticated read, M8 form
preparation, booking), `npm.cmd run typecheck`, `npx vitest run`, `npm.cmd run build`,
`npm.cmd run eval` and `npm.cmd run package:dir`. On this machine `package:dir` is expected to be
**ENVIRONMENT-BLOCKED** by Windows Application Control (`unicodedata.pyd`); it is reported as
exactly that, never as a pass. Production signing remains **NOT CONFIGURED** and real-account
release **BLOCKED**. M10 changes neither.
