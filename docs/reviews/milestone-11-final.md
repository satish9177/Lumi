# Milestone 11: final cross-slice review

> **General orchestration is not general authority.** Across five slices Lumi gained a closed capability
> catalog, a durable read-only orchestration graph, approved composition of one effectful capability
> (`project_start`), a unified task cockpit with pause/resume, and a generality eval suite. No capability
> executes because an intent model named it; every M1-M10 approval, grant, disclosure boundary and effect
> lock a composed capability already carried stays exactly as strict when reached through the orchestrator.

```text
Baseline               045c036809093fb3367d06c24392dbc05dc4a40d (M10, = main)
Audited head            826638aae677169223a981334bba3f23d0618a8d (before this closure pass)
Migration head          0022
```

```text
M11 S1 general routing/catalog         COMPLETE
M11 S2 durable read-only orchestration COMPLETE
M11 S3 approved capability composition COMPLETE
M11 S4 cockpit/pause/resume            COMPLETE
M11 S5 evals/final audit               COMPLETE

M11 engineering implementation         COMPLETE
M11 final cross-slice audit            COMPLETE

main                                   NOT MERGED
```

## 1. What each slice added

Per-slice detail lives in the individual reviews; this document summarizes and points to them rather than
repeating their content.

| Slice | Review | Adds | Migration |
| --- | --- | --- | --- |
| S1 | [milestone-11-s1.md](milestone-11-s1.md) | Closed capability catalog + general request-routing vocabulary. TypeScript-only; no capability executes because it was named. | none |
| S2 | [milestone-11-s2.md](milestone-11-s2.md) | Durable orchestration graph (`orchestrations` / `orchestration_steps`); links `public_research` through it behind its own existing trusted-scope approval. | `0021` |
| S3 | [milestone-11-s3.md](milestone-11-s3.md) | Composes `project_status` and `project_start`; `project_start` still opens the same R3 execution warning, effect key and cross-executor lock a direct request would. | none (composes over S2's graph) |
| S4 | [milestone-11-s4.md](milestone-11-s4.md) | Unified task cockpit with exactly two actions (Continue, Stop), `outcome_unknown` handling, schema-only `manual_handoff_required`. | `0022` |
| S5 | [milestone-11-s5.md](milestone-11-s5.md) | 20-case generality eval suite (139/139 total); two independent final cross-slice audit passes; one confirmed resume/expiry bug, fixed. | none |

Each slice review recorded **no reportable-severity findings** in its own scoped review (S1: none, two
addressed informational notes; S2: none, two informational notes; S3: none, one non-blocking observation;
S4: none, all seven checked-and-clear categories documented in that review). The findings below are from the
final cross-slice audit, which reviews all five slices together rather than one at a time.

## 2. Final cross-slice audit (two fresh, independent Claude passes; no external model)

Both passes were read-only over the complete `git diff 045c036..lumi-m11` diff (base commit before M11
through the audited tip), matching the M9/M10 audit process exactly. Full detail: [milestone-11-s5.md](milestone-11-s5.md#final-cross-slice-audit).

**Pass A -- authority, privacy/data lineage, provider routing, prompt injection, resume.**
**No findings in scope.** Traced all three composed capabilities end to end and confirmed no aggregation, no
skipped approval, no new authority at any hop, including under the cockpit's Continue/Stop and under a race
(every mutating write revision-fenced under `SELECT ... FOR UPDATE`). Confirmed the planner sees only
controller-authored bounded summaries, never raw private content. One forward-looking, non-blocking note:
nothing today structurally *ties* `orchestration_planning`'s private-class status to the composed set, so
whichever future slice composes a privacy-sensitive capability (`account_read`/`desktop_reason`) must
explicitly move that task class into `PRIVATE_TASK_CLASSES` -- a precondition to capture in that slice's
plan, not a finding against this one. One dead-code note (`agentCapabilityLines()` unused, fail-safe).

**Pass B -- scheduler, recovery, loops, budgets, effect-lock, races, restart, stale state, generic-route
bypass.**
**One confirmed bug, reproduced against a real Postgres database:** `OrchestrationService.resume()` did not
check orchestration liveness/expiry before reviving a `PAUSED` orchestration, unlike every other write path.
A `PAUSED` orchestration whose 30-minute TTL had elapsed while waiting on the user could be revived to
`RUNNING`, after which the next call would immediately refuse `orchestration_expired`, leaving it stuck in
an undocumented dead `RUNNING` state reachable only by Stop. Not an authority or effect-lock bug -- no
capability executed wrongly, no budget widened -- but a real robustness gap.

**Fixed** by `853450d fix(agent): resume() must not revive an expired orchestration (M11 S5)`:
`resume()` now checks `repository.is_live(orchestration_id)` at entry (fail-fast, before any capability
read) and again inside the final locked write immediately before `resume_running()`, since the capability
reads between the two are real I/O that could themselves cross the TTL boundary. New regression test:
`test_resume_refuses_once_the_orchestration_has_expired_rather_than_reviving_it`.

No other Pass B finding: effect-lock/races/duplicate-effects, restart/in-memory-state safety,
`outcome_unknown` honesty and generic-route closure all checked out clean under direct code tracing. One
non-reportable dead-code note (`OrchestrationRepository.fail()` has zero callers).

**Rejected findings:** none. Every finding raised was confirmed; the fixed one is fixed, the rest are
documented as non-blocking notes above.

## 3. Documented residuals (carried into any later milestone)

1. Only 3 of 16 catalog capabilities are composed (`public_research`, `project_status`, `project_start`);
   the other 13 are real-but-uncomposed and refuse honestly (`capability_unavailable`), never executed or
   guessed at.
2. A trusted available-refs mechanism is still required before document, account or desktop capabilities
   can be composed -- today's three composed capabilities need no such mechanism, but the next one likely
   will.
3. `manual_handoff_required` exists in the schema (migration `0022`) but is schema-only: reachable by no
   composed capability, so its count is honestly 0, not simulated.
4. Stop stops future scheduling only; it deliberately does not cascade into a linked child task's own
   cancellation path (a narrower, more conservative reading of the plan's Stop line, consistent with M10
   S5's own established Stop rule).
5. The loop guard relies on bounded budgets (`MAX_PLANNER_CALLS` and friends) rather than stronger
   "three non-progressing steps" A-B-A loop detection; a `FAILED` step can still be retried as a fresh step
   with a fresh approval. Acceptable to ship given budgets bound the worst case and the closed-enum planner
   has no paraphrase-evasion surface (S2/S4 already documented this; the final audit re-confirmed it).
6. Future privacy-sensitive composition (`account_read`/`desktop_reason`) must make `orchestration_planning`
   private (add it to `PRIVATE_TASK_CLASSES`) before any private summary or content is exposed through the
   orchestrator -- nothing today structurally enforces this automatically.
7. Production certificate NOT CONFIGURED; production-signed validation BLOCKED; real-account release
   BLOCKED. M11 changes none of these.

## 4. Validation

Carried over unchanged from [milestone-11-s5.md](milestone-11-s5.md#validation) (this closure pass changed
only docs and validation runs, no application/config/package-script source):

* `uv run mypy app`: clean (179 source files).
* `uv run pytest -m "not browser and not desktop_uia"`: 2,535 passed; the only failure is the pre-existing,
  documented environment baseline (`test_booking_routes_without_a_worker_answer_503`), unrelated to this
  diff.
* `npm.cmd run typecheck`: clean.
* `npx vitest run`: 2,552 passed, 22 skipped; the only failing test files are pre-existing, documented,
  machine-specific baselines, unrelated to this diff.
* `npm.cmd run build`: main/preload/renderer all build clean.
* `node scripts/run-evals.mjs`: 139/139 eval cases pass.

### Suites run for the first time in this closure pass (S5 recorded these as "not re-run")

* **`pytest -m desktop_uia`**: **49 passed**, 0 skipped, 0 failed (111.66s). M11 touched no desktop-UIA
  code, and this run reproduces no failure at all -- better than the M10-final baseline (47 passed, 1
  skipped, 1 failed on the known `test_the_fixture_appears_with_an_opaque_ref_and_no_native_identity`
  window-title baseline), which did not reproduce here.
* **Browser regressions** (targeted at the M11-relevant surfaces: M7 public research, M8 authenticated
  read, M8 form preparation/local draft, M10 downloads/workflow, booking/recovery --
  `test_acceptance_f_booking_recovery.py`, `test_authenticated_service_browser.py`,
  `test_booking_preparation.py`, `test_browser_booking.py`, `test_download_worker_browser.py`,
  `test_form_draft_browser.py`, `test_form_prepare_browser.py`, `test_local_form_draft_browser.py`,
  `test_research_browser.py`, `test_workflow_acceptance_browser.py`): **133 passed**, 23 deselected
  (environment-gated), 0 failed. A first attempt run concurrently with a `package:dir` build showed 1
  failed + 19 errors, all `SystemExit: 3` from a test-fixture uvicorn server failing to bind its
  `free_port()`-selected port under that resource contention; a clean isolated re-run reproduced none of
  it, confirming it was contention noise, not an M11 regression. Orchestrated `public_research` (M11 S2)
  exercises the same `test_research_browser.py` path as direct research and shows no regression.
* **`npm.cmd run package:dir`**: **PASS (exit 0)**, after one environment fix -- see below. `Lumi.exe`
  remains `Status: NotSigned` (confirmed via `Get-AuthenticodeSignature`); nothing was self-signed and no
  Windows policy was weakened.

**Environment fix required to get `package:dir` green (no source/config/package-script change):**
`services/agent/.venv` on this machine had been created against the system-installed Python 3.13
(`C:\...\Programs\Python\Python313`) rather than the project's required uv-managed standalone CPython 3.12
(`requires-python = ">=3.12"`, pinned by `services/agent/pyproject.toml`'s mypy config to 3.12). The
packaging script (`scripts/build-agent-runtime.mjs`) copies whatever interpreter `.venv/pyvenv.cfg` points
at verbatim into the bundle and then byte-compiles its entire `Lib/` tree; the system 3.13 install's copy of
`Lib/test/` carries CPython's own deliberately-malformed parser fixtures (`bad_coding.py`,
`badsyntax_pep3120.py`, etc.), which `python -m compileall` fails on by design, making `-j 0 compileall`
exit 1 with the real per-file errors swallowed (the script pipes `stdout` and only prints it on success). A
plain `uv sync --python 3.12 --frozen` in `services/agent` rebuilt `.venv` against the already-available
uv-managed `cpython-3.12.14-windows-x86_64-none` (`C:\Users\SATISH\AppData\Roaming\uv\python\...`), after
which byte-compiling and packaging both went clean. `.venv` is gitignored; no repository file changed to
fix this. Whether the same local-interpreter drift would reproduce on a clean machine following the
project's documented `uv sync` setup is untested but expected not to, since a fresh clone with no prior
system-Python venv would resolve to the pinned managed 3.12 by default.

## 5. Production status

Unchanged by M11:

```text
Production certificate            NOT CONFIGURED
Production-signed validation      BLOCKED
Real-account release              BLOCKED
```

## 6. Merge status

**M11 is not merged to `main`.** Per the milestone's own instruction, `main` is left untouched at
`045c036`; `lumi-m11` carries the complete, audited milestone plus this closure pass.
