# Architecture decisions

## Runtime split

- **Electron main (`src/main`)** owns `desktopCapturer`, notifications, selected-folder persistence, file opening, URL launching, permanent environment secrets, and all IPC validation.
- **Preload (`src/preload`)** exposes the fixed `window.lifeLens` API through `contextBridge`; it never exposes `ipcRenderer` itself.
- **Renderer (`src/renderer`)** owns the companion UI, browser microphone permission, WebRTC peer/data-channel lifecycle, transcript presentation, and confirmation UI.
- **Shared (`src/shared`)** contains type-only contracts, constants, parsing helpers, and runtime payload validators used on both sides of IPC.

## Realtime protocol

The main process reads `OPENAI_API_KEY` from the process environment and mints a short-lived client secret at OpenAI's `realtime/client_secrets` endpoint. It passes only that ephemeral credential to the renderer through the typed bridge. The renderer establishes a WebRTC peer, attaches microphone audio, and uses its data channel for image input, session updates, and function calls. This follows the official WebRTC pattern; the permanent key never enters renderer code or a committed configuration file.

The default model is `gpt-realtime-2.1`, because current official documentation describes it as the low-latency voice-agent model with tool support. The app starts a deterministic mock transport when no key is available so the whole local safety flow remains testable.

## Action safety

The model can only propose a typed `ToolProposal`. The renderer visibly renders the proposal and calls a distinct confirmed IPC method only after a click. The main process independently revalidates the proposal and permitted input before doing anything. No generic command, path, or URL executor exists.

| Tool | Main-process restriction |
| --- | --- |
| `create_reminder` | Requires confirmation; persists title, time, and source context only. |
| `search_documents` | Requires confirmation; only traverses explicit user-approved roots. |
| `open_file` | Requires confirmation; only opens a file returned by a previous approved search. |
| `open_url` | Requires confirmation; only permits `https:` and `http:` URLs. |
| `save_context` | Requires confirmation; stores only minimal structured context. |

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

Renderer confirmation is useful interaction feedback but is not trusted as the final authority. The main process parses every proposal again, checks capture provenance before `create_reminder` or `save_context`, and displays a native confirmation dialog before it creates a reminder, searches an approved folder, opens a returned file, opens a URL, or stores context.

## Release signing

Electron Builder produces the Windows installer and unpacked executable, but the repository deliberately contains no certificate or private signing material. A trusted Authenticode signing process is an external release prerequisite: Windows Smart App Control will block an unsigned release and must not be disabled or bypassed as part of LifeLens validation.

## UI design

The main BrowserWindow is transparent, frameless, always on top, and contains a CSS draggable companion. Its compact panel is not draggable, so buttons and input remain usable. The companion is deliberately lightweight: a colored orb with six observable states rather than an animated 3D pet.
