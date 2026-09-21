# Milestone 9, S3: trusted focus, semantic scroll and registered-application launch

> **Lumi can now do exactly three things to a Windows desktop, each only after you approve that exact step: bring one window to the front, scroll one list by one closed step, or open one application you registered. It still cannot click, type, select, invoke, drag, use the mouse or keyboard, run a command, or open an arbitrary program or path.**

> This does **not** mean Lumi can operate an application. There is no Invoke, no SetValue, no selection, no coordinate, no screenshot, no vision, no shell and no key. Those are S4/S5 (and some are permanently out of scope).

Status: **S3 implementation closed. S4, S5 and M10 are NOT started.**

```text
M8b engineering                    COMPLETE

Windows signing infrastructure     READY
Production certificate             NOT CONFIGURED
Real-account release               BLOCKED  (no production certificate; unchanged)

M9
  S1 Windows UIA observation       COMPLETE
  S2 Desktop disclosure/reasoning  COMPLETE
  S3 Focus / scroll / app launch   COMPLETE  (engineering; fixture + Character Map on this machine)
  S4 Semantic actions              NOT STARTED
  S5 Visual fallback               NOT STARTED

M10                                NOT STARTED
```

Nothing here used a real account, saved detail or private application, and no provider was called (S3 never talks to a model). Installed production-release validation was **not** performed and remains blocked by the missing Authenticode certificate.

## 1. Starting point

Branch `lumi-agent-v2`, pushed head `94ae3892756b747e08f6976a29d3222d60297eab` (`docs: close milestone 9 S2 desktop disclosure`), clean tree.

## 2. Final SHAs

| Commit | SHA |
| --- | --- |
| Implementation: `feat(agent): add trusted desktop focus and semantic scroll (M9 S3)` | see the final report (a document cannot contain the hash of its own commit) |
| Docs closure: `docs: close milestone 9 S3` | the commit that adds this file |

## 3. Architecture in one paragraph

Model proposes; the controller validates and authorizes; the worker executes ONE bounded effect. S3 adds no parallel ledger. A desktop effect is an `actions` row (tools `DESKTOP_FOCUS`, `DESKTOP_SCROLL`, `DESKTOP_LAUNCH`; risk R1/R1/R2) with an exact `approvals` row, an `action_attempts` row and a new `desktop_dispatches` row. The order is the browser booking order: the runtime builds the proposal from live facts the person chose -> exact approval card -> **Approve click** -> human-input baseline taken after the click -> ONE transaction claims the approval, inserts the attempt and moves the action `EXECUTING` (`begin_exact_execution`, with a guard that runs under the task lock) -> ONE transaction inserts the dispatch and **commits** -> the worker is called with no transaction open -> ONE transaction finishes the dispatch and the attempt together (`SUCCEEDED` / `FAILED` / `OUTCOME_UNKNOWN`). Desktop disclosure (S2) is not authority for any of it: no grant, no S2 code path and no provider is involved.

## 4. Migration `0014`

`services/agent/alembic/versions/0014_desktop_dispatches.py`: one table, `desktop_dispatches` (`id`, `action_id`, `attempt_id` **UNIQUE**, `worker_generation`, `operation` in `focus_surface | scroll_control | launch_app`, opaque `surface_ref/epoch`, `observation_id`, `snapshot_digest`, `control_ref`, `app_id`, `input_tick`, `status` in `DISPATCHED | OK | FAILED_BEFORE_EFFECT | OUTCOME_UNKNOWN`, closed `error_code`, whitelisted `result`, `started_at`, `finished_at`). The database enforces: `finished_at` is null exactly while `DISPATCHED`; ref/app-id/digest shapes; and that each operation carries **exactly** the identity it needs (a focus names a surface and no control or app; a scroll names surface, observation, digest and control; a launch names an app and nothing else). There is no column for a title, path, HWND, PID, coordinate or value; the migration test pins the column set and a name scan. Downgrade drops the table. `test_migration_0013` now steps down to `0013`; `test_migration_0014` and `test_migrations_match_table_definitions` pass.

## 5. Focus policy and verification

