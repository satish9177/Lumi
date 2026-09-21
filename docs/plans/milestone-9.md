# Milestone 9: Windows semantic computer use

> **Lumi can read a Windows application the way a screen reader does (roles, names, values and states from UI Automation) before it is ever allowed to touch one.**

Status: **S1 implemented (observation only). S2 and later are NOT started. M10 is NOT started.**

This plan refines the M9 entry in `docs/plans/general-computer-use-architecture.md`. That entry lists window inventory, focus, app launch, UIA observe/invoke/value/selection/scroll and a scoped visual fallback. M9 is delivered in slices, in the same order M8 used: **observation and target identity are reviewed before any write exists.** Each slice below is one reviewable change with its own tests and review report.

```text
M9 - Windows semantic computer use

S1  current (done)  isolated UIA worker + surface inventory + bounded semantic observation. Zero input.
S2  later           explicit desktop disclosure scope + planner observation (first time desktop text may reach a provider)
S3  later           trusted app focus + passive scroll observation
S4  later           bounded Invoke / Value / Selection actions on re-resolved targets
S5  later           limited visual fallback, only where semantics are unavailable
```

## The S1 invariant

> Lumi can observe a Windows UI semantically but cannot change, focus, invoke, type into, select, scroll, click, move, resize, launch, close or otherwise operate that UI.

Everything in S1 exists to make that sentence checkable rather than promised.

## What S1 delivers

1. **An isolated desktop worker**: `python -m app.desktop.main`, a separate process from Electron main, the durable runtime and the browser worker, launched by the runtime with an allowlisted environment and a fixed argument vector. It never receives `DATABASE_URL`, a provider key, a browser profile path, a cookie, task history, a shell, an executable path or Python to run.
2. **Windows surface identity.** A handle alone is not an identity. Each surface slot binds `(pid, hwnd, process creation time)` and carries an epoch that only increases, so a `(surfaceRef, surfaceEpoch)` pair is never reissued in a worker generation.
3. **Opaque, stale-safe refs.** `s1..s16` for surfaces, `u1..u200` for controls. A ref dies with its worker generation, its process, its window, a material structure change, or the observation it came from.
4. **A bounded semantic projection** (200 nodes, depth 12, 120 characters per string, 12 KB of text, 1000 elements scanned) with a closed role vocabulary and explicit truncation.
5. **Refusals before reading**: Lumi's own process tree, elevated or integrity-unverifiable processes, credential/consent brokers, and any surface containing a credential input.
6. **Containment.** UIA runs on one dedicated MTA thread in the worker. A hung provider poisons the worker; the runtime kills and fences that generation and the next read starts a new one.
7. **Local re-resolution** of a control from the live tree by exact match (`element_missing`, `element_ambiguous`, `element_changed`), implemented and tested although no action consumes it yet.
8. **Two typed runtime routes**, `GET /desktop/surfaces` and `POST /desktop/observations`, opt-in behind `LUMI_DESKTOP_OBSERVATION`, with no renderer or preload bridge.
9. **Migration `0012`**: `desktop_worker_generations` and `desktop_observations` (safe projection only).
10. **Source-level proof of zero input** and a structural provider/memory firewall.

## What S1 deliberately does not do

- No focus, activation, foreground change, show/hide, move/resize, close or launch of any window or process (including registered-app launch).
- No `Invoke`, `SetValue`, `Select`, `Toggle`, `Scroll` or any pattern action; no `SendInput`, keyboard or mouse; no clipboard read or write.
- No `desktopCapturer`, screenshot, OCR, vision or coordinates.
- **No provider disclosure.** Desktop observations are `desktop_private` and `untrusted_environment`. They are not given to Claude, OpenAI, Gemini, a voice model, the planner, memory, research context or any task summary. The planner is unchanged.
- No renderer/preload function and no visible UI. The routes are internal and require the runtime bearer credential, which only Electron main holds.
- No change to M7 research, M8 profiles, sign-in, authenticated reading, form observation/disclosure/draft, capture guards, booking or the Authenticode gate.

## Design decisions

