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

## Durable agent task runtime

Agent tasks live in a separate Python sidecar (`services/agent`, FastAPI and async SQLAlchemy) backed by PostgreSQL (`infra/docker-compose.yml`), not in the JSON store. Task state must survive restarts and support optimistic concurrency. Each state change is a single transaction that updates the task's `revision` with compare-and-swap and appends one ordered event. The Electron app is not wired to the runtime yet. When it is, only Electron main will talk to it, and the renderer's security boundary stays as described above. See [`AGENT-RUNTIME.md`](AGENT-RUNTIME.md).

## Unknown outcomes are never treated as failures

`FAILED` means the runtime knows an operation did not happen. `OUTCOME_UNKNOWN` means it does not know whether the side effect occurred. Collapsing the two is how an agent double-books an appointment or pays twice: it reads a lost response as a failure and retries. So once a consequential action might have reached the outside world, the runtime never retries it because the process crashed or the response was lost.

Concretely: an execution attempt that a previous runtime process started and never finished becomes `OUTCOME_UNKNOWN` at startup, never `FAILED` and never back to `APPROVED`. No new attempt is created. `OUTCOME_UNKNOWN` has exactly one outgoing transition, to `RECONCILING`, and the only way to resolve it is authoritative reconciliation that *reads* external state rather than acting again. Reconciliation is allowed to conclude that it still does not know; that is recorded honestly rather than downgraded to a failure.

Ownership of in-flight work is decided by a recorded runtime generation, not by a timeout, so a process never mistakes its own healthy work for a dead process's leftovers. That check becomes an expired worker lease when workers are added.

## Durable approvals for consequential actions

The runtime's approval is a durable, expiring, single-use record, not a boolean on the action. A boolean cannot expire, cannot be spent, and cannot say what was approved.

Each approval is bound to one action, to the exact proposal (SHA-256 over canonical JSON, always computed by the server — the API has no digest field), and to an action revision, so any change to the action invalidates an approval already granted. A stored proposal is immutable, enforced by a database trigger rather than by convention, because an approved proposal must be the proposal that executes. Expiry is checked by the statement that claims the approval, not by a cleanup job. Database constraints — a partial unique index for one live approval per action, a unique `approval_id` on attempts, a partial unique index for one unfinished attempt per action — make these facts rather than conventions.

No caller may approve an arbitrary proposal payload: the server reconstructs approval information from persisted state. The FastAPI approve/reject routes currently stand in for the approving UI, and the execution and reconciliation routes are internal scaffolding for tests and the future browser worker. They are loopback-only and not a public API. Electron main becomes the trusted UI and security broker in a later milestone.

`risk_tier` (`R0`–`R3`) is persisted so a later policy engine can auto-approve local reads. Until that engine exists every tier requires an explicit approval, because the fail-safe answer is the only correct one.

The Electron `PendingActionStore` is deliberately left in memory and unchanged. The durable execution model is established once, in the runtime, rather than built twice.

## Browser work runs in an isolated worker process

The browser is where untrusted input enters Lumi. A web page can lie, change underneath the agent, or carry text written to be mistaken for an instruction. Running Chromium inside the runtime would put the process that holds `DATABASE_URL`, the action ledger and the approval API in the same address space as whatever a website sent.

So the browser worker (`services/agent/app/browser/`) is a separate process. It is given a credential, a list of allowed origins, one typed operation and its typed input, and nothing else: no database connection or URL, no approval surface, no provider keys, no Electron `.env`, no shell, no filesystem, no other task's data. It cannot approve anything because there is no code path to an approval, not because it is trusted not to. It reports what it observed; only the runtime decides what an action's status becomes.

Loopback alone is not the trust boundary any more. Any process on the machine can reach loopback, and once real side effects are possible that is not good enough. Every request to the worker, health included, carries a bootstrap-minted credential in a header, compared in constant time. It is never in a URL, never logged, never stored, and never reaches the renderer. A worker URL configured without a token yields no browser capability rather than an unauthenticated one.

## A closed registry instead of browser scripting

The worker exposes exactly two routes, `GET /health` and `POST /v1/dispatch`. A dispatch names an operation from a fixed tuple of reviewed functions; there is no selector parameter, no URL parameter, no script parameter, and no `POST /browser/evaluate`. Origins are resolved worker-side from a name in the proposal, so a proposal can never choose where the browser goes.

This is the difference between a tool and a capability. A generic browser-automation endpoint behind an agent is a remote code execution primitive: anything that can write a proposal, including a future model, could then run arbitrary JavaScript on any origin. A closed registry means the set of things that can happen in a browser is a list a human reviewed, and adding to it is a code change.

Every operation declares a typed input and output, an effect class (`READ_ONLY`, `PREPARE`, `CONSEQUENTIAL`), preconditions, postconditions, a timeout and what that timeout *means*, a retry classification and a reconciliation path. The registry refuses to build if a consequential operation is retryable or has no reconciliation path, because such an operation would have no exit from `OUTCOME_UNKNOWN` but a guess.