* **Effect primitive: UIA `SetFocus` on the top-level window root**, one call site in `uia_backend.py`. A first design used Win32 `SetForegroundWindow`; **measured on this machine, the Windows foreground lock refuses it from a background process** (the fixture stayed behind), while UIA `SetFocus` brought it forward. No Win32 foreground call, `AttachThreadInput`, `ShowWindow` or synthesized key/click exists anywhere.
* **Only already-visible ordinary windows.** A minimized, cloaked, hidden or hung window is refused `surface_not_focusable`; Lumi never restores or shows anything (documented; restoring would widen the effect).
* **Before focusing** (in the worker, on the UIA thread): the runtime checked the worker generation; the worker re-resolves `(surfaceRef, surfaceEpoch)` against the live `(pid, hwnd, process creation time)`, refuses Lumi's own process tree, the credential/consent image deny-list, elevated or integrity-unverifiable processes, runs the S1 credential scan of the window (a password input anywhere refuses it), and refuses on human input since the approval baseline.
* **Verification:** after the call the worker re-proves the surface identity and reads the foreground window; the answer is `focused` only when the foreground is that exact HWND owned by that exact process. A foreground that is anything else is `not_focused`, recorded as a **known failure** (`FAILED`, dispatch `OK`, `error_code = not_focused`). Anything that fails after the call (an unreadable foreground, a lost input read) is `desktop_effect_uncertain`, never a known failure.

## 6. Semantic scroll and its verification

Only UIA `ScrollPattern.Scroll(NoAmount, amount)`, one call site, with a **closed step**: `small_up`, `small_down`, `page_up`, `page_down` (a pydantic enum on both sides; no number, no key name, no float). The person picks a scrollable control from a list Lumi builds by observing the window **locally** (`POST /desktop/actions/scroll-targets`; nothing leaves the runtime). A proposal binds surface, observation id, snapshot digest, control ref and step; it must be made from the **newest** observation of that surface, no older than **60 seconds**, and the same freshness is re-checked inside the claim transaction.

Immediately before the scroll the worker: re-resolves the surface and re-derives the control from the live tree by exact match (`element_missing` / `element_ambiguous` / `element_changed`, credential inputs on the path refuse), requires the `ScrollPattern` and `VerticallyScrollable`, and refuses on human input. The old observation's refs are killed **before** the effect, so no `uN` of that observation can be used again whatever the scroll does. Afterwards the runtime takes a **fresh S1 observation** (never extends the old one) and records its id.

Success means *the requested semantic scroll operation occurred and Lumi re-observed the UI*. It does **not** mean the wanted content is now showing (the card says so). `unchanged` (both positions read and equal) is a known failure `scroll_no_change`; an unreadable position with a normal return is reported as done with the position shown as unknown.

## 7. Registered application descriptor

The model, the renderer and the user's typed text can name an application only by `appId` (`^[a-z][a-z0-9_-]{0,31}$`, enforced in pydantic, in TypeScript, in the route body and by a database CHECK). The descriptor (`app/desktop/registry.py`) owns the canonical absolute executable and optional fixed arguments, and comes from **trusted configuration only**: `LUMI_DESKTOP_REGISTERED_APPS`, a JSON list read by Electron main from its own environment and validated whole by the runtime at startup (a bad document stops the runtime rather than being half applied). **There are no built-in applications** (a first design shipped Notepad; the review pointed out that on current Windows it is a launcher stub for a packaged app, which breaks duplicate detection).

## 8. Arbitrary path / argument / shell exclusion

A descriptor is refused unless its executable is: a local absolute path (no relative, UNC or `..`), a `.exe`, under `%SystemRoot%`/`%ProgramFiles%`/`%ProgramFiles(x86)%` (administrator-writable) and not in a user-writable subdirectory (`Temp`, `Tasks`, `Tracing`, ...), not on a forbidden-program list (shells, script hosts, interpreters, LOLBins, credential/consent UI: `cmd`, PowerShell, `wscript`, `mshta`, `rundll32`, `msiexec`, `python`, `node`, `java`, `mmc`, `wt`, `regedit`, `msdt`, `ssh`, `logonui`, `consent`, ...), and not Lumi's own executable (checked again at launch against the running roots). At most four fixed arguments, no NUL/newline. The launch request has no path, argument, working-directory, environment, URI or shell field (`extra="forbid"`; tested). The process is started with `subprocess.Popen`, an argument **list** `[app.executable, *app.args]`, `shell=False`, `close_fds=True`, stdio to `DEVNULL`, the working directory the executable's own directory, and an **allowlisted environment** (no `LUMI_*`, no token, no database URL, no provider key). `.bat`/`.cmd` and `cmd /c` cannot be registered. A registered VS Code means the application only: no document, workspace, task or terminal.

