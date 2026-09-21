# Milestone 9, S1: Windows semantic surface inventory and UIA observation (observation only)

> **Lumi can read a Windows application the way a screen reader does, and cannot touch it. Nothing it reads leaves the local runtime.**

> This does **not** mean Lumi can operate a Windows application, focus, launch or scroll one, see a screenshot, or hand what it read to a model. None of that exists yet.

Status: **S1 implementation closed. S2 and later are NOT started. M10 is NOT started.**

```text
M8b engineering                    COMPLETE
Windows signing infrastructure     READY
Production certificate             NOT CONFIGURED
Real-account release               BLOCKED  (no production certificate; unchanged)

M9
  S1 Windows UIA observation       COMPLETE  (engineering, synthetic/local fixture applications)
  S2+                              NOT STARTED

M10                                NOT STARTED
```

Nothing here used a real account, saved detail or private application. Installed production-release validation was **not** performed and remains blocked by the missing Authenticode certificate.

## 1. Starting point

Branch `lumi-agent-v2`, pushed head `ac380e3` (`docs: document Windows release signing gate`), clean tree.

## 2. Final SHAs

| Commit | SHA |
| --- | --- |
| Implementation: `feat(agent): add Windows semantic observation foundation (M9 S1)` | `3f176da802098e0506bbfde45f2dd677c0149bd6` |
| Docs closure: `docs: close milestone 9 S1 UIA observation` | the commit that adds this file (a document cannot contain its own hash; see the final report) |

## 3. Migration `0012`

`services/agent/alembic/versions/0012_desktop_observation.py` (revises `0011`) adds exactly two tables and one index:

* `desktop_worker_generations(id, runtime_generation -> runtime_generations, worker_started_at, registered_at)`, the identity of one run of the isolated desktop worker, bound to the runtime generation that started it.
* `desktop_observations(id, worker_generation -> desktop_worker_generations, surface_ref, surface_epoch, schema_version, classification, snapshot JSONB, snapshot_digest, truncated, created_at)`, the safe projection only. Constraints: `classification = 'desktop_private'`, `surface_ref ~ '^s([1-9]|1[0-6])$'`, `surface_epoch >= 1`, `schema_version >= 1`, `snapshot_digest ~ '^[0-9a-f]{64}$'`, `snapshot` is a JSON object.

There is no column for a window handle, process id or path, coordinates, an AutomationId or class name, or a password; there is no task association (no planner consumes these observations, so none was fabricated) and no action, input or target-handle table. `test_migrations_match_table_definitions` proves the migration equals the table metadata; `test_migration_0012_adds_exactly_the_desktop_tables_and_downgrades_cleanly` proves the exact column set, that downgrade to `0011` removes exactly the two tables and leaves `form_drafts` and `browser_worker_generations`, and that re-upgrade restores them. Present in the packaged bundle (section 27). The two foreign keys carry explicit short names (`fk_desktop_worker_generations_runtime_generation`, `fk_desktop_observations_worker_generation`) because the naming convention's generated names exceed PostgreSQL's 63-character limit.

## 4. pywinauto / packaging compatibility result

**Compatible. No fallback, no PyAutoGUI, no coordinate automation.** The spike ran before any integration, as required:

| Check | Result |
| --- | --- |
| Add to the agent environment | `pywinauto 0.6.9`, `comtypes 1.4.17`, `pywin32 312`, `six 1.17.0`, added with the marker `sys_platform == 'win32'` (other platforms do not install them). No `greenlet`/Smart-App-Control conflict. |
| Import under Lumi's Python 3.12 | `3.12.14` (uv-managed CPython): imports cleanly. |
| UIA backend enumerates and inspects the fixture | Yes: real top-level windows enumerated, and the deterministic fixture read (sections 20-21). |
| Survives `build-agent-runtime` | Yes: locked install into the bundled Python. |
| Native extensions import from the packaged runtime | Yes, with a scrubbed environment: `pythoncom`, `pywintypes`, `win32api`, `win32gui`, `win32process`, `win32event`, `_ctypes`, and the backend constructs. |
| Packaged worker serves a real window | Yes: the bundled Python's worker observed the fixture (28 nodes, marker seen, every effect counter zero); 16-21 s cold start on this machine. |

Three concrete findings the spike and the packaged build exposed, all fixed and recorded in `docs/PACKAGING.md`:

