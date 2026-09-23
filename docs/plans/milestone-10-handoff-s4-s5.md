# Milestone 10 handoff: S4 and S5 (for the next Claude session)

S1–S4 are **committed and pushed** on `lumi-m10` (S4: `docs/reviews/milestone-10-s4.md`, migration `0020`; S5 starts at `0021`). S5 is **not started**; §5 below is kept as the record of the S4 brief. Do not merge to `main`, and do not rewrite M9 history. Stop after M10; there is no M11.

Read these first, in order:

1. `AGENTS.md` (repo rules).
2. `docs/plans/milestone-10.md` (the plan; S4 at "S4 - cross-app preparation workflow", S5 after it).
3. `docs/reviews/milestone-10-s1.md`, `-s2.md` and `-s3.md` (what exists, what each review fixed, and the residuals).
4. `docs/SECURITY.md`, the sections "Approved documents…", "Controlled downloads…" and "Registered project recipes…".

## 1. State of the branch

```text
eda1620  M9 final (= main)                                    <- M10 audit range starts here
2f8a491  docs: plan milestone 10 bounded cross-app workflows
3f7e32a  feat(agent): … document and file broker (M10 S1)      0fdcd5f docs: close milestone 10 S1
5b2f6a1  feat(agent): … controlled download and file placement (M10 S2)   542929e docs: close milestone 10 S2
(S3)     feat(agent): add registered project recipes (M10 S3) + docs: close milestone 10 S3
```

**Migrations:**

* `0017`: documents and file broker;
* `0018`: transfers and `action_effect_keys`;
* `0019`: projects, recipes and runs;
* `0020`: workflows, steps, candidates and workflow values (S4).

Head is `0020`. The next is `0021`.

## 2. The process each slice followed (keep it)

For each slice: implement → a **fresh** Claude subagent adversarial review → the lead verifies every finding and fixes it (or documents it with a reason) → validation → docs → two commits → push:

1. `feat(agent): … (M10 Sx)` (code and tests);
2. `docs: close milestone 10 Sx` (`docs/reviews/milestone-10-sx.md`, plus SECURITY, AGENT-RUNTIME and STATUS).

Keep the model and subagent policy Claude-only. The lead owns security decisions and commits.

**Instructions for the review subagent:**

* read-only;
* **do not run pytest, eval or anything touching PostgreSQL**, because the test suites share ONE database; see the memory note "Test suites share one database";
* give it the brief from the assignment for that slice.

## 3. Environment gotchas (learned the hard way)

* **Shell scripting:**
  * Long bash heredocs and `python - <<'EOF'` blocks **mangle backslash escapes** (`\n`, `\\`) in this environment. Write patch scripts with the Write tool into the scratchpad and run them with `python <file>`, or use the Edit tool.
  * Never `cat > /dev/null` or read stdin in Bash. It hangs.
* **Test suites and the database:** eval, pytest and Electron acceptance must run **one at a time** (one DB). Run long suites with `run_in_background`.
* **Validation commands:**
  * Python (`services/agent`):
    * `uv run mypy`;
    * `uv run pytest -m "not browser and not desktop_uia" -q -p no:cacheprovider --deselect tests/test_booking_preparation.py::test_booking_routes_without_a_worker_answer_503` (a known environment baseline);
    * `uv run pytest -m browser <files>` for browser suites.
  * TypeScript (repo root):
    * `npm.cmd run typecheck`;
    * `npx vitest run` (known baselines: `real-inference`, `tokenizer-pack`, `accessibility`);
    * `npm.cmd run build`.
  * The final matrix also includes `npm.cmd run eval`, `pytest -m desktop_uia` and `npm.cmd run package:dir`.
* **Known environment failures:**
  * `pytest -m desktop_uia`: `test_the_fixture_appears…` fails because live desktop window titles contain `python.exe` or backslashes.
  * `npm.cmd run package:dir`: blocked by Windows Application Control (`unicodedata.pyd`). Report it as **ENVIRONMENT-BLOCKED**, never PASS. Do not weaken WDAC or self-sign.
* **Contract regeneration:**
  * After changing `TaskEventType` or error codes, regenerate the contract: `cd services/agent && uv run python -m app.api.contract --write`. That writes `src/shared/agent-runtime-contract.json`.
  * Also add the event to `TASK_EVENT_TYPES` in `src/shared/agent-contracts.ts` and to the timeline strings in `src/renderer/src/agent-task-view.ts`, or vitest's enum-contract test fails.
* **New tables:** update `TRUNCATE_ALL` in `tests/conftest.py`. Add an `M10_S5_TABLES` tuple (S4 added `M10_S4_TABLES`) and add it to the `without=` lists in `test_migration_0013/0015/0017/0018/0019/0020`.
* **Changing an uncommitted migration:** the test database is already stamped at head, so truncate, downgrade one revision and migrate again before re-running tests. `test_schema.py` compares metadata with the migrated database, so tables and migration must match exactly.
* **Packaging:** add each new migration and module to the presence checks in `scripts/build-agent-runtime.mjs`.
* **Source scanners:**
  * `tests/desktop_source_scan.py`: the word "breakaway" anywhere outside two allowed files fails.
  * `tests/test_documents_source.py`: only `quarantine.py` and `place.py` may write files, and only `sniff` may be imported by the transfer modules.
  * `tests/test_projects_source.py`: one `Popen`, no shell.