## 9. Duplicate-launch prevention

Before any spawn, in the worker and in this order: (1) the human-input check; (2) refuse a descriptor that is Lumi's own image; (3) adopt or refuse an earlier launch whose verification failed (below); (4) look for a live process whose **image path is exactly the registered executable** (creation-time-bound identity via `process_identity`, compared with `realpath` so a junction or alias is still recognised, Lumi's own tree never counts, a same-named file in another directory does not count, and a recycled PID has a different creation time and is not an instance); (5) if one exists, bring **that** instance forward instead (same approval) and report `already_running`; (6) a last human-input re-check immediately before the spawn; (7) spawn and verify. A dispatch id is performed at most once per worker generation, and a finished one **replays its stored answer**, so a lost reply is recovered by asking again under the same dispatch id, not by a second effect.

The review found a real duplicate path here: if verifying a freshly spawned process failed, the process was never recorded as launched, ancestry then called it Lumi's own (it is a child of the worker), a later request could not see it and started a second copy. The worker now records the spawned pid as **pending** before verifying; the next request resolves it (adopts a live, provably-registered process; clears a dead one; **refuses** an alive-but-unprovable one) before anything may spawn.

**Launched applications are ordinary applications.** They are started *outside* the runtime's kill-on-close job (`CREATE_BREAKAWAY_FROM_JOB`, which required adding `JOB_OBJECT_LIMIT_BREAKAWAY_OK` to that job; see residuals) so they are not treated as Lumi's own and are not killed with Lumi's tree, and the worker keeps a creation-time-bound `launched` exemption so the ancestry rule (which would call every child of the worker Lumi's own) does not hide them.

## 10. Human takeover

`GetLastInputInfo` (a tick that changes when the person uses the machine; the input itself is never seen and there is no hook or keylogger, and no clipboard is read) is read **after** the Approve click (so the click is not "takeover") and persisted in the dispatch as `input_tick` (a number, not input). The worker refuses with `human_input_detected` if it differs before the effect, and reports `input_changed` if it differs after, in which case the runtime records it, stops, and the card says Lumi stopped; a new step needs a new approval. For launch the check repeats immediately before the spawn and before bringing an existing instance forward. **Measured on this machine**: the person at the keyboard produces a ~60 Hz tick stream, and the real-window tests wait for a quiet moment (and skip rather than pass falsely). Residual: a key-up that arrives after the baseline can cause a spurious refusal (fails safe).

## 11. Recovery and unknown outcomes

`DesktopWorkerClient` refuses an answer addressed to another dispatch, generation or app (`StaleDesktopResult` -> `EFFECT_UNCERTAIN`, and the worker is fenced). Classification: a refusal raised **before** an effect can begin (identity, trust, takeover checks) is a known failure; `worker_unavailable`, `observation_timeout`, `desktop_effect_uncertain`, `desktop_backend_failed` (from an effect call), a garbled answer and any unexpected exception are `OUTCOME_UNKNOWN`. Nothing retries. A runtime that dies between the dispatch commit and the answer leaves an unfinished attempt: `RecoveryService` marks it `OUTCOME_UNKNOWN` (`runtime_restart`) and closes the dispatch in the same transaction; a test kills the service mid-call, recovers and asserts the worker was called once. Focus/scroll/launch are recoverable by a fresh observation and a **new** approval; an `OUTCOME_UNKNOWN` S3 action therefore does not block a new one (a launch inspects for a running instance first). The effect and its bookkeeping run as one task a cancelled HTTP request cannot interrupt, so a caller that disappears cannot leave an action `EXECUTING`.