1. **COM apartment ordering.** The backend must select the multithreaded apartment (`sys.coinit_flags = 0`) before anything initialises COM on its thread. Importing `pythoncom` first makes it a single-threaded apartment and comtypes then fails with "Cannot change thread mode after it is set". The build check imports the backend first for this reason.
2. **`from comtypes.gen import UIAutomationClient` works only after generation.** It worked on the development machine (wrappers generated by an earlier spike) and failed on a clean bundle; the build check caught it. The backend now requests the type library with `comtypes.client.GetModule("UIAutomationCore.dll")`.
3. **First-use cost and machine-specific wrappers.** comtypes checks generated wrappers against the type library's modification time and regenerates on a different machine (about 19 s cold here, into `comtypes/gen` if writable, otherwise `%APPDATA%\Python\Python312\comtypes_cache`). The runtime's worker startup timeout is 90 s. **Not measured on a second machine.**

## 5. Desktop worker architecture

```text
Electron main ---(runtime bearer)---> agent runtime (durable, Postgres)
                                          |  ManagedDesktopWorker (opt-in, lazy)
                                          |  fixed argv, allowlisted env, fresh credential/generation per start
                                          v
                                   desktop worker  (python -m app.desktop.main, 127.0.0.1:0)
                                     |- HTTP loop (uvicorn): auth, generation guard, 2 routes, deadline
                                     `- one UIA thread (COM MTA, daemon): probe (Win32) + backend (pywinauto UIA)
