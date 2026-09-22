# Milestone 9, S5: scoped desktop visual fallback

> **Lumi can now, only when its own deterministic code decides UIA's semantic reading of a window was
> insufficient, take ONE screenshot of that ONE window under an explicit approval, and -- only under a
> SEPARATE, later approval naming one provider and one purpose -- send a FRESH screenshot to that one
> provider for evidence, never an action.** The model never chooses to fall back to pixels: eligibility
> is pure, local, reviewed code. The renderer never picks the provider, the crop, or what a candidate
> means: it only ever reviews and approves what the runtime already narrowed to one bounded capture or
> one bounded disclosure.

> This does **not** add a way to act on what a screenshot shows. There is still no mouse, no keyboard,
> no `SendInput`, no coordinate click, no hotkey, no drag, no clipboard, no shell and no generic
> screenshot route. A vision result is a closed list of evidence (a label, a confidence, a region
> normalised to the crop) -- never a click, a key, an approval or an action -- and nothing anywhere in
> this codebase turns one into either. If semantic UIA cannot re-establish an actionable target after a
> screenshot, that is `manual_handoff_required` territory, not something this slice builds.

Status: **S5 implementation closed (engineering; fixture and real UIA capture on this machine). M10 is
NOT started.**

```text
M8b engineering                    COMPLETE

Windows signing infrastructure     READY
Production certificate             NOT CONFIGURED
Real-account release               BLOCKED  (no production certificate; unchanged)

M9
  S1 UIA observation                 COMPLETE
  S2 Desktop disclosure/reasoning    COMPLETE
  S3 Focus / scroll / app launch     COMPLETE
  S4 Bounded semantic actions        COMPLETE
  S5 Scoped visual fallback          COMPLETE  (engineering; real capture on real UIA)

M9 engineering implementation        COMPLETE pending final cross-slice review
M10                                   NOT STARTED
```

Nothing here used a real account, saved detail or private application, and no provider is called by
default (the desktop capability opt-in gate and `LUMI_SCRIPTED_MODELS` govern this exactly as S1-S4
already do). Installed production-release validation was **not** performed and remains blocked by the
missing Authenticode certificate.

## 1. Starting point

Branch `lumi-agent-v2`, local HEAD at the S4 closing commit (`docs: close milestone 9 S4`), clean tree.
The S3/S4 checkpoint was pushed to `origin/lumi-agent-v2` before S5 work began, per the brief.

## 2. Architecture in one paragraph

Two authorities stay separate for the whole slice, mirroring S2/S4's own split between disclosure and
execution, but applied to pixels instead of text: **consent to capture is not consent to send.**
`app.domain.desktop_vision.classify_fallback_eligibility` is pure, deterministic code over an
already-fresh S1 observation; it is the ONLY thing that can make a capture card exist at all, and it
returns one of three closed reasons (`uia_empty`, `uia_missing_required_semantics`,
`uia_truncated_without_target`) or nothing. `DesktopVisionService.create_capture` observes the surface
locally (silent, as S1 always is), classifies it, and opens a `desktop_vision_capture` grant -- no
provider is named, because none is ever contacted for this grant kind. `claim_capture` consumes that
grant and THEN calls the worker's new `capture()` effect exactly once: a `PrintWindow` of the window's
client area, encoded to PNG by the worker itself, returned once to Electron main for local use (on-device
display, best-effort local OCR) and never persisted as a whole. If that is not enough,
`DesktopVisionService.create_disclosure` opens a SEPARATE `desktop_vision_disclose` grant -- naming one
provider, one model and the person's own typed purpose -- only once that task's capture has SUCCEEDED.
`claim_disclosure` performs a BRAND NEW worker capture (a new `capture_id`, a new native call: never the
first capture's own bytes) and hands it to Electron main for exactly one call to the approved provider.
`DesktopVisionReasoner` (`src/main/agent/desktop-vision.ts`) is the only caller of `ModelRouter.run()`
with `taskClass: 'desktop_vision'`, the ONE class in `PRIVATE_VISION_TASK_CLASSES` allowed to carry
`request.image` at all. The provider's reply is validated into a closed `VisionResult`
(`extra="forbid"`) before it is ever stored, and `record_candidates` records it or the failure --
nothing runs, and nothing can run, from what it returns.

## 3. Migration `0016`

One migration, matching S2/S4's shape:

* `task_grants.kind` gains `desktop_vision_capture` and `desktop_vision_disclose` (no second
  authorization framework, exactly as S2's `desktop_disclose` and S4's `desktop_action_plan` were
  added).
* `desktop_captures`: one row per capture attempt. `grant_id`/`task_id` UNIQUE (one approval funds one
  attempt, ever). `STARTED -> SUCCEEDED | FAILED | OUTCOME_UNKNOWN`. Columns: `surface_ref`/
  `surface_epoch` (audit only), `geometry_fingerprint`/`frame_digest` (char64 hex, format-checked),
  `width`/`height`/`dpi`/`monitor_id` (all NULL until `SUCCEEDED`, all required together when
  `SUCCEEDED` -- a CHECK constraint enforces this). **No column can hold a pixel, a coordinate, a
  handle, a path or a process id.**
* `desktop_vision_disclosures`: one row per disclosure attempt. Same `STARTED -> SUCCEEDED | FAILED |
  OUTCOME_UNKNOWN` / `grant_id`+`task_id` UNIQUE shape, plus `capture_id` (FK to `desktop_captures`,
  RESTRICT -- an audit link to the capture that established eligibility, never a source of reused
  pixels), `provider`/`model`/`purpose`, and `candidates` (JSONB array, NULL until `SUCCEEDED`, a CHECK
  constraint requires it exactly when `SUCCEEDED`). The frame fields are written by a dedicated
  `record_frame` update on the still-`STARTED` row, independent of whether a result ever arrives --
  a crash before the provider replies still leaves an honest audit trail of what was captured.

`test_migrations_match_table_definitions`, the migration downgrade-refusal tests (mirroring S2/S4's own,
including the two test files exercising `0013`'s and `0015`'s standing revision, updated to account for
`0016` now being head) all pass.

## 4. The two task classes and what a provider is shown

Two new, private `ModelTaskClass` values do not exist here -- there is exactly ONE, `desktop_vision`
(`src/shared/model-contracts.ts`), listed in both `PRIVATE_TASK_CLASSES` and the new
`PRIVATE_VISION_TASK_CLASSES`. `DesktopVisionReasoner` (`src/main/agent/desktop-vision.ts`) builds:

* **Trusted**: the person's own typed purpose (`USER_UTTERANCE`), the application label (Lumi's own
  fact), and -- only if local OCR produced text for the SAME image -- that text, labelled `untrusted`
  and explicitly framed as "text Lumi's own local OCR read from the same image", never redacted the way
  S2's identifier redaction works (OCR text is already `desktop_private`/`untrusted_environment`, and
  the rules explicitly tell the model any instruction-shaped text inside it is data, not a command).