One desktop action at a time: a proposal supersedes any unanswered card (which can then never run), and a proposal made while an approved/running action exists is refused (`desktop_action_open`). Generic `/actions/*` routes can no longer approve, attempt, finish, reconcile or plant any `DESKTOP_*` action (`use_desktop_route`); rejecting stays available as Stop.

## 12. Source scanner evolution

The S1 zero-input scanner was **not** deleted. It is now an exact-capability scanner: every S1 deny-list name is still forbidden everywhere, and S3's three primitives are allowed only at exact call sites:

* `SetFocus`: one call, zero arguments, inside `uia_backend.py`'s `focus` method, no member reference dodge (`f = el.SetFocus`), and `.focus()` in `effects.py` only on a `root_for(...)` window root;
* `ScrollPattern.Scroll`: one call, two arguments, inside `scroll`;
* the process start: exactly one `subprocess.Popen` in `effects_win32.py`, whose argument vector and **every** keyword are pinned by AST (`shell=False`, `env=launch_environment()`, `cwd=ntpath.dirname(app.executable)`, `DEVNULL` stdio, `close_fds=True`, the exact creation flags); no `subprocess` alias, `from subprocess import`, `run/call/check_output`, `os.popen/system/startfile`;
* `effects_win32.py` may call only `GetForegroundWindow`, `GetLastInputInfo`, `OpenProcess`, `CloseHandle`, `QueryFullProcessImageNameW`; `SetForegroundWindow`, `ShowWindow`, `AttachThreadInput`, `SendInput` etc. are flagged in every file including that one;
* the job breakaway flag may appear in `effects_win32.py` and `windows_job.py` only.

Planted violations prove the scanner catches SendInput, keyboard, mouse, Invoke, SetValue, Select, Toggle, clipboard, arbitrary CreateProcess, ShellExecute, PowerShell, `cmd` and 25 mutations of the spawn call; the independent review found four scanner holes (bare-name `Popen`, alias imports, `os.popen` under a file-wide allowance, an unpinned argument vector / `env=os.environ`) and all are closed with planted tests. S4 primitives remain forbidden.

## 13. Electron

