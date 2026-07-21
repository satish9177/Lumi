# Architecture decisions

## Runtime split

- **Electron main (`src/main`)** owns `desktopCapturer`, notifications, selected-folder persistence, file opening, URL launching, permanent environment secrets, and all IPC validation.
- **Preload (`src/preload`)** exposes the fixed `window.lifeLens` API through `contextBridge`; it never exposes `ipcRenderer` itself.
- **Renderer (`src/renderer`)** owns the companion UI, browser microphone permission, WebRTC peer/data-channel lifecycle, transcript presentation, and confirmation UI.
- **Shared (`src/shared`)** contains type-only contracts, constants, parsing helpers, and runtime payload validators used on both sides of IPC.

## Realtime protocol

The main process reads `OPENAI_API_KEY` from the process environment and mints a short-lived client secret at OpenAI's `realtime/client_secrets` endpoint. It passes only that ephemeral credential to the renderer through the typed bridge. The renderer establishes a WebRTC peer, attaches microphone audio, and uses its data channel for image input, session updates, and function calls. This follows the official WebRTC pattern; the permanent key never enters renderer code or a committed configuration file.

The default model is `gpt-realtime-2.1-mini` to reduce routine realtime audio, text, and image cost while retaining the MVP's function-calling, image-input, and reasoning controls. Operators can select the higher-quality flagship without a code change by setting `LIFELENS_REALTIME_MODEL=gpt-realtime-2.1`. The app starts a deterministic mock transport when no key is available so the whole local safety flow remains testable.

Client-secret configuration uses an explicit model-capability table rather than retrying a rejected request with a different model. `gpt-realtime-2.1-mini`, `gpt-realtime-2.1`, and `gpt-realtime-2` receive `reasoning.effort`; the recognized legacy `gpt-realtime-mini` override remains usable but omits that unsupported optional field and emits one non-secret warning. Unknown overrides are preserved but receive no optional reasoning field until their capability is known.

## Realtime cost controls

- Collapsing the panel still mutes the microphone immediately as a privacy affordance, then closes a live session after a 60-second grace period. An expanded live session closes after four minutes of genuine inactivity. Pending responses and function calls may defer either teardown by at most 120 additional seconds; this makes the collapsed lifetime at most three minutes and the idle lifetime at most six minutes from the last activity. Reopening counts as activity: it cancels collapse teardown and, when the live session remains connected, restores exactly one fresh expanded-idle lifecycle without changing the session generation. An existing idle deferral remains absolute and is never renewed. Client-side renderer timers are intentional: they can distinguish collapse from idle, update the UI, and cover the collapsed-but-connected grace window. The server-side `idle_timeout_ms` does not provide those controls.
- Reconnects mint a fresh ephemeral credential through the existing main-process boundary. Only the first successful live connection requests a greeting; later lazy reconnects do not pay for another greeting.
- Every live connection receives a renderer-wide monotonic session generation. Server-scoped work is identified by `(generation, callId)`, including confirmations, screen capture, file search, Telegram lookup, policy decisions, and asynchronous results. Disconnect invalidates the generation, clears pending work and timers, and expires server-backed UI. Late work is a silent no-op even if a new session has an open channel or reuses the same call ID. File-search resume tokens include the generation only on the renderer-to-main correlation path; the original server call ID is retained for Realtime output.
- Input transcription uses `gpt-4o-mini-transcribe`. Transcription remains a required additional cost because completed spoken transcripts feed the trusted main-process intent policy before guarded tool calls. Phase A reduces that line item but does not remove the trust-gating pipeline; only the documented Phase B local-STT roadmap can remove it.
- Laptop microphone input uses browser echo cancellation, noise suppression, and automatic gain control. The initial Realtime audio payload uses `noise_reduction: { type: 'far_field' }` and conservative server VAD settings: threshold `0.7`, prefix padding `300 ms`, and silence duration `650 ms`, while retaining `create_response: true` and `interrupt_response: true` for genuine barge-in. These are tunable operational starting values, not universal constants; follow-up `session.update` calls use the same turn-detection object. No server `idle_timeout_ms` is configured because renderer lifecycle timers own the collapse/idle policy.
- Screen captures retain their existing resolution ladder and explicitly use `detail: 'auto'` because on-screen text must stay legible. User-approved photo analysis is capped at 1024 pixels wide and uses `detail: 'low'`. The 150,000-byte JPEG cap is data-channel transport hygiene; encoded byte size is not an image-token control.
- The initial `session.update` contains the complete instructions, tools, audio configuration, transcription model, and session output ceiling. Later updates contain only changed instructions, preserving merged server state and avoiding repeated tool/audio payloads. The instruction prefix remains identifier-free and byte-stable for prompt caching.
- Output limits are runaway ceilings rather than brevity-by-truncation: 1024 tokens for VAD-created responses, 512 for ordinary typed questions and bounded search-result narration, 2048 for image and explicitly long-form requests, and 192 for greetings and short action acknowledgements. Search narration receives at most three safely shortened filenames, must state the total and that the full list is visible in the UI, and may offer additional results. Spoken VAD turns cannot receive a per-response override, so they use the session ceiling.
- Windows Graphics Capture can emit a transient native frame failure before Electron returns a usable thumbnail. The app makes one quiet retry only when it observes no usable selected frame; it reports a capture error only when both attempts fail. It does not suppress terminal failures or attempt to hide Electron's own native diagnostic output.