* **The ONE image**: `request.image = { mimeType: 'image/png', base64 }`, the sole field on the whole
  router allowed to reach a provider under a private task class outside this one exception.
* **Never received**: conversation history, memory, browser/research context, another task, a window
  title, a handle, a process, a coordinate, or any OTHER image.

**Correction (found during the independent Claude review, section 13):** the OCR-text bullet above
describes `DesktopVisionReasoner.reason()`'s own capability accurately, but `runDesktopVisionDisclosure`
(`desktop-vision-controller.ts`) never actually populates `context.ocrText` when it calls `reason()` --
`runLocalOcr`'s result (from the earlier, SEPARATE local-only capture) is discarded, never threaded
through to the disclosure claim. In the current tree, `ocrText` is therefore always `undefined` and no
locally-read text ever reaches a provider: actual behavior is strictly narrower than this section
originally documented, never wider. This is a functionality/documentation gap, not a security finding
(the safe direction to be wrong in), and is left unwired -- see section 13 for the full reasoning.

`parseDesktopVisionResult` (TS) and `VisionResult`/`VisionCandidate` (Python, `extra="forbid"`) both
refuse a reply naming anything outside `{schemaVersion, candidates}` / `{schema_version, kind, label,
region, confidence, observedText}` -- there is nowhere in either shape to put a click, a coordinate, an
approval or a provider.

## 5. Worker-side capture (`app/desktop/capture_win32.py`, `dpi.py`, `png_encode.py`, `effects.py`)

`capture()` follows the exact order S3/S4 already established, adapted for a read-only effect:

1. refuse a capture id this worker has already begun (dispatch replay, keyed on `capture_id` the same
   way `dispatch_id` works for S3/S4's effects);
2. re-prove the surface (`SurfaceTable.resolve`: worker generation, `(ref, epoch)`, process creation
   time, not Lumi, not elevated, not a credential broker) and run the SAME fresh whole-surface
   credential re-scan S4's mutations added (`_refuse_if_credential_surface`, reused as-is);
3. refuse if the human touched the machine since the approval baseline;
4. read geometry (`WindowsCaptureBackend.geometry`: window rect, client rect, monitor, DPI) and refuse
   `capture_scope_uncertain` if the client-area crop cannot be proven to actually overlap the window
   (`app.desktop.dpi.capture_scope_certain`) -- a check nothing else in S1-S4 needed, because nothing
   else needed to prove pixels correspond to a specific rectangle;
5. a second, narrower human-input refusal immediately before the ONE native call, exactly like S4's
   mutations;
6. perform ONE `PrintWindow` (client-only, full-content-rendering flag for GPU-composited apps), read
   back the bitmap, encode to PNG (`app.desktop.png_encode`, stdlib `zlib` only);
7. verify the surface is still the same afterward (`SurfaceTable.verify_unchanged`); a window replaced
   mid-capture is `capture_refused`, never a guess.

`WindowsCaptureBackend` (`capture_win32.py`) is the third reviewed native-surface file (after `win32.py`
and `uia_backend.py`), pinned by its own exact allowlist (`PINNED_CAPTURE_WIN32` in
`tests/desktop_source_scan.py`); `PrintWindow` is pinned to exactly one call site, inside `capture()`,
with exactly its three reviewed arguments, the same AST-shape discipline `SetValue`/`Select`/`Invoke`
already have. It also declares `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)` once, at
construction (best-effort: a manifest or an earlier caller may already have fixed it), so `GetWindowRect`/
`GetClientRect`/`ClientToScreen` return true physical pixels rather than DPI-virtualized ones -- without
this, every geometry value below would be silently wrong on any non-100%-scale display.

**Frame identity (`app/desktop/dpi.py`, pure functions, no ctypes, fully unit-tested with synthetic
input).** `geometry_fingerprint` digests window rect + client rect + monitor id + DPI + process creation
identity into one value-free SHA-256; it changes on ANY material change (move, resize, monitor change,
DPI change, or the window being replaced by a different process instance), which is what lets staleness
be checked without ever comparing raw coordinates across a process boundary. `monitor_for_rect`
reimplements `MonitorFromWindow(MONITOR_DEFAULTTONEAREST)`'s own contract (greatest-overlap, falling back
to nearest) as pure, testable code rather than trusting the OS call's opaque behaviour; it is exercised
against synthetic 100%/125%/150%-DPI and multi-monitor layouts, including a monitor at a negative
virtual-screen origin and a window moved between monitors (`tests/test_desktop_dpi.py`, 24 cases).

