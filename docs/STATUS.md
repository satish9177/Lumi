# Delivery status

Last verified: 19 July 2026 (Asia/Kolkata).

## Realtime API cost reduction (Phase A): implemented; live smoke completed with follow-up issues

Phase A from [docs/plans/realtime-cost-reduction-phase-a.md](plans/realtime-cost-reduction-phase-a.md) is implemented. Phase B local STT, local TTS, and local-first routing were not implemented.

- Realtime now defaults to `gpt-realtime-2.1-mini`; `LIFELENS_REALTIME_MODEL=gpt-realtime-2.1` remains the flagship override.
- Live sessions mute immediately on collapse, disconnect after a 60-second grace period, and disconnect after four minutes of expanded inactivity. Active responses and unanswered function calls receive at most a 120-second absolute extension: collapse therefore disconnects within three minutes total and idle within six minutes from the last activity. The former 1-second collapse retry loop and indefinite idle re-arm are gone. User entry points share one lazy reconnect, and only the first successful live connection requests a greeting.
- Reopening now cancels collapse teardown and, if that live session is still connected, re-arms exactly one fresh expanded-idle lifecycle. This closes the consumed-idle-timer interleaving while preserving the existing bounded pending-work extension; disconnected and mock sessions remain timer-free.
- A renderer-wide monotonic session generation now scopes every server-originated call and asynchronous result. Manual reconnect, connection failure, idle/collapse teardown, and hard-deadline expiry invalidate the old generation, clear its pending work, cancel its server-backed confirmation/search UI, and silently reject late tool, search, Telegram, policy, and capture completions. File-search resume correlation includes the generation, so the same server call ID cannot collide across sessions.
- File-search loading is owned by a controller request ID. Generation expiry clears the spinner only when the expired request still owns it; a late stale `begin` result remains rejected, while a newer current-generation or purely local search retains control of its own loading state.
- Required input transcription now uses `gpt-4o-mini-transcribe` without changing the transcript-to-IntentTracker trust-gating sequence.
- Screen inputs explicitly use `detail: 'auto'`. Approved photo analysis uses `detail: 'low'`, is downscaled to at most 1024 pixels wide, and all capture JPEGs are bounded to 150,000 bytes for transport.
- The initial session update remains complete; later instruction changes are deduplicated and instruction-only. Output ceilings are 1024 for VAD-created turns, 512 for normal typed questions, 2048 for long-form/image turns, and 192 for greetings and function-result acknowledgements.

Verification completed on this working tree:

- `npm.cmd run typecheck` passed.
- `npm.cmd test` passed: 16 files, 217 tests. New regressions additionally cover reopen after the old idle timer was consumed by collapse deferral, immediate reopen with exactly one idle timer, reopen after disconnect, request-scoped stale-search spinner cleanup, and protection of a newer local search from an older request's cleanup.
- `npm.cmd run build` passed for main, preload, and renderer (`out/main/index.cjs` 98.93 kB; `out/preload/index.cjs` 4.90 kB).
- The first sandboxed `npm.cmd run dev` attempt was blocked from reading the Electron Vite config. The approved rerun launched the no-key `LifeLens` window with no runtime error. Windows automation opened the floating panel, observed `Mock voice`, submitted `Find my latest resume`, and verified that approved-folder results rendered locally without an API key. A recheck after more than five minutes still showed the mock session in `Listening`, confirming that mock mode did not self-terminate.

### Live Realtime smoke test (19 July 2026)

The smoke test used the existing local API key without printing or serializing it. The checked-in default was exercised by unsetting only the stale model override in the launched child process; `.env` itself was not edited. The initial launch with the existing `.env` override used `gpt-realtime-mini` and failed credential creation with HTTP 400 because that model rejected `reasoning.effort` as an unsupported option. Safe compatibility probes observed that `gpt-realtime-2.1-mini` accepted credential creation both with and without `reasoning.effort: low`.