## `commit_booking` acts; `lookup_booking` only looks

Reconciliation must be able to run against an action that may already have had its effect. That is only safe if looking cannot cause the thing it is looking for. `lookup_booking` is therefore a GET with no click, no form submission and no access to the flag that records a submission; a test asserts its source contains none of them. Reconciliation that could act would be a retry wearing a different name.

`commit_booking` requires a claimed approval and a persisted execution attempt — the worker refuses it outright without an `attempt_id` — and proves its postcondition by reading a receipt identifier off a confirmation page carrying the reference it submitted. A click that returned is not a booking.

## An absent booking is only a failure where a site guarantees it

`NOT_FOUND` from a lookup is recorded as `FAILED` only where that site's trust declaration in `app/domain/sites.py` says absence is authoritative, stored next to the reason it is. The deterministic fixture earns it: single writer, one process, no queue, no settlement step, no expiry, so a booking is visible to the very next lookup and one that was never created can never appear later.

Every other site defaults to not earning it. On a real booking site a booking can be pending, queued, held behind an unsettled payment, visible only to a logged-in session, or eventually consistent, and an empty lookup is evidence about the lookup rather than about the world. Those actions stay `OUTCOME_UNKNOWN`. A lookup that failed outright resolves nothing either: failing to look is not evidence of absence.

## Approved values are re-observed immediately before the irreversible click

An approval authorises specific values, and a website may change them afterwards. Before clicking, the worker re-reads doctor, time, price and currency from freshly resolved locators and compares them against the *persisted* proposal — not against anything the page or the worker has said since. Times are compared as instants so an equivalent time in another offset is not a change; everything else is exact.

On any difference nothing is submitted, the action becomes `FAILED`, and the differences are recorded structurally. The approval was already consumed by the attempt, so the changed values have no authorisation and cannot acquire any; proceeding needs a new proposal and a new approval. The worker has no mechanism to accept or renegotiate a change, only to report one.

For the same reason nothing survives a page transition: no element handles, and no value observed on an earlier page. A value cached during preparation is exactly what a changed page would slip past.

## A submission flag, set before the click, decides what a failure may claim

The single bit that separates a known failure from an unknown outcome is whether a consequential request may already have gone out. The worker sets `submitted` immediately before the click and never after, and every failure is classified against it: an error before a submission is a failure Lumi can stand behind, and an error at or after one is `OUTCOME_UNKNOWN`. A timeout after the click is a statement about how long we waited, never about whether a booking exists.

The same discipline applies at the RPC layer. A refused connection means the dispatch was never delivered, so nothing happened. A dropped connection or a timeout means it was delivered and the answer was lost, which for a consequential operation is `OUTCOME_UNKNOWN`. Collapsing the two into "the worker call failed" is how an agent retries a booking it already made.

## The database, not the code, enforces one browser submission per approval

`browser_dispatches.attempt_id` is UNIQUE. Combined with Milestone 2's one-unfinished-attempt-per-action and one-approval-per-attempt, a second real submission for one approved action cannot be written down: the insert fails in PostgreSQL before any request leaves the process. A CHECK additionally requires an `attempt_id` for any `CONSEQUENTIAL` dispatch, so a read-only reconciliation lookup can never be counted as an execution.

The worker keeps its own in-memory defence rather than relying on the runtime being careful: a repeated dispatch id that is still running is refused, and one that finished replays the stored answer without touching a browser.

## Worker identity is a generation, not a lease

`browser_worker_generations` mirrors `runtime_generations`: one row per worker process. The runtime handshakes *before* claiming an approval, learns the worker's generation and addresses every dispatch to it; a restarted worker refuses with `stale_worker_generation`, and the runtime discards any reply naming a different runtime generation, worker generation, dispatch or operation. Handshaking first means a missing or replaced worker is discovered while nothing is at stake — no attempt started, no approval spent, nothing to reconcile.

There is no heartbeat, expiry or renewal, because none of them would change a decision while one worker runs at a time. It becomes a lease by adding `expires_at` and `heartbeat_at` to that table and turning "is this generation current" into a query instead of an equality check; every caller already asks that question in one place.

## Deterministic fixture sites, kept out of application code

`services/agent/evals/` is evaluation infrastructure and is never imported by `app/`. The appointment fixture exists so browser execution can be measured against a site whose state is completely known, with authoritative counters and deterministic fault injection — including creating a booking and then losing the response.

Two properties are deliberate. It counts every submission, including rejected ones and ones whose response was dropped, so "the browser pressed the button" is observable rather than inferred. And it does **not** deduplicate: posting the same reference twice creates two bookings. A site that absorbed duplicates would hide the exact bug this work exists to catch, and "exactly one booking exists" would stop being evidence about Lumi rather than a courtesy from the site.

The worker drives the fixture through Playwright like any site and never calls its backend directly; `/__eval__/*` is the test control plane only. None of this establishes reliability on arbitrary public websites — it establishes execution and reconciliation semantics against sites Lumi controls.

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