**Encoding.** `app/desktop/png_encode.py` is a small, dependency-free 8-bit RGB PNG encoder (`zlib` from
the standard library; no Pillow, no `mss`, no imaging library anywhere in this codebase, matching the
project's conservative dependency policy). The alpha channel `PrintWindow`/`GetDIBits` returns is
dropped, not passed through: it is unreliable for some GPU-composited windows (frequently zero even
though the colour channels are correct), so treating every pixel as opaque is the safe, well-known
mitigation. Round-trip correctness is proven with a small, test-local PNG decoder
(`tests/test_desktop_png_encode.py`), not merely asserted.

## 6. Domain rules (`app/domain/desktop_vision.py`)

* `FallbackReason` / `classify_fallback_eligibility`: the deterministic trigger, section 2. Deliberately
  narrow and heuristic where it must be (matching S1's credential-name and S2's title-withholding
  heuristics): an empty observation, a tree dominated by unclassifiable roles, or truncation that
  plausibly hid a caller-supplied target hint. A false negative here means "no fallback offered," the
  safe direction to be wrong in.
* `CaptureGrantScope`: worker generation, surface identity, the classifying observation's id (re-checked
  at confirm time), the fallback reason, and a display target -- no `recipient`/`model` field exists on
  this scope at all, because none is ever needed.
* `DiscloseGrantScope`: worker generation, surface identity, the capture's id (audit link only),
  recipient, model, purpose, and a display target; `max_provider_calls: Literal[1]`, `failover:
  Literal["none"]`, matching S2/S4's own scopes exactly.
* `VisionRegion`/`VisionCandidate`/`VisionResult`: the closed evidence schema, section 4. A region must
  stay inside the crop (`x + w <= 1`, `y + h <= 1`, both up to a small float epsilon); confidence is
  bounded `[0, 1]`; at most 8 candidates. `parse_vision_result` refuses a non-object payload or a
  `ValidationError` as `result_malformed`, never partially trusting one.

## 7. The service (`app/services/desktop_vision.py`)

Mirrors `DesktopDisclosureService`'s create/confirm/claim/record shape, doubled for the two grant kinds,
sharing one task:

```text
create_capture   fresh S1 observe (silent) -> eligibility classification -> task + PENDING
                 desktop_vision_capture grant. No pixels yet.
confirm_capture  the trusted click -> grant ACTIVE, bound to that classification.
claim_capture    consume the grant, THEN call the worker ONCE -> ONE screenshot, local use only.
                 Metadata is durable; the image is not.

create_disclosure   requires that task's capture to have SUCCEEDED; names ONE provider/model/purpose
                    -> a SECOND, separate PENDING desktop_vision_disclose grant. Still no pixels sent.
confirm_disclosure  the trusted click -> grant ACTIVE.
claim_disclosure    consume the grant, THEN call the worker for a BRAND NEW screenshot (never the
                    first capture's own bytes) -> returned once for exactly ONE provider attempt.
record_candidates   the provider's closed evidence list, or the failure, or OUTCOME_UNKNOWN.
```

Ordering is the safety property, exactly as in S2: a grant is consumed (a durable compare-and-swap) and
a STARTED record is written *before* the sensitive call it authorises, and no database transaction is
open while that call (a worker HTTP call, or a provider call) runs. If the process dies after the
STARTED row commits, Lumi cannot know whether the screenshot was taken or the image reached a provider,
so the attempt is `OUTCOME_UNKNOWN` (`recover_started`/`_expire_stale_claims`, run at startup and on
every read, exactly like S2/S4) and is never replayed automatically: a retry needs a brand new
observation/capture and a brand new trusted approval.

Read-only capture has no `OUTCOME_UNKNOWN`-blocks-everything story the way S4's mutations do (`_open_locked`'s
unresolved-mutation lock): nothing about the target ever changes, so there is no silent-retry risk to
guard against beyond the ordinary "an approval is spent once" rule every grant already gets.

## 8. Electron main

`DesktopVisionController` (`src/main/services/desktop-vision-controller.ts`) mirrors
`DesktopPlanningController`: its own class, never a member of `VoiceTaskBackend`'s `Pick<...>`, so voice
cannot reach it by construction. Two claim methods:

* `runDesktopCapture`: claims the capture grant, takes ONE screenshot, and runs local OCR on it via an
  injected, optional `LocalOcrEngine` getter -- best-effort, never blocking the capture if the engine is
  absent (the person has not installed the optional "extras" pack) or fails. The image bytes are
  discarded at the end of this method; nothing here returns them to the renderer.
* `runDesktopVisionDisclosure`: claims the disclosure grant, takes a brand-new screenshot, calls
  `DesktopVisionReasoner.reason()` exactly once, and records the result. The provider/model are read
  from the claimed grant's own scope and checked against what `reasoner.candidateProvider()` still
  serves (`canServe`) before the claim -- exactly S4's own "provider still configured before the claim"
  discipline.

`isCaptureBlocked()` (`src/main/services/capture.ts`, built for an unrelated existing screen-capture
feature, gated on login-takeover/form-draft state) is reused as an EXTRA guard immediately before each
of the two claim methods' worker calls -- a login sign-in window or an open form-draft window refuses a
desktop screenshot too, not just the unrelated feature it was built for.

`isAllowedRuntimeRoute` (`src/main/services/agent-runtime-supervisor.ts`) gained the new
`/desktop/captures/*` route patterns to its hard-enforced allowlist; this is a real runtime guard (any
unlisted route throws before a request is ever sent), not only a test fixture, and was caught and fixed
during this slice's own implementation, before any adversarial review.

`registerAgentIpc` (`src/main/services/agent-ipc.ts`) wires nine new IPC channels, none reachable without
the same trusted-sender check (`assertTrustedSender`) every other agent channel already requires.

## 9. Renderer

`DesktopVisionPanel.tsx` is a new trusted panel, structurally parallel to `DesktopPlanningPanel.tsx`:
choose a window, describe what you are looking for (plus an optional exact-word hint for the
`uia_truncated_without_target` check), review the capture card (application/window text inert, the
deterministic reason shown back, "Allow once" / "Cancel"), then -- only after a capture succeeds -- type
a purpose and review a SEPARATE disclosure card (provider/model/purpose, "Allow once" / "Cancel" again).
Candidates render as inert evidence text (`<bdi>`-wrapped, no click handlers, no coordinate math beyond
displaying a percentage) inside a card titled "VISUAL EVIDENCE (NOT AN ACTION)".

## 10. Source scanner (`tests/desktop_source_scan.py`)