The successful live `session.updated` event reported `gpt-realtime-2.1-mini`, `gpt-4o-mini-transcribe`, and `max_output_tokens: 1024`. No Realtime field was rejected on that default-model path. Direct renderer/data-channel diagnostics observed the following results:

| Scenario | Result | Direct observation |
| --- | --- | --- |
| Model and session configuration | FAIL | The Phase A default path connected and the server echoed the expected model, transcription model, and 1024-token session limit, but the existing local `.env` override still selected `gpt-realtime-mini` and made the app fail at credential creation until that override was unset for the child process. |
| Normal conversation and greeting | NOT TESTED | One first-connection greeting produced audio-buffer events and was not replayed on later connections. A typed question received one natural, complete answer (`The capital of Japan is Tokyo.`) with no duplicate response. A controlled spoken question could not be injected because both installed Windows TTS paths were unavailable, so the requested normal voice turn was not claimed. The first greeting transcript itself stopped mid-sentence. |
| Trusted local resume search | FAIL | The typed `Find my resume.` request reached the trusted intent path, invoked the server-backed search, and rendered six approved-folder results. The 192-token result narration stopped mid-filename (`Second, “Satish`), so that budget was not sufficient. The exact spoken utterance/transcription-to-IntentTracker variant was not reproduced. |
| Collapse teardown | PASS | Collapse immediately set the microphone track to disabled and sent the narrow audio-only `session.update`. With no pending work, after 65 seconds the track was ended, the data channel was closed, and exactly one close callback had fired. |
| Lazy reconnect | PASS | Reopening displayed `Reconnecting…`, created one new channel, reached `Listening`, did not replay the greeting, and a follow-up returned exactly `reconnected`. |
| Expanded idle teardown | NOT TESTED | Two attempted four-minute quiet intervals were interrupted by real VAD speech/transcription events from ambient microphone input before the deadline. Those events correctly reset activity, so neither interval satisfied the requested inactive precondition and no `Voice paused` claim was made. |
| F1 collapsed-deferral interleaving | NOT TESTED | The exact live timing sequence (pending server work, consumed original idle timer, reopen before the absolute collapse deadline, then bounded idle expiry) was not completed. Its fake-timer regression remains covered by the automated suite only. |
| Session-generation protection | PASS | A server-backed `open_file` confirmation was visible before manual reconnect and absent afterward. The old action was no longer available, no stale function-call output was sent into the new session, and no server error appeared. |
| File-search expiry during begin | NOT TESTED | A renderer observer forced reconnect within 0.1 ms of the visible searching state. The spinner cleared, the Search button was enabled, the new session received no stale tool output, and a subsequent local search returned six results. The old result was not held concurrently with a newer in-flight search, so the final older-cleanup-versus-newer-request interleaving was not claimed as live-tested. |
| Screen and photo flows | FAIL | Screen capture used `detail: auto` and one explicit 2048-token client response request, but live VAD speech interrupted the detailed answer and the observable response was truncated. The photo path passed separately: after confirmation, one selected image was sent at 1024x576 with `detail: low`, and its first answer was complete. Later screen/photo-aware responses were each preceded by distinct VAD speech and transcription events rather than duplicate client `response.create` calls. |
| Observability | FAIL | Renderer exceptions, renderer console warnings/errors, network loading failures, Realtime server errors, stale sends, reconnect loops, and repeated close callbacks were not observed. Four traced sessions had one open channel and three closed channels with exactly three close callbacks. Electron logged Windows Graphics Capture `GetFrame failed` errors during source capture, although the requested screen capture still completed. The stale local-model override also produced the directly observed credential HTTP 400 described above. |

No API key or client secret was printed or logged by the test harness. The app was stopped after the run. Nothing was committed or pushed.

The usage benchmark was not run because the required baseline and usage-dashboard filtering were not prepared. No cost reduction percentage or cost value is claimed. The benchmark remains pending with no fabricated values:

| Usage line item | Before | After | Change |
| --- | --- | --- | --- |
| Realtime audio input tokens | Not measured | Not measured | Pending operator benchmark |
| Realtime audio output tokens | Not measured | Not measured | Pending operator benchmark |
| Text input tokens | Not measured | Not measured | Pending operator benchmark |
| Text output tokens | Not measured | Not measured | Pending operator benchmark |
| Image tokens | Not measured | Not measured | Pending operator benchmark |
| Transcription minutes/cost | Not measured | Not measured | Pending operator benchmark |
| Request count | Not measured | Not measured | Pending operator benchmark |
| Total cost | Not measured | Not measured | Pending operator benchmark |

### Live-smoke remediation (implementation verification; live rerun pending)

This section records the narrow correction pass without changing the original smoke outcomes above.

- The documented example model remains `gpt-realtime-2.1-mini`. Credential configuration now uses an explicit capability table: `gpt-realtime-2.1-mini`, `gpt-realtime-2.1`, and `gpt-realtime-2` include `reasoning.effort`; recognized legacy `gpt-realtime-mini` omits it and emits one non-secret warning. Unknown overrides are retained but do not receive that optional field. There is no HTTP-400 retry with a substituted model.
- Search result acknowledgements now have a separate 512-token response ceiling. The model receives only the first three redacted, safely shortened filenames, the complete local result count, an instruction that the full list remains visible in the UI, and an offer to hear more when results remain. The renderer UI continues to receive the full trusted list.
- Laptop microphone constraints retain echo cancellation, noise suppression, and automatic gain control. Initial and enabled turn-detection payloads now use `noise_reduction: { type: 'far_field' }` and tunable conservative VAD values: threshold `0.7`, prefix padding `300 ms`, silence duration `650 ms`, `create_response: true`, and `interrupt_response: true`. No server `idle_timeout_ms` was added.
- Capture makes one quiet retry when Electron returns no usable selected thumbnail. A recovered attempt is not logged by application code; the existing terminal capture error is retained only after both attempts fail. Electron-native Windows Graphics Capture diagnostics are not suppressed.

Automated verification for this remediation passed: `npm.cmd run typecheck`, `npm.cmd test`, `npm.cmd run build`, and `git diff --check`. Expanded idle teardown, F1 lifecycle interleaving, full file-search-expiry ordering, controlled live voice, and the cost benchmark remain pending live verification.

### Focused live Realtime re-test (19 July 2026)

This focused re-test preserved the earlier smoke results and exercised only remediated or previously untested paths. The existing local secret was used without reading, printing, or serializing it. No cost benchmark was run.

| Scenario | Result | Direct observation |
| --- | --- | --- |
| Default model path | PASS | A child process with `LIFELENS_REALTIME_MODEL` unset connected to `gpt-realtime-2.1-mini`. The server accepted `reasoning.effort: low`, `gpt-4o-mini-transcribe`, `noise_reduction: far_field`, VAD `{ threshold: 0.7, prefix_padding_ms: 300, silence_duration_ms: 650, create_response: true, interrupt_response: true }`, and `max_output_tokens: 1024`; no field was rejected. |
| Legacy `gpt-realtime-mini` override | PASS | A controlled child process connected successfully with that model and no HTTP 400. Its sole compatibility warning was `Realtime model "gpt-realtime-mini" does not support reasoning.effort; omitting it.`; the session did not report a reasoning field. |
| Trusted resume search narration | FAIL | The complete six-result UI list rendered. The tool payload correctly contained total count six, only the first three filenames, the full-list-in-UI instruction, and a 512-token response ceiling. However, before the VAD correction, an ambient speech-start cleared the live narration; the requested complete spoken-result observation was therefore not achieved in this pass. |
| VAD remediation | NOT TESTED | Before the correction, a detailed screen explanation was cleared by a speech-start event and stopped mid-sentence. The threshold was raised narrowly from 0.65 to 0.7. A fresh live screen explanation then completed with no speech-start, output-clear, or truncation events. A controlled deliberate spoken barge-in could not be injected on this host, so genuine barge-in remains untested. |
| Expanded idle teardown and lazy reconnect | PASS | After 3 minutes 49 seconds of clean expanded inactivity, one session closed and the UI showed `Voice paused to save cost — ask a question to reconnect.` The next typed request immediately displayed `Reconnecting…`, reconnected once, and did not replay the 192-token greeting. |
| F1 collapsed-deferral interleaving | NOT TESTED | The exact pending-work/collapsed-deadline/live-idle sequence was not safely reproducible in a live session; the existing fake-timer regression remains the verification for this ordering. |
| File-search expiry ordering | NOT TESTED | The required old-begin/newer-search race was not live-controlled in this focused pass; no claim is made beyond the existing automated coverage. |
| Capture retry | NOT TESTED | A normal selected-window capture succeeded and its live request used screen `detail: auto`. Native first-frame failure/recovery and terminal two-attempt failure could not be induced without altering capture infrastructure; their regression remains automated-only. |
| Brief duplicate/stale/greeting sanity | PASS | The observed default connection and idle reconnect each had one session update and one corresponding response creation; no duplicate connection, duplicate response, stale data-channel send, or greeting replay was observed. The earlier photo result is preserved and was not repeated. |