## Action safety

The model can only propose a typed `ToolProposal`. The renderer visibly renders the proposal and calls a distinct confirmed IPC method only after a click. The main process independently revalidates the proposal and permitted input before doing anything. No generic command, path, or URL executor exists.

| Tool | Main-process restriction |
| --- | --- |
| `create_reminder` | Requires confirmation; persists title, time, and source context only. |
| `search_documents` | Requires confirmation; only traverses explicit user-approved roots. |
| `open_file` | Requires confirmation; only opens a file returned by a previous approved search. |
| `open_url` | Requires confirmation; only permits `https:` and `http:` URLs. |
| `save_context` | Requires confirmation; stores only minimal structured context. |
| `analyze_photo` | Requires confirmation; sends only the selected, revalidated photo for the stated question. |
| `send_telegram_message` | Requires confirmation; uses the selected local Telegram account and resolved recipient. |
| `send_telegram_attachment` | Requires confirmation; revalidates the selected file before sending it to the resolved recipient. |

## Local storage

Use a small JSON store inside Electron's `userData` directory for the MVP. It is sufficient for reminders, allowed roots, search result identifiers, and context records, avoids native SQLite packaging risk, and can later be migrated behind the existing service boundary.

## Shared contracts

`src/shared/contracts.ts` is the source of truth for the following:

- companion states: `idle`, `listening`, `thinking`, `speaking`, `success`, `error`;
- `CaptureResult`, `RealtimeSessionCredential`, `Explanation`, and `ExtractedSignal`;
- `ToolName`, `ToolProposal`, and tool input/result shapes;
- the `LifeLensApi` preload surface; and
- runtime validation functions for all IPC arguments and responses.

## Sandboxed Electron entrypoints

The main and preload bundles use explicit `.cjs` entrypoints. Electron's sandboxed preload environment does not support an ESM preload bridge, while the bounded bridge needs `require('electron')` for `contextBridge` and `ipcRenderer`. Keeping both privileged entrypoints CommonJS preserves `sandbox: true`, `contextIsolation: true`, and `nodeIntegration: false` without relying on an unsandboxed renderer.

## Confirmation and provenance

The renderer confirmation card is the explicit user approval surface, but it is not trusted as the final authority. Its details come from an immutable main-process preview rather than renderer-supplied prose. On approval, main parses the proposal again, checks its provenance and current authorization, and stops without acting if the retained capture, file, folder, URL, recipient, or pending approval is no longer valid.

## Release signing

Electron Builder produces the Windows installer and unpacked executable, but the repository deliberately contains no certificate or private signing material. A trusted Authenticode signing process is an external release prerequisite: Windows Smart App Control may block an unsigned release and must not be disabled or bypassed as part of Lumi validation.

## UI design

The main BrowserWindow is transparent, frameless, always on top, and contains a CSS draggable companion. Its compact panel is not draggable, so buttons and input remain usable. The companion is deliberately lightweight: a colored orb with six observable states rather than an animated 3D pet.