`capture`, `printwindow` and `getwindowdc` are unlocked, each pinned to exactly the files that
legitimately own them (`FILE_ALLOWANCES`), on top of everything S1-S4 already forbid everywhere else
(`bitblt`, `getdc`, a blind `screenshot`/`grab`, `pytesseract`, `cv2`, `PIL.ImageGrab`, `mss` all remain
forbidden, including inside `capture_win32.py` itself). `PINNED_CAPTURE_WIN32` is `capture_win32.py`'s
own exact allowlist, mirroring `PINNED_WIN32`/`PINNED_COM`; `gdi32`/`_gdi32` were added to
`_DLL_VARIABLES` so the existing computed-dll-subscript protection covers the new DLL too.
`_single_call_violations` gained one small, general fix during this work: it previously flagged a
ctypes prototype declaration (`dll.Attr.argtypes = ...`) as an illegitimate "reference without a call"
for any newly-pinned single-call member, because it was written against `uia_backend.py`'s comtypes COM
methods (which never need `argtypes`); `PrintWindow`, a raw ctypes DLL function, does. The fix
distinguishes a prototype declaration from a genuine dodge (`f = element.SetFocus; f()`), and applies
retroactively to S3/S4's own pinned members too, closing a latent gap rather than only S5's own need.

## 11. Tests

| Area | Evidence |
| --- | --- |
| Pure geometry/DPI math | `test_desktop_dpi.py`: 24 cases -- 100%/125%/150% DPI scale conversion and its exact inverse; monitor matching with two monitors, a negative virtual-screen origin, a window spanning two monitors, a window moved between monitors, a window off every monitor, zero monitors; fingerprint stability and invalidation on move/resize/monitor/DPI/process-replaced; capture-rect clamping and scope-uncertainty on a torn or zero-area client rect |
| PNG encoder | `test_desktop_png_encode.py`: exact RGB round-trip via a test-local decoder, alpha-channel dropping (the PrintWindow/DirectComposition zero-alpha gotcha), a larger realistic image, refusal of non-positive dimensions and a mismatched buffer length |
| Vision domain | `test_desktop_vision_domain.py`: all three fallback reasons and the not-eligible case; purpose/model validation; the closed candidate schema rejecting region-outside-crop, zero-size region, out-of-range confidence, empty/over-long labels, extra fields (parametrized over `click`/`action`/`coordinate`/`approve`/`operation`/`control_ref`/`grant_id`/`focus`), more than 8 candidates, a non-object payload, and prompt-injection-shaped text in a label surviving as inert data rather than changing the shape; both grant scopes reject an unknown field and confirm `CaptureGrantScope` has no `recipient`/`model` field at all |
| Worker domain (fakes) | `test_desktop_effects.py`: successful capture with exact frame-binding fields; dispatch replay (idempotent on `capture_id`); credential-surface, elevated-surface and Lumi's-own-window refusals (all before any native call); human-input-before refusal; unavailable geometry and a torn/zero-area client rect (`capture_scope_uncertain`), both before any native call; a failed native capture and a native-capture exception (both `capture_refused`, detail-free); a window replaced mid-capture; capture without a configured platform (`UNSUPPORTED`) |
| Source scanner | `test_desktop_source.py`: the ctypes-import invariant now also names `capture_win32.py`; the wire-model forbidden-field scan excepts exactly `CaptureResponse.width`/`.height` (the image's own pixel dimensions, not a coordinate); the importer/`get_observation`-caller allowlists extended by name |
| Real Win32 + UIA (`desktop_uia`) | `test_desktop_effects_windows.py`: a real `PrintWindow` capture of the fixture's own window producing a real, non-trivial PNG (signature-checked) with plausible dimensions and DPI, proven untouched (every `UNTOUCHED` counter stays zero -- capture is passive); two captures of the same still window yield the same `geometry_fingerprint`. All 49 real-UIA tests (S1-S5) pass. |
| TypeScript | `desktop-vision.test.ts`: 28 cases on the strict candidate parser -- well-formed and empty lists, code-fence stripping, malformed/non-object/extra-field/wrong-version payloads, more than 8 candidates, the same action-field-smuggling parametrization as the Python domain tests, region/confidence/label bounds, wrong candidate kind, prompt-injection-shaped text surviving as inert data, and the snake_case wire translation (`candidatesToWire`) omitting an absent `observedText` rather than sending `null`. `authenticated-planner.test.ts`: the router now lists six private classes and exactly one (`desktop_vision`) is vision-capable; every OTHER private class still refuses an image; `desktop_vision` accepts one and still tries only one provider; a non-vision-capable provider under `desktop_vision` is still refused (documents the boundary explicitly rather than assuming it). `desktop-firewall.test.ts`: extended with an S5-parallel "one reviewed path to a provider" test, an S5-parallel "kept out of voice" test, the exact nine-method/nine-channel renderer-bridge and IPC pins, a dedicated "no path from a candidate to a click/coordinate/action" scan of the controller/wire/panel, the widened `/desktop/captures/*` route allow/reject lists, and the widened error-code and `desktop_vision`-prefix mention lists (with an explanatory note: unlike `desktop_planning`/`desktop_action_planning`, `desktop_vision` is also a prefix of unrelated identifiers this slice adds on purpose -- the two grant kinds and two error codes -- so that one check's expected list is wider by design, not by accident). |

## 12. Adversarial review (Codex Sol) and confirmed fixes

Five findings, all confirmed against the current tree (not merely trusted from the review) and fixed with
a narrow change plus a regression test that would have failed before the fix. Per the project's Claude-only
policy for M9 S2 onward, the Sol RE-verification pass this section's process would otherwise end with (a
short follow-up asking Sol to re-check its own five findings against the fixed tree) was intentionally not
run; section 13's independent Claude review covers that verification role instead, plus its own fresh sweep.

**Finding 3 (High) -- human-input-takeover baseline read fresh at claim, not at the trusted click.**

*Original attack.* `claim_capture`/`claim_disclosure` called `input_baseline()` themselves, at claim time,
and compared the human-input tick against that freshly-read value. A freshly-read baseline trivially always
matches "now": the whole point of a human-input-takeover check -- proving nobody touched the machine
between the person's trusted approval and the runtime acting on it -- was defeated by construction, not by
a subtle bug.

*Fix.* The tick is now read exactly once, at the trusted confirmation click itself
(`DesktopVisionService.confirm_capture`/`confirm_disclosure`, outside any open transaction), and persisted
atomically as `task_grants.approval_input_tick` in the SAME compare-and-swap that already moves the grant
PENDING -> ACTIVE (`confirm_grant`). `claim_capture`/`claim_disclosure` read that stored, historical value
off the grant row -- never a fresh one -- and pass it through to the worker's own native check.
`desktop_captures`/`desktop_vision_disclosures` keep their own copy of the same value, but only as an
immutable audit column written at claim time; the authoritative fact lives on `task_grants`, set once, at
approval.

*Regression test.* `services/agent/tests/test_desktop_vision_service.py` (existing suite, extended);
`test_migrations_match_table_definitions` and the migration/table-definition tests in
`test_schema.py`/`test_migration_0013.py`/`test_migration_0015.py` prove the schema matches. 146 tests in
the targeted suite (`test_desktop_vision_service`, `test_desktop_effects`, `test_schema`,
`test_migration_0013`, `test_migration_0015`) pass.

*Disposition:* **Fixed.** *Residual:* none identified -- the property is now a database fact, not a
convention callers must remember to follow.

**Finding 4 (High) -- `capture_scope_certain` proved nothing; it only checked a clamp produced a nonzero
result.**

*Original attack.* The function computed `client_rect.clamp_to(window_rect)` and accepted any nonzero-area
result. A torn `GetClientRect`/`ClientToScreen` read that only PARTIALLY overlaps the window rect (not a
disjoint, already-caught zero-area case, but a client rect that genuinely sticks out past the window's own
edge) still clamps to a nonzero rectangle -- the function would report the crop "certain" even though the
clamp had to silently substitute a smaller, unspecified rectangle for the client rect that was never
proven to actually be the client area. The same gap meant a window with a corrupted or spoofed geometry
read landing entirely off every real monitor (a negative-origin overflow, for instance) was never checked
against real display bounds at all.

*Fix.* `capture_scope_certain` (`services/agent/app/desktop/dpi.py`) now requires: positive DPI; a
non-degenerate window rect AND client rect on their own, before any clamping; `capture_rect(snapshot)`
(the clamp) to equal the RAW `client_rect` unchanged -- a clamp that had to cut anything means the crop was
never proven, only corrected, and is refused; the result within `MAX_CAPTURE_DIMENSION` in both axes; and
positive overlap between the capture rect and the resolved monitor's own physical bounds (a window merely
PARTIALLY off-screen still overlaps and is accepted; a window nowhere near any real monitor is refused --
Windows itself never allows a visible top-level window to be fully off every display, so zero overlap is
evidence of corrupt or spoofed data, not a display layout this code must tolerate).