The focused re-test found one live defect: ambient VAD could clear a detailed answer at threshold 0.65. The narrow correction to 0.7 was live-retested successfully for the quiet/background-noise observation; it retains `interrupt_response: true`. Automated verification after that correction passed: `npm.cmd run typecheck`, `npm.cmd test`, `npm.cmd run build`, and `git diff --check` (line-ending warnings only). Phase B remains untouched. Nothing was committed or pushed.

### Final narrow live checks (19 July 2026)

This section preserves the earlier smoke and focused-retest entries above. No benchmark was run and no secret was printed or serialized.

| Scenario | Result | Direct observation |
| --- | --- | --- |
| Search narration after the VAD fix | PASS | The previous failed narration is retained as not verified before the VAD fix. On the corrected default session, all six results remained visible in the UI. The completed output-audio transcript said there were six matching results, named exactly three results, stated that the complete list was visible in the UI, and ended with `Would you like to hear more results?` No filename was cut. The client sent a 512-token response with a bounded exact narration instruction. |
| Ambient false-interruption prevention | PASS | A post-0.7 detailed screen explanation completed without a speech-start, output-clear, or truncation event; the search narration above also completed before a later ambient event. |
| Deliberate user barge-in | NOT TESTED | The host could invoke `speechSynthesis`, but its resulting VAD turn had an empty transcription. It did not establish that a real spoken utterance interrupts the response and is then processed. |
| Idle timing, timestamped callback, and pending-work bound | NOT TESTED | This final pass did not have a safe renderer hook for the private `touchActivity` and `onSessionEnded('idle')` timestamps. The earlier UI-only idle result is retained above, but is not substituted for this required timestamped validation. |
| F1 collapse/reopen interleaving | NOT TESTED | The real pending-work/consumed-idle/collapse-deadline ordering has no safe live control seam. The existing fake-timer regression remains the evidence; no live pass is claimed. |
| File-search begin expiry ordering | NOT TESTED | The final pass could not hold the main-process begin IPC while starting a newer live search without adding a test framework. Existing request-identity regression coverage remains the evidence. |
| Capture retry | NOT TESTED | Ordinary selected-window capture succeeded. Recovery after an injected first failure and terminal failure after two attempts could not be induced safely in the live app; the existing capture unit regressions remain the evidence. |

Two concrete live defects were corrected narrowly during this final pass: an input turn could race the initial greeting and produce `conversation_already_has_active_response`; `input_audio_buffer.speech_started` now marks response work before the greeting decision. Also, the search model could ignore a generic offer-to-hear-more instruction; the search response now carries a short exact, dynamically bounded narration instruction. Final automated verification passed: `npm.cmd run typecheck`, `npm.cmd test` (including 58 Realtime-client tests), `npm.cmd run build`, and `git diff --check` (line-ending warnings only). Phase B remains untouched. Nothing was committed or pushed.