Eight fixed IPC methods/channels (`listDesktopApps`, `findDesktopScrollTargets`, `proposeDesktopFocus`, `proposeDesktopScroll`, `proposeDesktopLaunch`, `getDesktopAction`, `approveDesktopAction`, `declineDesktopAction`), each checking the sender first; the supervisor allows exactly the matching runtime routes and nothing that clicks, types, sets a value, selects, invokes or names a path. `DesktopActionController` is its own class (not in the voice backend's `Pick`), imports no model, interpreter or memory code, validates every input (ids, `s1..s16`, epochs, `u1..u200`, the closed step enum, app-id shape), is single-flight per approval and never retries when the runtime restarts under an approval. The wire parser copies a whitelist only. The trusted card states exactly one step ("Lumi will bring this window to the front" / "scroll this part of the window: one page down, using the window's own scrolling, not the mouse or keyboard; afterwards what you were shown is out of date" / "open this application; if it is already running, bring that one forward"), that it is once, and that Lumi stops if you use the keyboard or mouse. Application strings render only as inert, escaped text inside a labelled quote. `desktop-firewall.test.ts` was rewritten **on purpose**: the bridge may gain these eight methods and none other, and there is still no click, key, coordinate, value, shell or path anywhere in Electron.

## 14. Tests

| Area | Evidence |
| --- | --- |
| Worker domain (fakes) | `test_desktop_effects.py`: exact focus, stale/recycled HWND+PID, elevated, credential, Lumi, wrong generation, focus theft, foreground lock, minimized/cloaked, takeover before/during, dispatch replay and reuse, scroll success/stale/changed/vanished/non-scrollable/credential, closed step, launch descriptor/arguments/duplicate/PID reuse/lost reply/slow window/spawn failure/takeover/own-image, post-effect failure -> uncertain, no built-in apps |
| Worker protocol | `test_desktop_worker_app.py`: the route table is health + two reads + three reviewed effects; unknown paths 404 |
| Ledger against PostgreSQL | `test_desktop_actions_service.py`: durable dispatch first, single-use approval, racing approvals, wrong revision, decline, supersession, dead worker generation, stale surface/observation, refusal vs lost/uncertain answer, crash + recovery, cancellation, concurrent proposals, running-effect lock, scroll freshness/supersession, launch registry-only, no title/path in rows, DB identity constraints, immutable proposal, generic routes cannot touch a desktop action, generic reads hide window text |
| Real Win32 + UIA | `test_desktop_effects_windows.py` (`desktop_uia`): real UIA focus of a background window, real takeover refusal, closed window, real `ScrollPattern` scroll of a ListView with every unrelated fixture counter at zero, non-scrollable refusal, a **real launch** of Character Map (once, verified, ordinary surface, second request `already_running`), own-image refusal, environment scrub |
| Scanner | `test_desktop_source.py`: all planted violations, exact pins per module |
| TypeScript | controller (validation, exact routes/bodies, single-flight, no retry on restart, truthful messages, whitelist wire), card (inert hostile text, wording, no retry after unknown), firewall |

## 15. Independent Claude review (no external model)

A Claude subagent, read-only, was asked to break S3. No critical issue and no keyboard, mouse, shell or path authority. Findings and dispositions (each verified against the code):

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | High | Post-effect exceptions (input read, foreground read, response construction, spawn verification) surfaced as `BACKEND_FAILED`, classified a known pre-effect failure, allowing a duplicate on re-approval | **Fixed**: everything after an OS call is `EFFECT_UNCERTAIN`; `BACKEND_FAILED` from an effect call is unknown; regression tests |
| 2 | Med-high | Generic `/actions/*` and `POST /tasks/{id}/actions` did not exclude `DESKTOP_*` (could mark an effect done, wedge the global lock, plant a bad proposal) | **Fixed**: `use_desktop_route` in every generic route (any case); tests |
| 3 | Medium | Scanner gaps (bare-name/aliased `Popen`, file-wide `popen`, unpinned argv/env, member-reference dodge, file-wide focus/scroll names) | **Fixed** with an exact AST pin and planted tests |
| 4 | Medium | `BREAKAWAY_OK` lets any descendant that passes the flag leave the runtime job | **Documented residual** + a scanner rule confining the flag to two modules; nothing else passes it |
| 5 | Med/low | Registry deny-list short; Lumi's own exe registrable; user-writable subdirectories of the roots | **Fixed** (wider list, own-image refusal, writable-part refusal, cwd) |
| 6 | Med/low | Built-in Notepad is a stub on Windows 11 (duplicate detection) | **Fixed**: no built-in apps |
| 7 | Low | No takeover re-check right before the spawn/focus; key-up false positives | **Fixed** (re-checks); false positive **documented** |
| 8 | Low | `scroll` reports done when a position is unreadable; `not_focused` treated as verified no-change | **Documented** (call returned normally; position shown unknown; `not_focused` means "not in front") |
| 9 | Low | Concurrent proposals; cancellation leaving `EXECUTING` | **Fixed** (proposal lock; shielded settle task) |
| 10 | Low | Window title/control name stored in the proposal and returned by generic reads | **Partly fixed**: generic reads hide them; the row still holds them for the card (documented) |
| + | (mine) | While fixing #1 the regression test exposed a real duplicate-launch path (spawned-but-unverified child hidden by ancestry) | **Fixed** (pending-launch adoption); test |

## 16. Validation

| Check | Result |
| --- | --- |
| `uv run mypy` | `Success: no issues found in 253 source files` |
| `uv run pytest -m "not browser"` | 2,022 passed, 2 failed, 323 deselected (16m40s). The 2: `test_booking_routes_without_a_worker_answer_503` (known baseline: fails only because this machine's `services/agent/.env` sets `LUMI_PUBLIC_INSPECTION_HOSTS`; passes with it emptied, confirmed) and `test_the_runtime_exposes_exactly_these_desktop_routes_and_no_verb` (an S2 route-set pin that S3 changes **on purpose**; rewritten to exclude `/desktop/actions/*`, which `test_desktop_runtime` pins exactly; passes) |
| S3 Python tests | `test_desktop_effects.py` + `test_desktop_source.py` + `test_desktop_worker_app.py`: 270 passed; `test_desktop_actions_service.py`: 32 passed (real PostgreSQL); migrations `0013`/`0014`/schema: 13 passed |
| `pytest -m desktop_uia` (real Win32 fixture, real UIA, real launch) | `test_desktop_effects_windows.py`: 8 passed (real UIA focus, takeover refusal, stale window, ListView `ScrollPattern` scroll with every unrelated counter zero, non-scrollable refusal, Character Map launched once and never duplicated, own-image refusal, environment scrub) |
| `npm.cmd run typecheck` | clean |
| `npx vitest run` | 2,320 passed, 22 skipped and the same failures this machine had before S3 (`real-inference` and `tokenizer-pack` need model files; the `accessibility` scam-card CSS assertion); the one S3-caused failure (an S2 test counting *all* desktop channels) was fixed on purpose. New: 8 controller tests, 8 card tests, the rewritten firewall (13) |
| `npm.cmd run eval` | 118/118 eval cases passed |
| `npm.cmd run build` | passes |
| `npm.cmd run package:dir` | exit 0; bundle contains migration `0014`, `effects.py`, `effects_win32.py`, `registry.py`, `desktop_actions.py` (service, domain, repository); no fixture, no test file, no `.env`; the build check fails without them |

No browser suite was run: S3 does not change browser behaviour. It does add one shared behaviour, `services/windows_job.py` (the runtime job now allows breakaway), which only affects a process that passes `CREATE_BREAKAWAY_FROM_JOB`; the existing job/orphan tests in the non-browser suite passed. The `ActionService` is unchanged; recovery gained one call that closes a desktop dispatch (`test_outcome_unknown_recovery` and the new crash test pass). The authoritative ~42-minute browser suite is scheduled for the final M9 gate, per the brief.

## 17. Package impact

No new native dependency and no new executable (`subprocess.Popen` of a registered program is not a helper EXE). `build-agent-runtime.mjs` additionally fails the build if migration `0014` or `app/desktop/effects.py`, `effects_win32.py`, `registry.py`, `app/services/desktop_actions.py` is missing from the runtime bundle. The registered-application list is user configuration and is never bundled.

## 18. Authenticode status

Unchanged: signing infrastructure READY, production certificate NOT CONFIGURED, real-account release BLOCKED. Development `package:dir` success is not production-signed installed validation.

## 19. Residual risks (honest)

* **Job breakaway.** The runtime's kill-on-close job now allows a process that *asks* (`CREATE_BREAKAWAY_FROM_JOB`) to leave it; only the launch module asks, a scanner rule keeps it so, and I could not verify what Chromium or libuv pass. A launched application outlives Lumi (intended: it is the user's application).
* **Registered-executable TOCTOU/impersonation.** The path is validated at startup and compared by realpath at launch; an administrator who replaces the file between them defeats it. The roots are administrator-writable by construction.
* **Path-only instance identity.** Launcher-stub or packaged applications whose real process runs from another path cannot be deduplicated by image path (that is why there are no built-ins); only register applications whose process is the registered executable. A launcher that hands off and exits is reported `desktop_effect_uncertain`.
* **Focus is not foreground-verified against every ordering**: `focused` means the foreground is that exact window at the moment after the call; another program may take it a moment later.
* **Take-over false positives** (key-up after the click) refuse a step that could have run.
* **Titles/control names** are persisted in the immutable proposal for the card (`desktop_private`-class text; not returned by generic reads, not in dispatch rows, events, logs or IPC beyond the card).
* **Scroll of an unreadable position** is reported done with the position unknown.
* Application text remains untrusted and prompt-injection-capable; S3 has no model in the loop, so there is nothing to inject into.
* No installed, production-signed validation; one framework (Win32/UIA fixture) plus one real classic Win32 program (Character Map) on this machine.

## 20. Confirmation

**S4, S5 and M10 were not started.** No Invoke, value, selection, coordinate, mouse, keyboard, hotkey, drag, clipboard, screenshot, vision, shell or arbitrary launch was added.