*Regression test.* `services/agent/tests/test_desktop_dpi.py`:
`test_a_client_rect_only_partially_inside_the_window_is_scope_uncertain`,
`test_zero_or_negative_dpi_is_scope_uncertain`, `test_a_degenerate_window_rect_is_scope_uncertain`,
`test_a_capture_rect_wider_than_the_maximum_dimension_is_scope_uncertain`,
`test_a_window_entirely_off_every_monitor_is_scope_uncertain`,
`test_a_window_only_partially_on_its_monitor_is_still_scope_certain` (the last one is a false-positive
guard: ordinary partially-off-screen use must still work). All pass, alongside the pre-existing dpi test
suite.

*Disposition:* **Fixed.** *Residual:* the overlap/dimension bounds are proof-of-geometry, not a full
render-correctness proof -- `capture_scope_certain` cannot see what `PrintWindow` will actually draw, only
that the numbers describing where to draw it are internally consistent and physically plausible.

**Finding 5 (Medium-High) -- geometry read once, never re-verified immediately before the native capture
call.**

*Original attack.* `effects.py`'s `capture()` already re-checks surface identity (`verify_unchanged`),
credential surfaces and human input a second time, immediately before the native `PrintWindow` call --
established S4 practice for exactly this class of TOCTOU gap. Geometry (position, size, monitor, DPI) was
the one input to that same call NOT re-checked: it was read once, at the top of the method, used to size
the bitmap and compute `capture_scope_certain`/the recorded `geometry_fingerprint`, and never touched
again. A window that moved, resized, or changed monitor/DPI in the gap between that first read and the
actual `PrintWindow` call (itself a separate, measurable-latency Win32 call sequence) would still pass
`verify_unchanged` (which only proves the SAME process/window, not the same geometry), and the capture
would proceed sized and labelled against stale numbers the approval never actually covered.

*Fix.* Immediately before the native call (in the same place the existing surface/credential/input
re-checks already sit), geometry is read a second time, built into a fresh `GeometrySnapshot`, and its
`geometry_fingerprint` compared against the one computed from the FIRST read. A mismatch refuses
`CAPTURE_SCOPE_UNCERTAIN` before any native call; the fingerprint is never silently recomputed and accepted
-- a change is always a refusal, never a reason to proceed on updated numbers the person never saw. This
covers both `claim_capture` and `claim_disclosure` (a brand-new capture attempt each), since both go
through this same `capture()` effect.

*Regression test.* `services/agent/tests/test_desktop_effects.py`:
`test_a_window_moved_between_the_two_geometry_reads_is_scope_uncertain` (scripts the SECOND geometry read
to return a moved window via a new `FakeCapturePlatform.on_geometry_call` hook; asserts zero native capture
calls) and `test_a_window_unchanged_between_the_two_geometry_reads_still_captures` (a false-positive guard:
an ordinary, unchanged capture still succeeds). Both pass alongside the full existing effects suite (208
tests in the targeted run).