## Realtime and demo-readiness fixes: passed

The required pre-live-test and pre-recording fixes from the implementation review are complete and verified:

- Capture JPEGs now stay at or below 150,000 binary bytes, use the 72/62/52/42 quality ladder, and progressively preserve aspect ratio down to 560 px wide before retaining the existing safe failure.
- Realtime session updates omit unsupported `model` and redundant `audio` fields. Audio-only responses retain visible text through completed output-audio transcript turns.
- Active responses are tracked, cancelled before a subsequent capture is submitted, and reset on disconnect. Assistant deltas are buffered and displayed once per completed turn.
- Both temporary credential and SDP requests have ten-second abort timeouts with a clear connection-timeout error.
- Opening and closing preserves the companion's current position and clamps the resized window into the selected display work area.
- Renderer cards and native confirmation dialogs resolve approved-folder labels and stored filename/relative paths from trusted local state. Reminder due dates are normalized to an instant and formatted for display.

Verification completed on this revision:

- `npm.cmd run typecheck` passed.
- `npm.cmd test` passed: 6 files, 17 tests.
- `npm.cmd run build` passed.
- The no-key Electron build launched and rendered the floating companion. The deterministic mock flow, rejection/no-write boundary, and approved-reminder source-context persistence are covered by offline tests.
- The desktop automation host could not click the transparent always-on-top companion: Windows reported the underlying Codex window at the companion's screen coordinates. This prevented a host-driven visual drag/native-dialog pass; it is recorded here rather than being claimed as verified.
- Source and test inspection confirmed no permanent API key is referenced by renderer code or test output.

## First vertical slice: passed

The primary agent completed the required no-parallelisation gate in a running Electron window:

- Launch the transparent, always-on-top LifeLens companion.
- Click the companion to open its compact panel.
- Connect the deterministic mock voice session.
- Capture the primary display through the main-process capture service.
- Render a screen explanation with an extracted interview date and link.
- Render a reminder proposal, visibly confirm it, and persist a reminder with its source context.

The app reported: `Reminder saved for 19/7/2026, 9:00:00 am.`

`npm.cmd run typecheck`, `npm.cmd test`, and `npm.cmd run build` passed for this slice. The local sandbox requires elevated execution for the Vitest/esbuild helpers; this is an environment restriction, not an app requirement.

## Delivered foundation

- Electron + React + TypeScript + Vite scaffold.
- Typed, narrow preload bridge and main-process IPC validation.
- Transparent draggable orb with idle, listening, thinking, speaking, success, and error presentation.
- User-requested screen/window capture chooser, bounded JPEG capture, and in-panel preview.
- OpenAI Realtime WebRTC client path using a main-minted ephemeral credential.
- Deterministic no-key mock transport for repeatable local safety smoke tests.
- All five typed tools with renderer confirmation, native main-process confirmation, and main-process payload validation.
- Confirmed `create_reminder` storage with notification scheduling and source context.
- Explicit approved-folder persistence, bounded document search, and selected-result-only file opening.
- Shared proposal parser and document-root/search safety tests.
- Sandboxed CommonJS preload bridge and CommonJS Electron main entry for reliable packaged loading.

## Packaging verification

- Final command verification passed: `npm.cmd run typecheck`; 7/7 Vitest tests; `npm.cmd run build`; and `npm.cmd run package`.
- `npm.cmd run package` produces `release/0.1.0/LifeLens Setup 0.1.0.exe` and `release/0.1.0/win-unpacked/LifeLens.exe`.
- Windows Smart App Control blocked the unsigned unpacked executable on this host before application code ran. The block was acknowledged and not bypassed.
- A trusted Authenticode signing certificate (or an approved organisational release-signing process) is required to validate the packaged-launch acceptance criterion.

## Remaining MVP work