## 4. Building blocks S4 and S5 compose (do not re-implement)

| Capability | Where | Notes |
| --- | --- | --- |
| Documents: roots, add file, extract, local compare, ONE provider disclosure | `app/services/documents.py`, `src/main/services/document-controller.ts` | Private class `document_compare`: one recipient, no failover |
| Download to quarantine → placement | `app/services/transfers.py`, `transfer-controller.ts` | `file_transfer` grant; `type_mismatch`; tombstone reconciliation |
| Effect lock | `app/domain/effects.py`, `app/repositories/effects.py`, `ActionService.start_scoped_attempt(effect_keys=…)` | `GLOBAL_TIER = {external_mutation, project_run}`; `RECONCILIATION_REGISTRY` currently covers download and file_create only |
| Project runs | `app/services/projects.py` | global-tier `project_run` key |
| Authenticated form observation and preparation, protected values (M8 S4–S6) | `app/services/form_prepare*.py`, `app/domain/protected_values.py`, `app/api/routes.py` (`_refuse_disclosure_tool`) | Reuse **without weakening**: freeze, firewall, exact approvals |
| Booking fixture (commit, lookup) | `app/browser/operations/…`, `evals/sites/appointments`, `lookup_booking` read-only reconciliation | For S5 Acceptance F |
| Generic action routes | `app/api/routes.py` | Already refuse desktop, form, transfer and project tools. S5 must also stop `POST /tasks/{id}/actions` proposing ANY registered effect tool (for example the booking commit), a same-task bypass the plan names |

## 5. S4: cross-app preparation workflow (to do)

From the assignment and the plan: a **deterministic controller** (no new planner holding every tool) composes the following steps:

1. download (S2) → placement (S2);
2. extraction and compare (S1);
3. optional provider disclosure (S1);
4. candidate fields → trusted adoption into a **workflow-scoped** protected value that remembers its provenance (`document_extracted` or `provider_derived`, which must quote the disclosed projection);
5. authenticated form observation → exact protected-value/form preparation (M8 S4–S6);
6. **STOP BEFORE SUBMIT**.

**Requirements:**

* **Lineage:** a `workflows` table plus `workflow_steps` (child tasks by role). A document, transfer, disclosure, adopted value or session can be consumed only within the same workflow, in the role that produced it.
* **Recipients:** provider disclosure and the form's receiving origin are **separate approvals**; neither implies the other.
* **Not added:** no upload, no submit or Enter, no generic click. M8's freeze and firewall are unchanged.
* **Acceptance:** a combined synthetic acceptance with **submission count = 0** (use the appointment/account fixtures; assert the fixture's submission counter).
* **Review brief:** attack cross-task/workflow authority reuse, provenance laundering (a provider-derived value adopted as user-typed), the recipient binding, and submit via any path.

## 6. S5: cross-executor recovery and consequential test action (to do)

* **Effect registry:** give it its final closed shape — booking (`external_mutation`; lookup by reference; authoritative absence per site), transfers, placement, project runs, desktop mutations (key M9 S4 effects as `desktop_mutation` so they join the lock).
* **Generic route:** block the generic proposal route for registered effect tools.
* **Acceptance F:**
  1. Run the fixture booking; the fixture confirms it.
  2. Suppress the response and kill the runtime and worker.
  3. After restart there is exactly one action, one dispatch and one effect, in state OUTCOME_UNKNOWN.
  4. Read-only reconcile: FOUND gives SUCCEEDED; authoritative absence gives FAILED; non-authoritative gives OUTCOME_UNKNOWN.
  5. While it is unresolved, each of the following is refused: same task, new task, browser route, desktop route, project recipe, file broker, another provider, another model.
* **Cancellation:** stop or cancel revokes future dispatch, stops owned runs and keeps the evidence. It never marks an in-flight effect failed and never compensates.
* **Final audit:** two independent Claude audit passes over `eda1620..HEAD`. The lead verifies each finding. Write `docs/reviews/milestone-10-s5.md` and `docs/reviews/milestone-10-final.md`.
* **Final docs:** update `SECURITY.md`, `AGENT-RUNTIME.md`, `PACKAGING.md`, `STATUS.md` and `README.md`.
* **Final validation matrix:** run the whole matrix in §3, including `npm.cmd run eval` and the relevant browser regressions (M7 research, M8 authenticated read, M8 form preparation, booking, `test_download_worker_browser`). Report `package:dir` honestly.
* **Final report:** in the assignment's format.

## 7. Open residuals carried forward (documented, not bugs to fix silently)

* **S1:**
  * the helper has no network sandbox;
  * `registerDroppedFile` trusts main's dropped-file registration;
  * the listing has a benign TOCTOU.
* **S2:**
  * no sandbox against a same-user process inside the quarantine;
  * a placed file keeps the quarantine's ACL;
  * placement is same-volume only.
* **S3:**
  * npm runs script text through `cmd.exe` (the text is hashed and shown);
  * a same-user rewrite of `package.json` after the spawn;
  * an access-denied liveness check stays OUTCOME_UNKNOWN (fail-closed);
  * the overlap check is not serialised across two concurrent native dialogs.
* **Release state:** production certificate NOT CONFIGURED; real-account release BLOCKED; M10 changes neither.