*Disposition:* **Fixed.** *Residual:* the same irreducible gap the S4 review already documented for its
own second checks applies here too -- the native call still has to acquire its own device context and
perform the actual `PrintWindow` after this last check, which cannot itself be re-verified without
changing how the OS call is made. The unchecked window is now exactly that one call, not everything since
the first geometry read.

**Finding 6 (Low; test-coverage, not a code defect) -- `isCaptureBlocked()` re-check unverified by any
test.**

*Original attack (as reported).* Capture eligibility must be re-checked at the last responsible moment,
not only implied by the card having existed, since a sign-in takeover or an open form-draft window can
appear between card review and the actual claim.

*Finding on inspection.* Reading `src/main/services/desktop-vision-controller.ts` closely: `isCaptureBlocked()`
was ALREADY the last synchronous statement before each of the two claim network calls (`runDesktopCapture`
and `runDesktopVisionDisclosure`), with no `await` between the check and the claim being issued -- as tight
as JavaScript's single-threaded execution allows. `capture.ts`'s guard state (`takeoverGuard`/
`formDraftWindow`) is a live, synchronously-read module variable, not memoized or cached. No code defect
was found. What WAS missing: zero test coverage of this property anywhere -- `desktop-vision-controller.ts`
had no test file at all before this fix, so a future refactor could silently reorder or drop the check with
nothing to catch it.

*Fix.* No production code change. New `src/main/services/desktop-vision-controller.test.ts`, exercising
both claim methods with the blocked state flipped to `true` as a side effect of the GET that reads the
approved card back -- the latest possible moment before the claim -- and asserting zero claim calls, zero
OCR calls, zero provider `reason()` calls in the blocked case, and normal success in the unblocked case.

*Regression test.* `desktop-vision-controller.test.ts`, 4 cases, all pass; confirmed they fail if the
`isCaptureBlocked()` check is commented out (verified by temporarily removing it during this fix and
observing the new tests fail before restoring it).

*Disposition:* **Verified already fixed; regression coverage added.** *Residual:* the same irreducible
class of gap already acknowledged for `effects.py`'s own second checks -- the guard's answer can still
become stale during the async network round-trip to the runtime/worker itself, which no synchronous
in-process re-check can close. `isCaptureBlocked()` is the ambient-environment guard (sign-in/form-draft
windows); it is layered ON TOP OF, not instead of, the grant's own server-side ACTIVE/not-expired
compare-and-swap, which is the authoritative consent-revocation check.

**Finding 8 (Medium; consent-integrity/UX, not a data leak) -- disclosure card never showed the target
application/window.**