| # | Decision | Reason |
| --- | --- | --- |
| D1 | pywinauto UIA (over comtypes) behind a small `UiaBackend` interface; not PyAutoGUI; no coordinate fallback. | Follows the architecture. The spike (S1 review, section 4) showed it installs, imports and enumerates under the packaged Python 3.12. |
| D2 | Separate worker process, not a thread in the runtime. | A stuck COM provider can leave a thread hung forever. The process is the only real containment boundary. |
| D3 | Dedicated MTA thread inside the worker as well. | UIA calls never run on an event loop; the worker's HTTP loop stays responsive to report a timeout. |
| D4 | Slots with monotonic epochs instead of per-inventory refs. | Makes `(ref, epoch)` globally unique in a generation, so HWND/PID reuse and window churn can never re-attach an old ref. |
| D5 | Control refs belong to the latest observation only. | An action slice must re-derive from a fresh observation; older refs are dead by construction. |
| D6 | Process-ancestry exclusion, seeded by the supervisor, bound by creation time. | Title matching is trivially spoofed in both directions. The runtime and Electron roots cover the renderer, DevTools, the runtime, the browser worker and every Chromium it owns (sign-in, takeover and form-preparation windows) and the desktop worker itself. |
| D7 | Elevated and integrity-unverifiable surfaces are withheld from the inventory (no title read) and re-checked before any traversal. | Same-integrity content is untrusted; higher-integrity content is not ours to read, and "cannot tell" fails closed. |
| D8 | A credential input anywhere in the scan budget makes the whole surface `credential_surface` with zero content. | A partial snapshot around a password field is not safe. The password flag is read before any text, so the value is never fetched. |
| D9 | Two-pass stability check. | Two different answers are two different windows; stitching them would invent a UI that never existed (`surface_changed`). |
| D10 | Opt-in (`LUMI_DESKTOP_OBSERVATION`), lazily started, no product caller. | Same rule as the browser worker: a deployment cannot acquire a capability by accident, and nothing else depends on the worker being present. |
| D11 | Retention of the newest 25 observations and for at most 24 hours. | Desktop text is private and local; it is not kept indefinitely. |
| D12 | The worker generation is part of the observe request. | `(ref, epoch)` is unique only within one generation; a new worker restarts every slot, so an old pair must not resolve there (found by the independent review). |
| D13 | Soft time budget, cache-request batching and a sibling cap, under a 30 s hard deadline. | Measured: uncached reads took ~9 s for 200 nodes of a simple Win32 window. A big window should come back marked `time`, not die at the deadline. |
| D14 | Exact allowlists for `win32.py` and `uia_backend.py`, beside the deny-list scanner. | A deny-list of names is a tripwire; an allowlist of the only calls those files may make is a pinned surface. |
| D15 | Job-object membership excludes Lumi processes when the runtime created its job. | Ancestry breaks when a launcher exits; the kill-on-close job does not. |

## Later slices (not implemented, boundaries refined)

**S2, disclosure.** Define an explicit, user-approved desktop disclosure scope: which surface, which fields, what text, for what task and for how long. Desktop text is untrusted environment data and must never carry instructions or approvals. Only then may an observation be projected into a planner payload. Needs its own classification, redaction and firewall rewrite (this slice's firewall tests are meant to be changed on purpose here).

**S3, focus and passive scroll.** Trusted, user-initiated focus of a registered application, and scroll *observation*. Focus is a state change, so it takes a per-action approval, a stale-identity check before use, and human-input takeover detection. Registered-app launch belongs here or after, never as arbitrary paths.

**S4, actions.** Bounded `Invoke`, `ValuePattern` set and `SelectionItem` select, each on a control re-resolved from the current tree, each with an exact approval, effect classification, `OUTCOME_UNKNOWN` handling and the existing action ledger. No hotkeys, drag, terminal typing or coordinates.

**S5, visual fallback.** Scoped region capture only where UIA exposes nothing, with the existing capture consent and guards, DPI/multi-monitor transforms, and no blind coordinate click.

## Test strategy

- A deterministic Win32 fixture (`tests/desktop_fixture_app.py`, ctypes only, never bundled) with standard controls, a dynamic mode, a credential mode, bulk/deep modes, and target-side event counters that prove observation caused zero clicks, invokes, edits, selections, focus changes or activations.
- Domain rules against scripted fakes (identity, exclusion, elevation, credentials, bounds, epochs, re-resolution, stability), which run on any platform.
- The real UIA backend against the real fixture (`-m desktop_uia`, Windows only), and the real worker as a subprocess, including a genuinely hung provider.
- An AST source scanner with planted violations, schema tests for the absence of identity/geometry/verb fields, and a firewall that proves no other module can read a desktop observation.

## Exit for S1

Only S1's engineering exit is claimed: the tests above, mypy, the Python and TypeScript suites, eval, and `package:dir` with the desktop backend imported from the bundled Python. **Production-signed installed validation remains blocked** by the missing Authenticode certificate, and the real-account release gate is unchanged.