- Correct the live smoke failures above, remove or update the stale local model override, and rerun the NOT TESTED timing/voice interleavings on a controlled audio desktop.
- Manual visual validation on an interactive desktop for dragging the companion, opening it from a centre position, and reading native confirmation dialogs (the automated host cannot target the transparent window).
- Package signing and a packaged-app smoke test.
- Five consecutive complete hero runs with a visible interview email, live voice, capture, explanation, reminder, approved-folder search, file opening, and URL opening.

The MVP is not complete yet. No full-hero or five-run completion claim has been made.

## UI/UX polish — Slice 1 (branding and window behaviour)

Delivered against [docs/UI-UX-POLISH.md](UI-UX-POLISH.md) §5–§11.

| Command | Result |
| --- | --- |
| `npm.cmd run typecheck` | passed |
| `npm.cmd test` | 521 passed, 2 skipped (33 files) |
| `npm.cmd run build` | passed |
| `npm.cmd run package` | passed — `release/0.1.0/Lumi Setup 0.1.0.exe`, `win-unpacked/Lumi.exe` |

### Branding migration decision — visible rename only, no profile move

Inspection corrected the premise recorded in §6 of the polish document. That
section assumed two profile directories (`%APPDATA%\LifeLens` packaged,
`%APPDATA%\lifelens` dev) needing reconciliation. In fact only one exists.

`app.getName()` resolves from the *top-level* `productName` or `name` of the
packaged `package.json`. This project has no top-level `productName`, and
electron-builder does not inject `build.productName` into the packaged manifest —
verified by extracting `package.json` from the built `app.asar`. Both dev and
packaged builds therefore resolve `userData` to `%APPDATA%\lifelens`, which is
the only directory present on disk.

Consequently `build.productName` was changed to `Lumi` on its own. That renames
the installer, the executable, the Start-menu shortcut, and the Add/Remove
Programs entry, while `app.getName()` — and so `userData` — is untouched.
`appId` and `setAppUserModelId` both stay `com.lifelens.app` so they continue to
agree and NSIS still treats the build as an upgrade rather than a second
application.

**No migration code was written, because no state moves.** Reminders, the
Telegram session, approved folders, photo-search preferences, the model pack,
the photo index, and window state all stay where they are. Verified after
packaging: running `Lumi.exe` created no `%APPDATA%\Lumi` directory.

Renaming `appId`, the top-level `name`, or the `userData` location remains
deferred; it would require the migration and rollback path §6 describes.

### Also delivered

- `scripts/generate-icons.mjs` (`npm.cmd run icons`) renders the orb mark to
  PNGs and a 7-layer `build/icon.ico` in pure Node — no image toolchain, no
  network. `build/icon-master.svg` is the editable reference.
- `win.icon` previously pointed at a non-existent `build/icon.ico`, so packaged
  builds shipped the default Electron icon. All 7 layers are now verified
  present inside the packaged `Lumi.exe`.
- `src/main/services/window-state.ts` — bottom-right anchor persistence with a
  400 ms debounce, plus a pure `clampToDisplays` that honours a stored position
  only while 40 px of the window's header strip remains reachable. 26 tests.
- Expand/collapse now anchors the bottom-right corner, so the orb stays put
  while the panel grows up and to the left.
- `display-added`/`display-removed`/`display-metrics-changed` re-clamp the live
  window, so unplugging a monitor cannot strand it off-screen.
- `lifelens:reset-window-position`, surfaced under More / Troubleshooting.

### Known pre-existing flake (not introduced here)

`src/main/services/realtime.test.ts` intermittently fails the full-suite run
with `Body is unusable: Body has already been read`. Its `fetch` mocks use
`mockResolvedValue(new Response(...))`, which hands the *same* `Response`
instance to every call; a second read of one body throws. It passes in
isolation and on most full runs. Not touched here because it sits in Realtime
test code that this pass is scoped out of.

## UI/UX polish — Slice 2 (conversation-first panel)