*Original attack.* The disclosure card (`DesktopVisionPanel.tsx`, `awaiting_disclosure_approval` phase)
named the recipient provider/model and showed the person's typed purpose, but never said WHICH
application or window was about to be captured and sent -- a person approving it had no way to distinguish
"share an image of VS Code" from "share an image of some other window" without trusting page-generated
text elsewhere in the flow. The underlying data was already correct and already flowed end to end:
`AgentDesktopDisclosureCardView.applicationLabel`/`.windowTitle` were already on the wire (from
`DiscloseGrantScope`'s own "display target" field, section 6), and the controller already destructured
`applicationLabel` from the card (`desktop-vision-controller.ts`) -- the renderer alone never displayed it.

*Fix.* The disclosure card now renders a `desktop-vision-disclosure-target` element naming the application
and window, rendered exactly like the capture card's own existing target line: inert text inside a `<q>`/
`<bdi>` pair with a visually-hidden "text from the application, not from Lumi" label, so a hostile window
title can never be mistaken for app-authored copy or affect a button.

*Regression test.* New `src/renderer/src/components/DesktopVisionPanel.test.tsx`, 4 cases: the disclosure
target renders and is distinct from the recipient line; a hostile window title (`<script>`/`<b>Approve</b>`
content) renders only as escaped, inert text and never appears inside a `<button>`; two different targets
(`VS Code` vs `Notepad`) produce visibly distinct rendered output, so a reviewer can actually tell them
apart. All pass; the pre-existing capture-card target test also confirmed as a baseline.

*Disposition:* **Fixed.** *Residual:* none identified for the rendering itself. As with every other trusted
card in this codebase, the person's own attention is still the final control -- inert, correctly-escaped
text cannot force a click, but it also cannot make someone read it.

## 13. Independent Claude review

A separate, fresh Claude session (no context from the work above) performed an adversarial sweep of the
whole S5 slice against the eleven-vector attack list below, independent of section 12's findings. It was
explicitly told not to re-check findings 3-8, only to sweep for NEW, concrete issues.

**Finding (High) -- Finding 5's own fix reopened a human-input-takeover TOCTOU gap.** `effects.py`'s
`capture()` re-checks human input a second time immediately before `verify_unchanged`/the fresh-geometry
re-read (Finding 5's fix), but that fresh `geometry()` call is itself a real Win32 call sequence
(`GetWindowRect`, `GetClientRect`, `ClientToScreen`, `EnumDisplayMonitors`) with its own measurable
duration -- so by the time execution reached the actual native `PrintWindow` call, the human-input check
had last run BEFORE that extra work, not immediately before the pixel capture, the way every other second
check in this method (and S4's own mutations) is supposed to. A person who started touching the machine
during that fresh-geometry read would not have been caught.

*Fix.* A third `_refuse_if_human_input` call now runs immediately after the fresh-geometry fingerprint
comparison and immediately before the native `capture()` call -- closing the reopened window the same way
S4's own mutations close theirs (narrowed to just that one call, not eliminated to zero, since the native
call still has to acquire its own device context afterward).

*Regression test.* `test_human_input_between_the_second_geometry_read_and_the_native_call_stops_capture`
(`services/agent/tests/test_desktop_effects.py`), using the `on_geometry_call` hook Finding 5's own test
added to mutate the input tick during the SECOND geometry read; asserts `HUMAN_INPUT_DETECTED` and zero
native capture calls. Confirmed to fail without the fix and pass with it. Re-verified independently in this
session: `uv run mypy app tests` clean (239 files); the targeted suite (`test_desktop_effects`,
`test_desktop_dpi`, `test_desktop_vision_domain`, `test_desktop_vision_service`, `test_desktop_source`,
`test_schema`) -- 376 passed.

*Disposition:* **Fixed.** This protects both `claim_capture` and `claim_disclosure`, since both call the
same shared `capture()` effect. *Residual:* the same irreducible "native call still has to acquire its own
device context" gap already documented for Finding 5 and for S4's own second checks.

**Noticed, deliberately not fixed (documented, not silently accepted):**

1. `desktop_vision_disclosures` has no CHECK constraint requiring the frame fields
   (`geometry_fingerprint`/`frame_digest`/`width`/`height`/`dpi`/`monitor_id`) to be non-null when
   `status = 'SUCCEEDED'`, unlike `desktop_captures`'s own `frame_fields_when_succeeded` constraint. Traced:
   the only path to `SUCCEEDED` (`record_candidates` -> `finish_disclosure_succeeded`) requires a
   `disclosure_id` that is only ever released to a caller AFTER `claim_disclosure` has already committed a
   `record_frame` write in its own transaction -- so the gap is not reachable through the current
   architecture. A data-integrity belt-and-suspenders omission, not an attack path; closing it is a schema
   change outside a narrow fix and is left for a future migration, not this review.
2. The OCR-to-disclosure-prompt wiring section 4 originally described is not actually implemented --
   corrected in section 4 above. Actual behavior sends strictly LESS to the provider than documented, never
   more; not a security finding.

**Vector-by-vector result:**

| # | Vector | Result |
| --- | --- | --- |
| 1 | Credential/elevated/Lumi capture | Clean -- shared `SurfaceTable.resolve` trust checks (Lumi-ancestry exclusion, `DENIED_IMAGES`, integrity ceiling) plus `capture()`'s own fresh whole-tree credential re-scan, run twice. |
| 2 | Wrong-window crop | Clean -- `hwnd` threaded end to end from `resolved.identity.hwnd`; `verify_unchanged` re-proves identity before AND after the native call; `PrintWindow(PW_CLIENTONLY)` renders only the target's own client content; dimensions come from a client rect Finding 4 now proves already equals the window's own client rect. |
| 3 | Stale frame beyond Finding 5 | Clean -- the geometry-fingerprint re-check is sound; nothing further found at the geometry/frame level (the one gap found was a human-input timing issue, folded into the finding above). |
| 4 | Human takeover | **Real finding** -- fixed above. |
| 5 | Capture-consent race | Clean -- `TaskRepository.lock_task` (`SELECT ... FOR UPDATE`) serializes confirm/claim/revoke per task; every grant compare-and-swap conditions on status + revision (+ expiry for claim), so a revoke racing a claim can win at most one of the two writes, never both. |
| 6 | Geometry/DPI change beyond Finding 4/5 | Clean -- DPI awareness set once, idempotently, at backend construction; `geometry_fingerprint` now covers window/client rects, monitor id AND monitor rect, DPI and process identity; no raw geometry value crosses the DB/wire boundary unchecked. |
| 7 | Provider substitution/failover | Clean -- shared, already-reviewed S2/S4 router infrastructure (`permits` bound to the exact approved recipient+model; one attempt only); `desktop_vision` confirmed by grep as the only call site ever setting `taskClass: 'desktop_vision'`; `runDesktopVisionDisclosure` double-checks `canServe` pre-claim and the claimed grant's own recipient/model post-claim. |
| 8 | Text approval reused for image, or vice versa | Clean -- grant `kind` is a Pydantic `Literal` filtered on every repository query; `CaptureGrantScope` structurally has no `recipient`/`model` field at all. |
| 9 | Second image reuse | Clean -- `desktop_captures.task_id`/`desktop_vision_disclosures.task_id` are both DB `UNIQUE`; terminal task-state transitions block further grant creation; `claim_disclosure` always performs a brand-new native call with a brand-new `capture_id`, never referencing the original capture's bytes. |
| 10 | Raw frame leakage | Clean (within this slice's own code) -- DB columns structurally cannot hold a pixel; `image_base64`-carrying types are only ever consumed inside `DesktopVisionController`, never returned through IPC to the renderer; the validation-error handler returns a fixed message, not an echo of the rejected body. Not exhaustively re-checked: every log statement in the whole codebase beyond the paths this slice's own code touches. |
| 11 | Vision -> coordinate action escalation | Clean -- both candidate schemas (Python `extra="forbid"`, TS strict parser) are closed with no click/coordinate/action field possible; the renderer renders candidates as inert text with no click handlers; no code path found reading a candidate's region/label into any S4 action input. |

Sanity-check items also confirmed: `desktop_vision` is genuinely the only `PRIVATE_VISION_TASK_CLASSES`
member and the only `taskClass: 'desktop_vision'` call site in `src/`; `max_provider_calls: Literal[1]`/
`failover: Literal["none"]` are enforced (not just typed) by the router's one-attempt loop plus the DB
`UNIQUE` constraint plus terminal task-state transitions together; the source scanner genuinely pins
`PrintWindow`/`GetWindowDC`/`GetDIBits` to `capture_win32.py` only, with `BitBlt`/`GetDC`/`mss`/
`PIL.ImageGrab`/`cv2`/`pytesseract` still forbidden everywhere, including inside `capture_win32.py` itself.

## 14. Validation

Re-run after every finding in sections 12 and 13 was fixed (this is the FINAL post-fix validation, not
the pre-review pass). Each Python DB-backed suite run alone, per this project's shared-database
constraint; no two DB-backed commands (pytest, `npm run eval`) ever run concurrently.

| Command | Result |
| --- | --- |
| `uv run mypy app tests` | Clean: 239 source files, no errors |
| `uv run pytest -m "not browser"` | 2193 passed, 1 failed, 323 deselected. The one failure is `test_booking_routes_without_a_worker_answer_503` -- the same pre-existing M2/booking baseline S4's own review documented (an error-code-text drift: `browser_worker_unavailable` vs. `browser_worker_not_configured`), not an S5 regression. |
| `pytest -m desktop_uia` (real Windows, this machine) | **49 passed** in isolation. One run of the full matrix (with this suite following immediately after `npm run eval`) showed 1 additional failure, `test_focus_brings_a_background_window_to_the_front_through_ui_automation_only` (S3's own focus test, unrelated to S5) -- `HUMAN_INPUT_DETECTED`, because a real person (this session's own operator) was actively using the keyboard/mouse on this machine while the real-hardware suite ran. Re-run alone immediately after: passed. Matches this project's own documented real-hardware caveat (`GetLastInputInfo` ticks on any real input; a real-window test needs a quiet input generation) -- not a regression. |
| `npm.cmd run typecheck` | Clean |
| `npx vitest run` | 2367 passed, 22 skipped (deliberate), 1 failed + 3 test files errored, all four pre-existing and unrelated to S5 (`real-inference.test.ts`, `tokenizer-pack.test.ts` -- environment/network-dependent, predating M9 entirely; `accessibility.test.tsx` -- the same pre-existing baseline S4's review already documented). `realtime.test.ts`, listed as a fourth pre-existing failure in an earlier pass of this same table, did not fail in this final run. |
| `npm.cmd run build` | Clean: main/preload/renderer all build |
| `npm.cmd run eval` | **118/118** eval cases passed (unchanged from S4: S5's evidence-only vision path is not part of the conversational agent-eval harness) |
| `npm.cmd run package:dir` | **Fails on this machine, for a reason entirely outside this codebase.** The build itself, and the new migration-0016/seven-new-file bundle-presence checks `build-agent-runtime.mjs` gained for S5, all succeed; the failure is in the byte-compile step (`compileall`), which fails trying to compile a THIRD-PARTY file bundled inside the embedded Python distribution's own `site-packages` (`win32com\test\errorSemantics.py`, a pywin32 test fixture, never imported by Lumi's own code). Root-caused: `import unicodedata` on this machine's freshly-extracted embedded Python raises `ImportError: DLL load failed while importing unicodedata: An Application Control policy has blocked this file` -- this machine's Windows Application Control / endpoint-security policy is blocking `unicodedata.pyd`, a stdlib extension module, from loading at all. `scripts/build-agent-runtime.mjs`'s own diff for S5 only adds bundle-presence assertions; it does not touch Python-distribution fetching or pruning. This is a local machine/security-policy blocker, not a code defect, and not fixable from within this repository -- flagged here rather than silently worked around or claimed green. |

**Browser regression suite:** not re-run, following S3/S4's own precedent. `uv run pytest -m "not
browser"` already re-ran every non-browser suite touching the action ledger, task grants and recovery
paths and found nothing broken; S5 added two new tables and one new worker route, none of which any
browser-path code touches.

## 15. Package impact

No new native dependency and no new executable; see `docs/PACKAGING.md`'s S5 section for the exact
bundle-verification checks `build-agent-runtime.mjs` gained.

## 16. Authenticode status

Unchanged: signing infrastructure READY, production certificate NOT CONFIGURED, real-account release
BLOCKED.

## 17. Residual risks (honest)

* **Local OCR ships as a pluggable interface, not a shipped backend.** `runLocalOcr` only calls a real
  `LocalOcrEngine` if the person has already installed the optional "extras" pack; this slice proves the
  boundary (bounded, local, discarded, never sent anywhere) end to end, not OCR quality or availability
  by default. Wiring a bundled OCR backend by default is real future work, tracked separately, and was a
  deliberate scope decision to avoid adding a new default-installed native dependency mid-slice.
* **The safe-Invoke-style heuristics this slice reuses are unchanged and still English-centric**: the
  file-picker title match and credential-name heuristic S1/S4 already documented as residuals are
  untouched by S5 and inherited as-is by the credential re-scan `capture()` reuses.
* **`classify_fallback_eligibility`'s thresholds (`_MIN_RECOGNISABLE_NODES`, `_UNKNOWN_FRACTION_THRESHOLD`)
  are reviewed but heuristic**, matching the project's own precedent for S1's credential-name matching
  and S2's title-withholding heuristic: a false negative (no fallback offered when one might have
  helped) is the safe direction to be wrong in, and is the only direction this function can be wrong in
  by construction (nothing downstream can force a capture without it returning a reason first).
* **`InvokePattern`-style "prove the positive path end to end on real hardware" gap, mirrored here as
  the disclosure-candidates path**: the real-hardware suite proves capture (`PrintWindow`) end to end
  against a real window, but proving a real PROVIDER call end to end was out of scope for this slice
  (it would need a real, configured API key and a real network call in the test suite, which S2/S4 also
  do not do for their own provider calls) -- the reasoning/parsing layer is thoroughly proven against
  fakes (28 TS cases, 31 Python domain cases) but not against a live provider response.
* **No installed, production-signed validation.** One framework (Win32/UIA fixture) on this machine,
  matching every prior slice's own residual.
* **S4's own deferred residual is untouched by S5, not silently fixed.** `docs/reviews/milestone-9-s4.md`
  sections 8, 13 (finding 3) and 19 document a confirmed SECOND, durable copy of a set-value's raw value
  living in the ordinary action ledger (`actions.proposal`), outside the immutable plan-grant scope the
  brief's own wording permits it in -- no currently-reachable code path was found that leaks it from
  there, so it was deliberately deferred rather than fixed in S4. S5 never touches `DesktopActionService`,
  `desktop_dispatches`, `actions`, or the S4 planner/proposal path at all, so this residual is carried
  forward unchanged and still open for the final, whole-M9 cross-slice review to decide on -- it is
  recorded here only so that review does not have to rediscover it.

## 18. Confirmation

**M10 was not started.** No coordinate, no keyboard, no mouse, no `SendInput`, no hotkey, no drag, no
clipboard, no shell, no generic screenshot route and no way to turn a vision candidate into a click or a
keystroke exist anywhere. The model never authorizes a capture or a disclosure; the renderer never
decides the crop, the provider, or what a candidate means.