```

`services/agent/app/desktop/` is the worker side (`errors`, `protocol`, `surfaces`, `observer`, `win32`, `uia_backend`, `worker`, `main`) plus the runtime-facing supervisor and client (`managed`, `client`); `app/services/desktop.py` and `app/repositories/desktop.py` are the runtime service and persistence. The worker is a separate process from Electron main, the durable runtime and the browser worker. Enabled only with `LUMI_DESKTOP_OBSERVATION=1`, started lazily by the first desktop request, and nothing in the product calls it. On a non-Windows platform the routes answer `desktop_automation_unsupported`; nothing is faked. If the worker fails, no browser feature is affected (`test_the_capability_is_off_by_default_and_other_routes_are_unaffected`).

## 6. Worker credentials

A fresh 32-byte URL-safe credential per start, in the worker's own environment; the worker binds `127.0.0.1` port 0 itself and reports the port it owns as one JSON line on a pipe only the runtime holds, and the runtime talks only to that reported address. The credential is compared with `hmac.compare_digest` (`test_the_credential_is_compared_in_constant_time` spies on it), required on `/health` and both routes, never echoed. A wrong, missing, non-ASCII or previous-generation credential is refused; unknown paths are 404. A non-loopback `Host` or any `Origin` is refused. The runtime's client sets `trust_env=False` so no proxy in the environment or registry can carry the credential or the text (found by the review). The worker's environment is built from an allowlist (`SystemRoot`, `WINDIR`, `SystemDrive`, `TEMP`, `TMP`, `PYTHONDONTWRITEBYTECODE` plus its own `LUMI_DESKTOP_*`): no `DATABASE_URL`, runtime token, provider key, browser profile path or `PYTHONPATH`/`PYTHONSTARTUP` (`test_the_worker_environment_is_built_from_an_allowlist_not_copied` feeds it a hostile source environment).

## 7. Generation fencing

The worker mints a generation UUID at startup; every request names the expected generation and a mismatch is `stale_worker_generation`; a response for another generation, or about another surface, is discarded by the client. The runtime registers each generation in `desktop_worker_generations`. **The independent review found that `(surfaceRef, surfaceEpoch)` is unique only within one generation**: after a fence, a new worker restarts every slot, so an old pair could have been read as whatever now occupied that slot. The runtime observe request therefore carries the `worker_generation` the surface was listed under, and the service refuses any other generation without fencing the healthy worker (`test_a_pair_from_a_replaced_worker_generation_is_refused_without_costing_the_new_worker_its_life`, a real worker and real fixture).

## 8. COM/UIA thread model

All Win32 and UIA work runs on one dedicated daemon thread inside the worker process, never on the runtime's event loop and never in Electron main. The backend module is imported on that thread so COM initialises there as the multithreaded apartment. The HTTP loop stays free to answer, which is what lets a worker report a timeout while its UIA thread is stuck. Verified: during a real hung provider the runtime's event loop kept ticking (`test_a_real_hung_provider_times_out_within_the_deadline_and_the_process_is_replaced`, `...runtime` variant).

## 9. Process-level timeout containment

A stuck COM provider can leave a thread hung forever, so the boundary is the process. The worker runs each call under a hard deadline (default 30 s, `LUMI_DESKTOP_TIMEOUT_SECONDS`); on expiry it returns `desktop_observation_timeout`, marks itself poisoned (everything else it is asked returns the same) and schedules its own exit; the runtime independently bounds every call by that deadline plus a margin, kills the whole worker (`fence`) and lets the next read start a fresh generation. Verified against a **real** hostile provider: the fixture can block its UI thread on every `WM_GETOBJECT`, and with a 2 s deadline the call returned in well under 8 s, the runtime heartbeat kept ticking, nothing was stored, the worker was fenced and a later read on a new generation succeeded. A worker that dies *mid-call* is reported as `desktop_worker_unavailable` and replaced (`test_a_worker_that_dies_mid_call_is_reported_and_replaced`); a crashed worker gets a new generation and credential, and the old credential is worthless against it. A crash loop is stopped for 60 s, not for the life of the runtime.

**Slow is not hung.** Measuring a 200-node read of the simplest possible Win32 window (250 separate HWND buttons, the slowest case for the Windows MSAA proxy) took **9 s** with per-property reads, against a 10 s deadline the tests had not exercised at full size. The fix is layered: properties are fetched in one batch per parent with a UIA cache request (about 5 s for the same window), siblings are read one at a time and capped at 300 per parent, and the first pass runs under a soft time budget (`min(10 s, 40% of the deadline)`) after which it stops and the observation is declared `truncated` with reason `time` instead of dying at the deadline. A time-truncated read is compared with the previous read over the nodes both saw, so a slow window does not needlessly bump its epoch (`test_a_window_too_big_to_read_in_time_comes_back_marked_not_killed`).

## 10. Surface identity

A handle alone is not an identity. Each of 16 slots binds, internally, `(worker generation, pid, hwnd, process creation time, epoch)`. `s1..s16` are stable for a surface across refreshes, freed when it goes, and reissued with a strictly higher epoch (a vacated slot bumps its epoch, so the old pair can never match the next occupant). None of pid, hwnd, creation time, process path or command line is exposed on the runtime API, in renderer IPC (there is none), in storage or in diagnostics.

## 11. Stale HWND/PID defense

`resolve` re-proves identity against the live system on every use: the window must still exist and belong to the same pid, and that process must still have the recorded creation time; otherwise `stale_surface`. Tests: closed window; process exit; process restart with the same pid and hwnd (recycled numbers); HWND reused by another process; malformed and out-of-range refs; wrong epoch; explicit release; a window replaced during a read (`surface_changed`); the same for real, with a real fixture process restarted under the same title (`test_a_recycled_process_cannot_inherit_a_ref_after_the_fixture_restarts`). A window recreated *inside the same process* with the same handle value before any refresh is not distinguished (Windows handle values carry a uniqueness counter, so this is rare); that is a stated residual.

## 12. Opaque surface/control refs

Surfaces `s1..s16`, controls `u1..u200`, both closed patterns validated on every wire model and request. A control ref belongs to exactly one observation of one surface epoch: a new observation replaces the table, and a material structure change also bumps the surface epoch. No ref survives a worker restart. Re-resolution from the live tree is exact-match only: zero matches is `element_missing`, several is `element_ambiguous`, a match that is a different element instance (runtime id) is `element_changed`; a renamed label is `element_missing` (there is no nearest-label fallback), and a control that has become a credential field is `credential_surface`. Implemented and tested with fakes and against the real backend, though nothing consumes it yet. Live UIA objects are never persisted: a ref maps to a worker-memory locator.

## 13. Semantic schema

`DesktopObservation` (schema version 1, `classification: desktop_private`, `trust: untrusted_environment`): `observation_id`, `surface_ref`, `surface_epoch`, `worker_generation`, `nodes[]`, `node_count`, `depth`, `truncated`, `truncation[]`, value-free `fingerprint`. A node: `control_ref`, `parent_ref`, closed `role` (41 UIA control types plus `unknown`), bounded `name` and `text`, `enabled`, `visible`, `focused`, `focusable`, `selected`, `checked` (`on`/`off`/`mixed`), `expanded`, and `patterns[]`, which is availability only (nothing here calls a pattern). Every model is `extra="forbid"`. **Not projected**: AutomationId, ClassName, FrameworkId, RuntimeId, NativeWindowHandle, ProcessId, bounding rectangle, coordinates, raw property dictionary, COM object or backend representation (`test_the_observation_carries_no_native_identity_or_geometry` pins the exact key set; `test_no_wire_model_has_a_field_for_native_identity_geometry_or_an_action` walks every model). Text precedence: name for every control; value for controls that expose it; for text and document controls the bounded text pattern first (it can be asked for 121 characters, so a huge document costs the same as a small one), then the value pattern.

## 14. Bounds and truncation

200 nodes, depth 12, 120 characters per string, 12 KB of text in total (UTF-8 bytes), 1000 elements scanned, depth 24 scanned, 300 siblings per parent, and the time budget. Truncation is declared with reasons `nodes`, `depth`, `text`, `scan`, `time`, and structure is kept even when the text budget is spent. Tested exactly with fakes (including a 6000-row child list, and, measured by hand, 600 real sibling buttons reaching the sibling cap) and for real (node bound with 250 real buttons, depth bound with 14 nested panes reaching 15 UIA levels, 400-character text). The traversal is iterative (no recursion). One residual: the credential scan shares the scan budget, so a credential input beyond 1000 elements or depth 24 is not seen, and the observation says so with `scan` (`test_a_credential_beyond_the_scan_depth_is_not_seen_and_the_observation_says_so`).

## 15. Credential exclusion

A surface is refused as a whole, with zero content, if any element within the scan budget is a credential input: UIA `IsPassword`, or an edit or combo named like a credential (password, passcode, passphrase, PIN, OTP, one-time or verification or security code, CVV/CVC, secret, token, API key, private key, seed or recovery phrase, card number, and a few common translations). The scan continues past the 200-node projection cap; on a hit everything gathered is discarded and only `credential_surface` returns. **The password flag is read first and its value is never requested**: the batch cache request carries no value property and values are read only after the cached flag says the element is not a credential. Proven with fakes (`sensitive_reads == 0`) and, against the real fixture, by a counter on the fixture's password edit that increments for any `WM_GETTEXT`, `WM_GETTEXTLENGTH`, `EM_GETLINE` or `WM_COPY`: it stays **0** through a refused observation, while the password text in the fixture is unchanged and appears in no output, log or exception. Ordinary controls that merely mention passwords ("Show password") are not credentials. Blind spots (a secret field that is neither flagged nor named like a credential; a password manager's window is not special-cased) are residual.

## 16. Elevation refusal

The integrity level of the target's token is compared with `min(Lumi's level, Medium)`, so **even an elevated Lumi does not read an elevated window** (found by the review). A higher-integrity process, an unreadable integrity level (fail closed) and an unknown own level are all withheld from the inventory **without reading their title**, and are re-checked before any traversal at observation time (`elevated_window_refused`, `integrity_unverifiable`; the backend is not touched at all). Lumi requests no administrator right, no `uiAccess`, no secure-desktop access. Verified for real that the probe reads Medium for itself and fails closed for the System process; no real elevated window is used in tests (that would need administrator rights), so the elevated path is tested with a scripted probe. Credential/consent broker images (`consent.exe`, `credentialuibroker.exe`, `logonui.exe`, `lockapp.exe`, ...) are denied as a second layer.

## 17. Lumi / self-target exclusion

By **process ancestry from trusted process identities**, never by title. The supervisor passes the runtime's PID and, when Electron supervises it, Electron main's PID; the worker binds each to its creation time at startup (an unbindable root fails closed) and always adds itself. A process is excluded if it is a root or descends from one through a chain whose parents are no younger than their children (a recycled parent id is not ancestry). That covers the renderer, DevTools, the runtime, the browser worker and every Chromium it owns (sign-in, takeover and form-preparation windows) and the desktop worker. Hardened after review: the process snapshot must succeed and be non-empty (never "nothing descends from Lumi"), windows are enumerated before the snapshot, a live process the snapshot does not know is treated as Lumi's, and **when the runtime created its kill-on-close job, every process in that job is also Lumi's** (`QueryInformationJobObject`; live membership, so no PID reuse), which survives a launcher that exited between a root and a Lumi window. Proven for real: by pid, by ancestry through a launcher, and a real job whose launcher died (`test_a_window_whose_launcher_died_is_still_lumis_by_job_membership`, which shows ancestry alone cannot see the window and the job can). A title that merely says "Lumi" is an ordinary surface; a Lumi window with an innocuous title is excluded (`test_exclusion_is_not_a_title_match`). The model, the page and the user cannot remove an excluded pid. Not verified with real Electron in S1 because no product caller exists.

## 18. Zero-input structural proof

`tests/desktop_source_scan.py` parses every module under `app/desktop/` with `ast`. It fails on any member, name or definition that is an input or window-mutation primitive (invoke, set-value, select, toggle, scroll, focus, activate, foreground, show/hide, move/resize, close, launch, `SendInput`, keyboard, mouse, message posting, clipboard, capture, process termination and more), on import of an input, automation, capture or launch library, on dynamic dispatch (`getattr`, `eval`, `vars`, `attrgetter`, `methodcaller`, ...), on a process-access right stronger than `PROCESS_QUERY_LIMITED_INFORMATION`, on ordinal exports and numeric pattern ids, and on the acting UIA pattern interfaces. It is proven against **over a hundred planted violations** (every bypass the independent review found is now one), against read-only code it must allow, and per file (only `managed.py` may start or stop a process and only `worker.py` may start its own thread). Because a deny-list is a tripwire, **`win32.py` and `uia_backend.py` are also held to an exact allowlist** of the 24 kernel/user/advapi/dwm query entry points and the COM members and pattern interfaces they may use; any other call fails. The production code passes. At runtime the fixture's counters (buttons, `BM_CLICK`, toggles, edits, selections, `WM_SETTEXT`, scrolling, mouse, keys, focus, activation, moves, close, and password reads) stay at zero through every observation while `WM_GETOBJECT` climbs (the read really reached the target). Honest limits: the scanner is a tripwire plus a pinned surface, not a proof, and pywinauto's package import loads its own keyboard/mouse/application modules into the worker process even though nothing in Lumi's code references them.

## 19. Provider / memory firewall

Desktop text is `desktop_private` and `untrusted_environment` and goes nowhere in S1. **No new model call, no planner change.** Proven four ways: (1) a marker (`M9_S1_DESKTOP_PRIVATE_MARKER_71A`) is planted in a fixture edit control and, after a real observation through the runtime, exists in **exactly one table, `desktop_observations`**, out of every table in the database (`test_desktop_text_exists_only_in_desktop_storage` scans `t::text` of every public table), and in no log record (including DEBUG) and no worker stdout or stderr (the window title carries the marker too); (2) a Python importer test pins that only `main.py`, the API routes and errors and the desktop service can import desktop code and that no planner, answer, research, authenticated, booking, form or task module references it, and the desktop table is referenced only by its own persistence; (3) `src/main/agent/desktop-firewall.test.ts` (14 tests) proves nothing in Electron main, preload, renderer or shared mentions the routes, the schema, the marker or the storage, that the renderer bridge has no desktop function, and that the context builder, episodic memory and both planners have no path to a desktop record; (4) Electron main never sets `LUMI_DESKTOP_*`, so the feature cannot be enabled in a packaged build. Diagnostics carry counts, a truncation flag, a duration, a worker generation, an observation id and an error code, never a title, name, text, PID/HWND, path or control ref (`test_diagnostics_hold_only_counts_codes_and_ids`). A hostile string cannot leak through an exception either: unpaired UTF-16 surrogates are replaced at the boundary, every unexpected failure in the worker or the persistence step is reduced to one text-free code before a framework can log it, and a database error no longer quotes the observed text (all found by the review). Desktop text at rest is retained for at most 25 observations and 24 hours (swept at startup even with the capability off). A later slice that defines an explicit disclosure scope must change these tests on purpose.

## 20. Fixture design

`tests/desktop_fixture_app.py`: a test-only Win32 application in pure `ctypes` (never bundled; the build fails if it is), built by a Claude subagent in an isolated worktree to a written spec and then verified by me against real UIA. Standard controls (`STATIC`, `EDIT`, `BUTTON` push/check/radio, `COMBOBOX`, `LISTBOX`, a custom pane), a disabled button, a hidden edit, an off-screen button, plus modes `dynamic` (relabel the heading, destroy and recreate the Submit button with a new HWND), `credential` (an `ES_PASSWORD` edit), `bulk` (N buttons, D nested panes), `--label-length`, `--overflow-items`, and a hostile-provider command. It is shown with `SW_SHOWNOACTIVATE` and never activates itself. Every window is subclassed and **counts the messages that reach it from outside**: `getobject`, focus, activation, `BN_CLICKED`, `BM_CLICK`, toggles, edits, selections, `WM_SETTEXT`, scrolling, mouse, keys, moves, close and password reads; its own mutations are muted. Quirks observed for real: `IsOffscreen` is never true for plain HWND buttons (list items scrolled out of view do report it, so `--overflow-items` exists), the title bar is part of the UIA tree (the standard tree is 28 nodes, depth 3), and an Edit's or ComboBox's name comes from its preceding STATIC label.

## 21. Real UIA test results

Real probe, real pywinauto backend, real fixture process, on a dedicated MTA thread (`tests/test_desktop_uia_windows.py`, 16 tests): opaque refs and no native identity in the listing; process restart makes a ref stale; every standard control projected with its semantics (edit text, button invoke pattern, disabled, checkbox on, radios, combo `Beta` with expand/collapse, list items with the selected one, hidden control absent); hierarchy (pane -> nested button; parents precede children); off-screen list items reported not visible; the marker appears exactly once and no identity field; node, depth and text bounds; a window too big to read in time comes back marked `time`; a dynamic label gives a fresh observation and kills old control and surface refs; re-resolution finds a live control and reports a rebuilt one missing; a credential surface returns no content and the password is never requested; the excluded process and its descendants are absent; the real probe fails closed for unreadable processes; and observation changes nothing on the target (foreground window unchanged, every effect counter zero, `getobject > 0`, fixture state unchanged).

`uv run pytest -m desktop_uia` (real fixture, real worker, real job objects): **33 passed** in about 2 minutes. Of the whole desktop suite (**331 tests**): domain 68, hardening 19, worker HTTP boundary 31, real UIA 16, real worker process 8, runtime + persistence + migration 36, source/firewall 153.

**Manual smoke (Notepad), observation only.** A Notepad this session launched (none was running) was observed passively and then closed by PID: the first read was refused `surface_changed` (a WinUI window still populating), the second succeeded: 67 nodes, depth 6, roles including `window`, `tab`, `tab_item`, `document` (patterns `scroll`, `text`, `value`), `menu_item`, `button`; `desktop_private`. Only roles and counts were printed (Notepad can restore prior text). No Notepad-specific policy exists.

## 22. Worker crash and hang tests

Real subprocess (`tests/test_desktop_worker_process.py`, 8 tests): an allowlisted environment; a fixed argument vector; a worker that never reports ready is killed; the worker serves the fixture; the credential and generation are enforced by the real process; a crashed worker is replaced with a new generation and credential and the old credential is refused; a real hung provider times out within the deadline with a live event loop and a fresh generation serves the next read; the parent watchdog ends the worker when the runtime dies. Runtime level: hang without hanging the runtime, mid-call worker death, a stale generation not fencing a healthy worker, nothing stored for a refused or timed-out read.

One transient was observed and is **not root-caused**: in one full-suite run a real observation in `test_the_real_worker_serves_the_fixture_under_a_scrubbed_environment` was refused `surface_changed` (the two passes disagreed). It did not reproduce in 8 isolated cold-start runs, in 72 reads under 8 CPU burners, or when toggling the fixture's non-client activation state between reads (structure unchanged). `surface_changed` is the designed answer to a tree that moved between passes and the designed response is another read (observation has no effect to double), so the real-window test helpers now retry a `surface_changed` up to twice, as a caller would. It is recorded as a residual.

## 23. mypy

`uv run mypy` (strict, `app`, `tests`, `alembic`, `evals`): **Success, no issues in 228 source files** (the new `comtypes` and `pywinauto` libraries are configured `ignore_missing_imports`).

## 24. Python suite

`uv run pytest -m "not browser"` on the final tree: **1769 passed, 1 failed, 323 deselected** (14 min 22 s), run alone on a freshly migrated and truncated database. The one failure is the known baseline `test_booking_routes_without_a_worker_answer_503` (an environment-configured browser worker makes the route answer `browser_worker_unavailable` instead of `browser_worker_not_configured`); it is unrelated to this slice and fails identically without it. A first full run earlier in the same session, before the review fixes, was 1768 passed and 2 failed: the same baseline plus the one transient `surface_changed` described in section 22.

The real Windows UIA tests were also run separately: `uv run pytest -m desktop_uia` **33 passed** (about 2 minutes).

Targeted browser subset (shared runtime wiring changed; the browser worker lifecycle code did not): `tests/test_browser_profile_worker.py`, `test_login_takeover_service_browser.py`, `test_authenticated_service_browser.py`, `test_public_page_worker.py` and `test_local_form_draft_browser.py` (browser marker): **70 passed** (14 min). The full 42-minute browser suite was not run.

## 25. TypeScript suite

`npm.cmd run typecheck`: clean. `npx vitest run`: **2240 passed, 22 skipped, 1 failed** across 122 files (115 passed, 4 skipped, 3 failed); the failures are exactly the known baselines recorded in memory since M8b S6 (`accessibility.test.tsx` and the unloadable `real-inference` and `tokenizer-pack` files), unchanged. `npm.cmd run build` succeeded as part of `package:dir`.

## 26. eval

`npm.cmd run eval`, run alone: **118/118 eval cases passed**.

## 27. Packaging

`npm.cmd run package:dir` succeeded on the final tree (exit 0). Bundle 680.6 MB (Python 240.8 MB, agent code 4.2 MB); manifest `version: 3` now records the desktop backend, `pywinauto 0.6.9`, `comtypes 1.4.17`, `pywin32 312`, the process's own integrity level (`8192`, Medium) and the generated wrappers. The build (step 4c, before byte-compiling) verifies from the bundle alone, under a scrubbed environment, that the backend imports (backend first, then the `pywin32` natives explicitly) and constructs, and that comtypes generated `UIAutomationClient.py` into the bundle; it **fails the build** if a native binary of Lumi's own (`.exe`, `.dll`, `.pyd`, ...) appears under `agent/`, if the desktop fixture, harness or fakes are present, if `app/desktop/worker.py` or migration `0012` is missing. The bundle contains `app/desktop/` (source only) and `0012_desktop_observation.py`, no `tests/`, no fixture, no secrets (`.env` check unchanged), and the desktop worker runs on the **existing bundled Python**: no second executable and no helper binary. The browser headless and headed launch checks still pass from the bundle. The packaged desktop worker served the real fixture (section 4). `pywin32` also ships `Pythonwin.exe`, `pythonservice.exe` and script launchers that Lumi never runs; they are third-party and are noted as pruning candidates. Packaged Electron acceptance was **not** rerun: the app/runtime startup path is unchanged and the desktop capability is off in a packaged build. Smart App Control did not block any newly packaged native dependency on this machine; it was not disabled.

## 28. Authenticode / release status

Unchanged. The signing infrastructure exists and the gate is not modified (no compatibility fix was needed: `pywin32`/`comtypes`/`pywinauto` `.pyd`/`.dll` files fall under the existing third-party classification by suffix). The production certificate is still not configured and **real-account release remains BLOCKED**. Nothing here claims installed production-release validation, and no signature status of the produced package was re-verified in this slice.

## Independent review

A fresh Claude subagent reviewed the whole slice adversarially (read-only; it ran throwaway scripts against the pure-logic modules and fakes, not the database or the real worker). Findings and dispositions:

| # | Finding | Disposition |
| --- | --- | --- |
| 1 | HIGH: `(ref, epoch)` unique only per worker generation; the API carried no generation. | **Fixed.** Request carries `worker_generation`; a mismatch is `stale_worker_generation` and does not fence the healthy worker. Real test. |
| 2a-c | Exclusion fails open: failed/empty snapshot, snapshot before enumeration, dead intermediate ancestor. | **Fixed.** Snapshot failure/empty is `BACKEND_FAILED`; enumerate first; unknown live process treated as Lumi's; job-object membership added (real test). |
| 3 | Unpaired surrogate turns a read into an unclassified 502, kills the worker, can put text in a log. | **Fixed.** Sanitised at every boundary; unexpected failures reduced to a text-free code inside the worker before a framework can log them. |
| 4 | Reads unbounded before clipping; large windows time out and kill the worker. | **Fixed and measured** (the real cost was worse than stated: 9 s for 200 simple nodes): cache requests, sibling cap, text-pattern-first bounded reads, a soft time budget with `time` truncation, a 30 s hard deadline. |
| 5 | "Elevated yields no content" only if Lumi is not elevated. | **Fixed.** Ceiling is `min(own, Medium)`. |
| 6 | The zero-input scanner is a deny-list, not a proof; verified bypasses. | **Fixed.** All bypasses are planted violations; exact allowlists for `win32.py`/`uia_backend.py`; wording changed from "proves" to tripwire plus pinned surface. |
| 7 | Client inherits proxy settings. | **Fixed** (`trust_env=False`, tested). The browser client has the same pattern and was deliberately not touched. |
| 8 | LOW: same-process HWND reuse; DB error quotes parameters; no time bound on retention; credential coverage gaps; docstring; `C:\Windows` JS string; lockout; shared `last_stats`; relative-import blind spot; possible foreground flake. | **Fixed** except same-process HWND reuse (documented residual; handle values carry a uniqueness counter) and the credential scan's bounded coverage (now tested and declared). |
| Tests | Vacuous diagnostics assertion; weak status assertion; string-grep constant-time test; password-never-read proven only by a fake. | **Fixed.** Exact dataclass fields; exact 403; a `compare_digest` spy; a real fixture counter for password reads. Missing tests added (cross-generation, snapshot failure/race/dead ancestor, surrogates, huge child lists, credential beyond scan depth, mid-call worker death). |

Things it checked and found sound: identity and epoch logic (a single-generation fuzz of 40 seeds x 3000 steps found no violations), observe ordering (resolve, trust re-check, two-pass stability, verify, project), the bounds and iterative walk, the credential path, the worker boundary, service locking and fencing, migration naming, the closed persisted projection, the firewall, and that no UIA read has a UI-changing side effect beyond the documented accessibility-mode switch.

## Requirement to test mapping (the 32 fixture-test categories)

| # | Category | Test(s) |
| --- | --- | --- |
| 1 | visible top-level fixture appears | uia: `test_the_fixture_appears_with_an_opaque_ref_...` |
| 2 | opaque `surfaceRef` | uia same; domain `test_visible_surfaces_get_opaque_refs_and_no_native_identity` |
| 3 | standard controls projected | uia `test_standard_controls_are_projected_with_their_semantics` |
| 4 | hierarchy preserved | uia `test_hierarchy_is_preserved` |
| 5 | text bounded | uia `test_long_text_is_bounded_and_declared`; domain per-node and total |
| 6 | node bound | uia + domain |
| 7 | depth bound | uia + domain |
| 8 | truncation declared | all bound tests, plus `time` and `scan` |
| 9-12 | disabled, checkbox, radio, combo/list | uia standard-controls test |
| 13 | dynamic label gives a fresh observation | uia `test_a_dynamic_label_gives_a_fresh_observation_...` |
| 14 | stale control ref rejected | same; domain |
| 15 | stale surface after process restart | uia `test_a_recycled_process_...`; domain |
| 16 | simulated HWND reuse cannot reuse a ref | domain `test_hwnd_reuse_by_another_process_is_stale`, `test_process_restart_with_the_same_pid_and_hwnd_...` (fakes; real HWND reuse cannot be forced) |
| 17 | credential surface returns zero content | uia + runtime + domain |
| 18 | password value never appears | uia (`password_reads == 0`, no output) + domain (`sensitive_reads == 0`) |
| 19 | elevated target refused | domain and hardening (scripted probe; a real elevated window needs administrator rights) + real probe fail-closed |
| 20 | Lumi/excluded process absent | uia by pid and ancestry; hardening real job test |
| 21 | worker itself absent | domain (`test_the_worker_itself_is_excluded_...`; the worker owns no window) |
| 22-24 | no coordinates; no PID/HWND; no AutomationId/ClassName/FrameworkId | exact key-set and schema tests (domain, uia, source) |
| 25 | provider marker firewall | runtime `test_desktop_text_exists_only_in_desktop_storage`; source importer tests; TS firewall |
| 26 | no focus change | uia `test_observation_changes_nothing_on_the_target` |
| 27 | no invoke/click/change events | same (all effect counters 0) |
| 28 | worker token required | worker-app + worker-process |
| 29 | stale worker generation rejected | worker-app + worker-process + runtime cross-generation |
| 30 | worker crash recoverable | worker-process crash; runtime mid-call death |
| 31 | observation timeout does not hang runtime | worker-process + runtime real hang with a live event loop |
| 32 | source scanner planted violations | `test_desktop_source.py` (153) |

## 29. Residual risks

* **UIA accessibility quality depends on the target application.** Some applications expose incomplete or misleading trees, and a desktop application's content is `untrusted_environment` even when it is same-integrity and readable.
* **An identical semantic replacement can be indistinguishable.** A replacement with the same roles, names, patterns and enabled states does not bump the epoch, and static-text labels are excluded from the fingerprint on purpose. A time-truncated read compares only the nodes it and the previous read both saw.
* **UIA providers can hang.** Killing the process is containment, not prevention. A window that is merely big is read under a time budget and comes back marked; on a Win32 window made of hundreds of separate HWND controls a full 200-node read still takes about 5 s.
* **Elevated, UAC and secure-desktop applications are unsupported by design.** No real elevated window was used in tests.
* **No coordinate or vision fallback exists.** No screenshots, OCR or clipboard.
* **Desktop observations are private and not provider-visible in S1**, and are retained at rest for at most 25 observations and 24 hours.
* **Credential detection is bounded and heuristic**: 1000 elements, depth 24, 300 siblings per parent, English-centric names with a few translations; a secret field that is neither flagged nor named like a credential is not detected; a credential input beyond the scan budget is declared as `scan` truncation, not refused.
* **Same-process HWND reuse** (a window replaced by another with the same handle value in the same process before any refresh) is not distinguished.
* **A transient `surface_changed`** was observed once on a real window and not reproduced (section 22); the designed response is another read.
* **UIA can switch a Chromium, Electron or Office application into its accessibility mode** (a performance side effect, not a UI change).
* **pywinauto loads its own input modules** into the worker process. Lumi's code has no path to them (source scan), but a compromised dependency would.
* **First-use cost**: comtypes regeneration on another machine (about 19 s cold here) and a 16-21 s cold worker start from the bundle; the worker startup timeout is 90 s. Not measured on a second machine.
* **Exclusion by ancestry** is only as good as the trusted roots the supervisor supplies and, when the job is trusted, the runtime's own job; a Lumi process outside both is not recognised. Electron main is not yet a caller, so the Electron root has not been exercised end to end.
* **Production-signed installed validation remains blocked** by the missing Authenticode certificate, and this slice does not change that.

## 30. Explicit confirmation

**S2 and later slices, and M10, have not been started.** No focus, activation, foreground change, show, restore, move, resize, close or launch of any window or process; no invoke, set-value, select, toggle or scroll; no keyboard, mouse or `SendInput`; no clipboard read or write; no screenshot, OCR, vision or coordinate; no provider, voice, planner, memory or research disclosure; no renderer or preload function; no change to M7, M8, sign-in, authenticated reading, form observation/disclosure/draft, the capture guards, booking or the release-signing gate.