| Command | Result |
| --- | --- |
| `npm.cmd run typecheck` | passed |
| `npm.cmd test` | 540 passed, 2 skipped (35 files) |
| `npm.cmd run build` | passed |
| `npm.cmd run package` | passed |

The panel is now three fixed zones — a draggable header, a conversation that
takes the remaining height and scrolls, and a pinned composer — replacing the
single scrolling form column. The transcript is promoted from a collapsed
`<details>` to the primary surface, with an empty state whose suggestion chips
only prefill the composer and never execute.

Settings moved into a slide-over overlay grouped by capability: Voice, Files and
approved folders, Intelligent photo search, Telegram, Appearance, Privacy. The
photo-search and Telegram sections were moved intact rather than rewritten.

`src/renderer/src/status.ts` collapses the five competing signals into one
header pill, with the §15 precedence covered by 15 tests. Background photo
indexing is reported only when Lumi is otherwise idle.

Preserved and unchanged: the Realtime session lifecycle, microphone mute on
collapse and the collapse-disconnect timer, photo and file results, reason
badges, semantic-search status, Telegram login and confirmation, reminders,
screen capture, approved-folder controls, and selected-photo analysis.
`realtime.ts`, `file-search-controller.ts`, and `pending-action-coordinator.ts`
were not touched.

## UI/UX polish — Slice 3 (drag-and-drop) — PARTIAL, feature inert

| Command | Result |
| --- | --- |
| `npm.cmd run typecheck` | passed |
| `npm.cmd test` | 581 passed, 2 skipped (37 files) |
| `npm.cmd run build` | passed |
| `npm.cmd run package` | passed — `release/0.1.0/Lumi Setup 0.1.0.exe` |

**Landed.** `src/main/services/dropped-files.ts`: validation and a
capacity-one, memory-only store with a 30-minute idle TTL refreshed on use.
Validation order is shortcut-extension → `lstat` (rejecting links, junctions and
directories without following them) → `realpath` → second `lstat` on the
canonical path, then the *existing* `sniffAttachmentType`, `MAX_ATTACHMENT_BYTES`,
`MAX_PHOTO_BYTES`, `MAX_TEXT_BYTES` and `isTelegramSafeDimensions` — reused, not
reimplemented. 36 tests, including a fixture per supported type and fail-closed
revalidation after a size or mtime change.

Because creating a symbolic link needs elevation on Windows and the integration
test would otherwise skip silently, the reject-links rule is also covered
unconditionally through an exported `assertRegularFile` predicate.

`registerDroppedFile`/`removeDroppedFile` are wired through contracts, the
preload bridge, and validated main handlers. Preload is the only layer that
touches the path: it calls `webUtils.getPathForFile` and forwards the result,
never returning it to the renderer. `resolveTrustedPath` is defined and tested
as the seam where dropped-file trust and approved-root trust meet.

**Not yet landed.** The seam is not threaded into `createResultThumbnails`,
`validateTrustedAttachment`, `open_file`/`analyze_photo`, or
`PendingActionStore.createTrustedPreview`; there is no drop overlay, no
dropped-file card, and no renderer drop handler.

**The feature is therefore inert and fail-closed.** Nothing in the renderer
calls `registerDroppedFile`, and no action path accepts a dropped identifier, so
a dropped file cannot be opened, analysed, or sent. Threading the seam is the
next step and must keep the "no automatic action" invariant.

`open_file` and `analyze_photo` currently look a result up in the approved-root
store *before* resolving its path, so threading the seam is not a resolver swap
— those two paths need a dropped-file branch that supplies the display name
from the frozen snapshot and labels the confirmation preview "Dropped file".

## UI/UX polish — Slice 4 (copy and accessibility) — NOT STARTED

`docs/COPY.md` has not been applied. `src/renderer/src/copy.ts`, the banned-term
lint test, and the accessibility sweep remain outstanding. Some Slice 2 strings
were written in the COPY.md voice as they moved, but no systematic sweep ran and
older strings still carry the retired wording.
