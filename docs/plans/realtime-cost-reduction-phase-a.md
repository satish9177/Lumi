# Realtime API cost reduction — Phase A implementation plan

## 1. Status and scope

- **Status:** Planned. Nothing in this document is implemented yet. Every fact in §2 was verified against the repository on 19 July 2026; everything else is design.
- **Scope:** Phase A only — reduce OpenAI Realtime API cost while keeping the existing natural realtime voice experience for cloud-handled conversation. Phase B (local-first hybrid) is a roadmap in §15 and must **not** be implemented in this change.
- **Audience:** A Codex implementation session. This document is intended to be sufficient without repeating the architectural investigation.
- Anything marked **(proposed)** does not exist in the repository today.

## 2. Current behavior and verified cost sources

### 2.1 Session establishment

- `src/main/services/realtime.ts:4` — `REALTIME_MODEL` defaults to `'gpt-realtime-2.1'`, overridable via `LIFELENS_REALTIME_MODEL`.
- `src/main/services/realtime.ts:25-74` — `createRealtimeSessionCredential` POSTs to `https://api.openai.com/v1/realtime/client_secrets` with `session: { type: 'realtime', model, reasoning: { effort }, audio: { output: { voice: 'marin' } } }`. Reasoning effort defaults to `'low'` (`getRealtimeReasoningEffort`, lines 10-23). No API key → `{ mode: 'mock', model }` (lines 27-30). The credential's `expiresAt` (line 72) is returned but never acted on.
- `src/renderer/src/realtime.ts:516-576` — `connectLive` gets the microphone, creates an `RTCPeerConnection`, and POSTs the SDP offer to `https://api.openai.com/v1/realtime/calls`. `waitForDataChannel` (line 578) sets `connected = true` and calls `configureLiveSession` (line 605) on channel open.
- `configureLiveSession` sets `awaitingInitialSessionUpdate = true` and sends the initial full `session.update`. When the server acks with `session.updated` (`handleServerEvent`, lines 721-727), `requestGreeting` (lines 610-618) sends a `response.create` — **a paid greeting on every connect**.

### 2.2 The always-on billing problem (largest cost source)

- `src/renderer/src/LifeLensApp.tsx:77-81` — collapsing the panel only calls `clientRef.current?.setListening(expanded)`.
- `src/renderer/src/realtime.ts:248-260` — `setListening(false)` sets `track.enabled = false` and sends a narrow `session.update` with `turn_detection: null`. **A disabled WebRTC track still transmits silence frames, and Realtime bills them as audio input.** The session, WebRTC connection, and billing stay live until app unmount (`LifeLensApp.tsx:97-105` cleanup) or connection failure (`realtime.ts:530-536`).
- There is no idle timeout of any kind.

### 2.3 Input transcription — a required additional cost

- `src/renderer/src/realtime.ts:42` — `INPUT_TRANSCRIPTION_MODEL = 'whisper-1'`, applied in `sendSessionUpdate` (line 657). Whisper-1 is billed per audio minute on top of realtime audio tokens.
- This is **not removable waste**: `conversation.item.input_audio_transcription.completed` → `handleUserTranscript` (lines 729-731, 795-811) → `onUserTranscript` → `window.lifeLens.noteUserRequest` (`LifeLensApp.tsx:146`) → main-process `IntentTracker` (`src/main/services/intent-policy.ts`). The trust gating that lets a spoken "find my resume" auto-run a model-initiated `search_documents` (and blocks screen capture for file-search intents) depends on these transcripts. The `intentUpdate` promise chain (`realtime.ts:206, 807-811, 901-911`) serializes each transcript ahead of guarded tool calls. Transcription remains required until Phase B local STT produces the transcripts instead.

### 2.4 Image cost

- `src/main/services/capture.ts:5-9` — `MAX_CAPTURE_WIDTH = 1_600`, `MAX_CAPTURE_HEIGHT = 1_000`, `MAX_CAPTURE_BYTES = 180_000` (exported), `MIN_CAPTURE_WIDTH = 560`. `encodeCaptureImage` (lines 84-109): JPEG quality ladder 72/62/52/42, then ×0.72 width downscale, floor 560 px, else throw.
- Screen captures are sent as `{ type: 'input_image', image_url: capture.dataUrl }` with **no `detail` field** at `src/renderer/src/realtime.ts:464` (`sendCapture`) and `:1001` (`sendCaptureForRequest`). `detail` omitted/`auto` is processed as **high** detail: a 1600×1000 image ≈ 1,100 image tokens.
- Photo analysis: `src/main/services/tools.ts:76-119` (`analyze_photo` case) loads the user-approved photo (`loadImageForAnalysis`) and runs the **same** `encodeCaptureImage` ladder — the byte cap is the only bound, so a photo can go out at up to 1600 px+ with high detail. Sent at `src/renderer/src/realtime.ts:305` (`analyzeSelectedPhoto`), again without `detail`.
- Follow-ups reuse the image already in the conversation (no re-upload; `realtime.test.ts:472-481` proves it), but conversation-resident images are re-billed as (cached) input each turn.

### 2.5 session.update churn

- `sendSessionUpdate` (`realtime.ts:645-663`) always sends the **full** payload: composed instructions (≈2.4 KB, ~600 tokens; `SYSTEM_INSTRUCTIONS` lines 45-68 + `sessionInstructions` lines 625-637), all 8 `TOOL_DEFINITIONS` (lines 70-182), `tool_choice`, and the audio block.
- `updateLiveSessionInstructions` (lines 639-643) calls that full `sendSessionUpdate` and is triggered from **six** sites: `setApprovedRoots` (237), `analyzeSelectedPhoto` (297), `clearSelectedPhoto` (321), `invalidateScreenContext` (422), `sendCapture` (447), `sendCaptureForRequest` (988) — even when the composed string did not change. Identical-prefix stability is what keeps cached-input pricing applying; instructions are already deliberately identifier-free (comments at 620-624, 632).
- `setListening` (248-260) sends its own narrow `turn_detection`-only update — this one is correct and must be preserved.

### 2.6 response.create inventory (all six emission sites)

| # | Site | Line | Current payload |
|---|------|------|-----------------|
| 1 | `analyzeSelectedPhoto` | 309 | `response: { output_modalities: ['audio'] }` |
| 2 | `sendUserRequest` (typed question) | 377 | `response: { output_modalities: ['audio'] }` |
| 3 | `sendCapture` (capture + question) | 468-471 | `response: { output_modalities: ['audio'] }` |
| 4 | `requestGreeting` | 611-617 | `response: { instructions: …, output_modalities: ['audio'] }` |
| 5 | `sendFunctionCallOutput` (after tool result) | 690 | bare `{ type: 'response.create' }` — **no `response` object today** |
| 6 | `sendCaptureForRequest` (capture for held call) | 1006 | `response: { output_modalities: ['audio'] }` |

Spoken turns detected by `server_vad` create responses **server-side with no client `response.create`** — they can only be governed by session-level defaults (§8).

### 2.7 Other verified behavior relied on below

- `disconnect` (`realtime.ts:495-514`) resets: `connected`, `responseActive`, `awaitingInitialSessionUpdate`, `currentCapture`, `currentExplanation`, `lastUserRequest`, `resultOrdinals`, `selectedPhoto`, `listening = true`; closes channel/peer, stops tracks, removes the audio element, cancels `speechSynthesis`. `completedCallIds`/`answeredCallIds` are `readonly` Sets and are **never** cleared — harmless (call IDs are unique per server conversation) but Codex should not "fix" this by clearing them mid-session.
- `sendEvent` (665-670) **throws** when the data channel is not open. `sendFunctionCallOutput` (672-695) catches and routes to `callbacks.onError` — this is the current stale-send error-spam path.
- `completeFileSearch` (328-346) answers each held search call at most once via `answeredCallIds`.
- Mock mode: `connect` short-circuits (214-221); `handleMockUserRequest` (380-403) runs `classifyUserIntent` locally; `speakMock` uses `window.speechSynthesis`. Mock never opens a data channel.
- Renderer entry points that need a live session: `askQuestion` (`LifeLensApp.tsx:239-259`), `captureScreen` (187-221) / `requestScreenContext` (223-237), `confirmPendingAction` → `analyzeSelectedPhoto` when `result.analysisImage` (515-516). `openCompanion` (166-171) already reconnects when `!isConnected()`.
- Verified pricing/model facts (web-checked July 2026 — Codex should not re-research): `gpt-realtime-2.1-mini` audio $10/M in, $20/M out (flagship `gpt-realtime-2.1`: $32/$64); mini text $0.60/$2.40, image $0.80/M; mini supports function calling, image input, `reasoning.effort`, 32k context, 4,096 max output tokens. `gpt-4o-mini-transcribe` ≈ $0.003/min vs whisper-1 $0.006/min and is a valid `audio.input.transcription.model` value. `input_image` content parts accept `detail: 'low' | 'high' | 'auto'`; `auto` resolves to high. `session.update` is a merge: omitted fields keep their previous values.

## 3. Goals

1. Stop paying for silence: the session must be torn down when idle or collapsed, and lazily rebuilt.
2. Move the default model to `gpt-realtime-2.1-mini` while keeping `gpt-realtime-2.1` one env var away.
3. Halve the transcription line item without touching the trust-gating pipeline.
4. Cut image tokens where quality allows (photo analysis) and make screen-capture detail intent explicit.
5. Stop resending the instructions + tool definitions when nothing changed.
6. Bound runaway output cost with ceilings that never truncate legitimate long-form answers.
7. Prove the savings with a repeatable before/after benchmark — no estimated percentages.

## 4. Non-goals

- No Phase B implementation (local STT, local TTS, local routing) — §15 is documentation only.
- No redesign of local file search, ranking, IntentTracker, SearchOrchestrator, pending actions, or trusted-action handling.
- No change to mock mode behavior.
- No local LLM, no GPU requirement; target stays an ordinary CPU-only Windows laptop (~8 GB RAM).
- No conversation-item deletion bookkeeping for stale images (considered; negligible saving at mini's cached-image rate versus the `conversation.item.created` id-tracking it requires).
- No use of the server-side `idle_timeout_ms` (see §5).

## 5. Architectural constraints

- **Client-side timers are intentional.** The collapse and idle timers must live in the renderer because they distinguish collapse from idle, must not fire while a response or function call is in flight, must drive renderer UI ("Reconnecting…", paused notice), and must cover the collapsed-but-connected window. Do **not** replace them with the Realtime server `idle_timeout_ms`.
- Muting is a privacy affordance, not a cost control: collapse must still mute **instantly** (existing `setListening(false)` behavior is preserved) and disconnect only after the grace period.
- The permanent API key stays in the main process; all new behavior uses the existing ephemeral-credential flow. Every reconnect mints a fresh credential via `window.lifeLens.createRealtimeSession()`, which makes the unused `expiresAt` moot.
- The instructions prefix must stay byte-stable and identifier-free (prompt caching); the existing tests at `realtime.test.ts:130` and `:507` enforce this and must keep passing.
- All IPC shapes in `src/shared/contracts.ts` stay backward compatible; additions are optional fields only (and Phase A needs none — the `RealtimeSessionCredential` shape is untouched).

## 6. Detailed Phase A design

### A1. Default model → `gpt-realtime-2.1-mini`

- `src/main/services/realtime.ts:4`: change the fallback string to `'gpt-realtime-2.1-mini'`. Nothing else changes; `LIFELENS_REALTIME_MODEL` override and `reasoning: { effort }` already work and are supported by the mini.
- `.env.example:2`: `LIFELENS_REALTIME_MODEL=gpt-realtime-2.1-mini`, plus a comment: set `gpt-realtime-2.1` for the higher-quality flagship.
- Note for Codex: `REALTIME_MODEL` is read at **module load**. The new default-model test must use `vi.resetModules()` + `vi.stubEnv('LIFELENS_REALTIME_MODEL', '')`/delete + dynamic `import('./realtime')`, then assert the client-secret request body `session.model === 'gpt-realtime-2.1-mini'` (extend the fetch-mock pattern already used at `realtime.test.ts:37-50`).

### A2. Session lifecycle: mute → grace disconnect → lazy reconnect

New constants **(proposed)** at the top of `src/renderer/src/realtime.ts`:

```ts
const COLLAPSE_DISCONNECT_MS = 60_000        // collapse: mute now, disconnect after 60 s
const IDLE_DISCONNECT_MS = 4 * 60_000        // expanded: disconnect after 4 min of genuine inactivity
```

**RealtimeClient changes (proposed):**

1. New callback `onSessionEnded?: (reason: 'idle' | 'collapsed' | 'error') => void` on `RealtimeCallbacks`.
2. `connect(credential, options?: { greet?: boolean })` — greeting is requested in the `session.updated` branch only when the flag was true. Default `greet: true` preserves current behavior for explicit connects.
3. **Idle timer** (private): armed only when `mode === 'live' && connected`. `touchActivity()` resets it and is called from: `handleUserTranscript`, `sendUserRequest`, `sendCapture`, `sendCaptureForRequest`, `analyzeSelectedPhoto`, `handleToolCall`, `handleResponseDone`, `completeFileSearch`. When the timer fires while work is pending, **re-arm** instead of disconnecting. "Pending work" = `responseActive === true` OR an outstanding function call. Track outstanding calls with a new `private readonly pendingCallIds = new Set<string>()` **(proposed)**: add in `handleToolCall` after the duplicate check; delete in `sendFunctionCallOutput` (all outputs go through it) and in `completeFileSearch`'s early-returns. On a clean fire: `this.disconnect(); this.callbacks.onState('idle'); this.callbacks.onSessionEnded?.('idle')`.
4. `disconnect()` additionally clears the idle timer, clears `pendingCallIds`, and resets the A5 instructions cache. Its existing per-session resets are already correct for reconnect (a new session is a brand-new server conversation with no memory of captures or photos).
5. **Stale-send hardening:** add a guard at the top of `sendFunctionCallOutput`: if `this.mode !== 'live' || this.dataChannel?.readyState !== 'open'`, return silently. This converts post-teardown arrivals from `completeFileSearch`, `sendToolResult`, `declineToolProposal`, `declineScreenContext`, and `completeTelegramRecipientSearch` into no-ops instead of `onError` spam (today `sendEvent` throws and the catch at `realtime.ts:692-694` surfaces it). The user-facing entry points (`sendUserRequest`, `sendCapture`, `analyzeSelectedPhoto`) keep throwing when not connected — the app layer reconnects first (below).

**LifeLensApp.tsx changes (proposed):**

1. Collapse effect (`:77-81`): keep `setListening(expanded)` unchanged (instant mute). Additionally, when `expanded` becomes false and the client is live-connected, start a `COLLAPSE_DISCONNECT_MS` timeout that calls `clientRef.current?.disconnect()` and marks voice paused; clear the timeout when `expanded` becomes true again.
2. New `ensureConnected(): Promise<void>` **(proposed)** — returns immediately when `clientRef.current?.isConnected()`; otherwise awaits `connectVoice()`. Guard against concurrent calls by storing the in-flight promise (reuse/replace the `isConnecting` flag). Call it at the top of `askQuestion`, before `client.provideScreenContext` in `captureScreen`, and before `analyzeSelectedPhoto` in `confirmPendingAction` (`:515-516`). `openCompanion` keeps its existing `isConnected()` check.
3. `connectVoice` passes `{ greet: !hasConnectedOnceRef.current }` and sets the ref after the first successful live connect (mock keeps its own greeting).
4. Wire `onSessionEnded` in the `RealtimeClient` construction (`:140-151`): set companion state `idle`, set a paused notice — exact string: **"Voice paused to save cost — ask a question to reconnect."** Do not clear local UI (search results, thumbnails, transcript, capture preview stay; they are local). Only the model-side context is gone, which the client's own `disconnect()` resets already, so `sessionInstructions()` recomposes correctly on the next connect.
5. **"Reconnecting…"**: when `ensureConnected` runs `connectVoice` and `hasConnectedOnceRef.current` is true, render the connecting badge text as "Reconnecting…" (plain conditional on existing `isConnecting` state; no new contract types).

### A3. Transcription model

- `src/renderer/src/realtime.ts:42`: `const INPUT_TRANSCRIPTION_MODEL = 'gpt-4o-mini-transcribe'`. No other change — the transcript event type and the IntentTracker flow are model-agnostic.
- Framing for docs and commit message: transcription is a **required additional cost** (trust gating depends on it), halved — not removed. It is removed only by Phase B local STT.

### A4. Image cost controls

1. **Screen captures keep their resolution** (on-screen text must stay legible; shrinking below a 768 px short side is the only way to reduce high-detail tokens and would hurt reading). Add an explicit `detail: 'auto'` to the `input_image` content parts at `realtime.ts:464` and `:1001` to document intent.
2. **Photo analysis** uses `detail: 'low'` at `realtime.ts:305` (photos of scenes/people tolerate low fidelity; ~85 tokens instead of ~1,100), and the prepared image is downscaled in main: in `src/main/services/tools.ts` `analyze_photo` (line ~102), call `encodeCaptureImage(image, { maxWidth: 1024 })` **(proposed parameter)**.
3. `src/main/services/capture.ts`: extend `encodeCaptureImage(sourceImage, options?: { maxWidth?: number })` **(proposed)** — when the source width exceeds `maxWidth`, resize to `maxWidth` (aspect preserved via the existing width-only `resize`) before entering the quality ladder. Default behavior without options is unchanged (screen captures).
4. `MAX_CAPTURE_BYTES`: `180_000` → `150_000`. This is transport hygiene for the data channel only — **JPEG byte size does not directly reduce image-token usage** (tokens follow pixel dimensions/detail); say so in the code comment.

### A5. session.update deduplication

**(all proposed, in `src/renderer/src/realtime.ts`):**

1. `private lastSentInstructions: string | undefined`, reset in `disconnect()`.
2. `sendSessionUpdate()` remains the **initial full** payload (instructions, tools, tool_choice, audio incl. transcription and turn_detection, plus the A6 session `max_output_tokens`) and is called only from `configureLiveSession()`. It records `lastSentInstructions`.
3. `updateLiveSessionInstructions()` becomes: compose `sessionInstructions()`; if identical to `lastSentInstructions`, return; otherwise send `{ type: 'session.update', session: { type: 'realtime', instructions } }` only, and update the cache. Tools and audio are never resent — `session.update` merges, omitted fields persist.
4. `setListening`'s narrow `turn_detection`-only update (248-260) is untouched and must not include instructions.

### A7-adjacent note (numbering per repo conventions is Codex's choice): mock mode and local systems

- The idle/collapse timers must be inert in mock mode (`mode === 'live'` gate) — mock demo sessions never self-terminate. Add a regression test.
- IntentTracker, `normalizeSearchQuery`, document search/ranking, trust policy, SearchOrchestrator, FileSearchController, pending actions: **no changes**. The IntentTracker lives in main with its own TTL and is unaffected by renderer reconnects.

## 7. Session lifecycle — state transitions and cleanup ownership

States (conceptual; implement with existing fields + the new timers, not a formal FSM):

```
DISCONNECTED ──connectVoice()──▶ CONNECTING ──channel open──▶ LIVE(listening)
LIVE ─panel collapse─▶ LIVE(muted, collapse timer 60 s) ─timer─▶ DISCONNECTED(paused: 'collapsed')
LIVE(muted) ─panel expand before timer─▶ LIVE(listening)          [timer cleared]
LIVE ─no activity 4 min, no pending work─▶ DISCONNECTED(paused: 'idle')
LIVE ─idle timer fires with responseActive or pendingCallIds─▶ re-arm timer
DISCONNECTED(paused) ─askQuestion/captureScreen/photo confirm─▶ CONNECTING ("Reconnecting…", greet: false)
any ─connection failed─▶ DISCONNECTED(error)                      [existing path, realtime.ts:530-536]
```

Cleanup ownership:

| Responsibility | Owner |
|---|---|
| Timers (arm/clear/re-arm), pendingCallIds, lastSentInstructions | `RealtimeClient` (idle timer); `LifeLensApp` (collapse timer) |
| WebRTC/channel/track/audio-element teardown, per-session model context reset | `RealtimeClient.disconnect()` (existing, lines 495-514) |
| Paused notice, "Reconnecting…" badge, greet-once ref | `LifeLensApp.tsx` |
| Fresh credential per reconnect | existing `connectVoice` → `window.lifeLens.createRealtimeSession()` |
| Held search calls / pending confirmations after teardown | main-process stores keep working; replies to the departed model become silent no-ops (A2 step 5) |

## 8. Token-budget policy

Output audio is the most expensive token class ($20/M mini, $64/M flagship). Policy: **ceilings, not truncation** — brevity comes from instructions; caps only stop runaways.

- **Session default** (in the initial `session.update`): `max_output_tokens: 1024`. This is the only control that applies to **server-VAD auto-created responses** (spoken turns have no client `response.create`, §2.6), so it must be high enough for a spoken article/story explanation. 1024 audio tokens ≈ a substantial spoken answer; if live testing shows truncation of legitimate spoken long-form answers, raise this default rather than adding complexity.
- **Per-response budgets** via `response.create → response: { max_output_tokens, … }`, chosen by a small `pickResponseBudget(kind)` helper **(proposed)**:

| Kind | Budget | Sites (from §2.6) |
|---|---|---|
| confirmation/ack | 192 (range 128–256) | #5 `sendFunctionCallOutput` — add a `response` object `{ output_modalities: ['audio'], max_output_tokens }`; #4 `requestGreeting` |
| normal question | 512 | #2 `sendUserRequest` when no long-form cue matches |
| long-form | 2048 (range 1600–2400) | #1 `analyzeSelectedPhoto`, #3 `sendCapture`, #6 `sendCaptureForRequest`, and #2 when the request text matches a long-form cue |

- Long-form cue check **(proposed)**: a small case-insensitive regex over the outgoing request text — e.g. `explain in detail|in detail|detailed|article|story|summarize (this|the) (page|article|screen)|walk me through|step by step`. Keep it a short list; it only picks a ceiling.
- **Instruction policy** — replace the last sentence of `SYSTEM_INSTRUCTIONS` (`realtime.ts:67`, "Keep a visible text version of each answer under 120 words.") with:
  - "For simple requests, answer naturally in one or two short sentences."
  - "For article, screen, story, or explicitly detailed requests, give a complete structured explanation without omitting necessary context."
  - "Do not repeat the user's question."
  (This instruction change intentionally alters the cacheable prefix once; the `realtime.test.ts:130`/`:507` invariants — no timestamps, no filenames — still hold.)
- Limitation to document in code: spoken VAD-auto turns cannot receive per-response budgets; they are governed by the 1024 session default only.

## 9. File-by-file implementation map

| File | Changes |
|---|---|
| `src/main/services/realtime.ts` | A1: default model string (line 4). |
| `src/renderer/src/realtime.ts` | A2: `onSessionEnded`, greet option, idle timer + `touchActivity`, `pendingCallIds`, stale-send guard in `sendFunctionCallOutput`; A3: transcription constant (line 42); A4: `detail` fields (lines 305, 464, 1001); A5: `lastSentInstructions`, instruction-only updates; A6/§8: session `max_output_tokens: 1024`, `pickResponseBudget`, per-response budgets at sites #1–#6, SYSTEM_INSTRUCTIONS last sentence swap. |
| `src/renderer/src/LifeLensApp.tsx` | A2: collapse grace timer in the `expanded` effect (77-81), `ensureConnected()` used by `askQuestion` (239), `captureScreen` (187), `confirmPendingAction` photo branch (515-516), `connectVoice` greet flag + `hasConnectedOnceRef`, `onSessionEnded` wiring (140-151), paused notice + "Reconnecting…" badge text. |
| `src/main/services/capture.ts` | A4: `encodeCaptureImage` optional `maxWidth`; `MAX_CAPTURE_BYTES` 150_000 with a "bytes ≠ tokens" comment. |
| `src/main/services/tools.ts` | A4: `analyze_photo` calls `encodeCaptureImage(image, { maxWidth: 1024 })` (~line 102). |
| `.env.example` | A1: new default + flagship-override comment. |
| `docs/DECISIONS.md` | New "Cost controls" section: model default rationale; mute-vs-disconnect (silence frames are billed); client-side timers over `idle_timeout_ms`; transcription as required cost; image detail policy; dedupe; token-budget policy. Update the `gpt-realtime-2.1` sentence in "Realtime protocol". |
| `docs/STATUS.md` | Dated section with the changes and, after the benchmark runs, the measured table (§12). |

No changes: `src/shared/contracts.ts`, preload, IntentTracker/orchestrator/search/ranking/store/pending-actions, mock paths.

## 10. Test plan

Vitest; 16 test files exist. Run `npm.cmd run typecheck` and `npm.cmd test`.

**`src/main/services/realtime.test.ts`** (pattern at lines 37-50):
- New: default model is `gpt-realtime-2.1-mini` when `LIFELENS_REALTIME_MODEL` is unset — needs `vi.resetModules()` + dynamic import because `REALTIME_MODEL` is module-load-time; assert via the mocked fetch request body.
- New: `LIFELENS_REALTIME_MODEL=gpt-realtime-2.1` is honored (flagship override).

**`src/renderer/src/realtime.test.ts`** (helpers `injectDataChannel`, `setLiveMode`, `callHandleServerEvent` already exist):
- Line 123: tighten `transcription: { model: expect.any(String) }` → exact `'gpt-4o-mini-transcribe'`.
- Line 106 test: initial payload additionally asserts `max_output_tokens: 1024`; greeting `response.create` (line 127 assertion) now also carries the small budget; greeting only fires when `greet` was requested (new test: reconnect with `greet: false` → no `response.create` after `session.updated`).
- New A5 tests: two consecutive `setApprovedRoots` with identical composed instructions → exactly one `session.update` after the initial; post-initial updates contain `instructions` but no `tools`/`audio` keys; `setListening(false)` update still contains only `turn_detection` (existing tests at 624-663 keep passing).
- New A2 tests (use `vi.useFakeTimers()`): idle timer fires after `IDLE_DISCONNECT_MS` → `onSessionEnded('idle')` and channel closed; timer re-arms while `responseActive`; re-arms while a tool call is unanswered (pendingCallIds); `touchActivity` via a transcript event defers it; **mock mode never fires the timer**; after `disconnect()`, `completeFileSearch`/`sendToolResult` are silent no-ops (no `onError`).
- New A4/§8 tests: capture `input_image` part carries `detail: 'auto'`; photo part carries `detail: 'low'`; `sendUserRequest('explain this page in detail')` emits `max_output_tokens` in the long-form band; a function-call-output follow-up emits the confirmation budget.
- Existing invariants that must keep passing: 130 (no timestamps in instructions), 507 (no filename in instructions), 260 (one terminal search answer), 553 (photo before connect throws), 624/646 (mic privacy).

**`src/main/services/capture.test.ts`** (FakeCaptureImage at lines 5-18):
- New: `encodeCaptureImage(img, { maxWidth: 1024 })` downscales a 1600-wide fake to ≤1024 preserving aspect; without options behavior is unchanged (existing tests already pin the ladder and the ≤`MAX_CAPTURE_BYTES` bound, and survive the 150 KB change because they use the exported constant).

**`src/main/services/tools.test.ts`**:
- Line 180: tighten `result.analysisImage!.width` bound from `<= 1_600` to `<= 1_024`.

## 11. Manual verification plan

1. `npm.cmd run typecheck`; `npm.cmd test`; `npm.cmd run build`.
2. **Mock mode** (no `OPENAI_API_KEY`): orb, demo greeting, mock file search and capture flow unchanged; leave it open >5 min — no self-termination.
3. **Live** (operator key): DevTools network shows the `client_secrets` body with `"model":"gpt-realtime-2.1-mini"`; session connects, greets once.
4. Speak "find my latest resume": transcript appears, trusted search auto-runs (IntentTracker gating intact), results spoken by number.
5. Collapse the panel: mic mutes instantly; within ~60 s the connection closes (webrtc-internals or a debug log). Reopen and type a question: "Reconnecting…" appears, the answer arrives, **no repeated greeting**.
6. Leave expanded and silent 4+ minutes: paused notice appears. Ask a question: reconnects and answers.
7. Capture a screen and ask "explain this page in detail": complete, untruncated spoken answer; DevTools data channel shows the long-form `max_output_tokens`. A search confirmation follow-up shows the small budget.
8. Toggle photo select/clear and folder approval repeatedly: `session.update` appears only when instructions actually changed and never contains `tools` after the initial one.

## 12. Before/after cost benchmark (defines the measurement — do not fabricate results)

Run the identical scripted sequence twice: once on current `main` (before), once on the implemented branch (after), same machine, same key, one run each, sessions separated enough to read cleanly on the usage dashboard.

Sequence (~10 min): connect → ask one typed question → perform one spoken file search → capture the screen and request an explanation → remain silent 2 min with the panel expanded → collapse the panel → wait 5 min → reopen and ask one more question → close.

Record per run from platform.openai.com/usage (filter by key and model): realtime audio input tokens; realtime audio output tokens; text input/output tokens; image tokens; transcription usage (minutes/cost line); request counts; total cost. Compute % change per line item.

The implementation writes the measured table into `docs/STATUS.md`. Only measured numbers may be claimed — no projected percentages anywhere in docs or commit messages.

## 13. Failure cases and edge cases

- **Idle fires mid-speech:** a spoken utterance only touches activity when its transcript completes. Mitigations already in the design: `responseActive` re-arms the timer, and 4 min of true silence is required. Optional hardening **(proposed, Codex judgment)**: also `touchActivity()` on `input_audio_buffer.speech_started` server events (currently unhandled in `handleServerEvent`).
- **Collapse timer vs in-flight work:** if a response or unanswered call is pending when the collapse timer fires, defer exactly like the idle timer (same pending-work check) and retry shortly after.
- **Concurrent reconnects:** two rapid `ensureConnected` calls must share one in-flight `connectVoice` promise; `connect()` already starts with `this.disconnect()` so a stray double-connect cannot leak a peer connection.
- **Tool output after teardown:** search resolution or a confirmation arriving after disconnect → silent no-op (A2 step 5); the local result still renders; the main-process store is unaffected. On the next session the model simply has no memory of the old call — acceptable by design.
- **Reconnect with stale UI context:** the previous capture preview stays visible locally, but the new session has no image; `sessionInstructions()` reports "no current screen context", so the model will request a fresh capture if needed. Do not resend old images automatically.
- **Credential mint fails on reconnect:** existing `connectVoice` error path (state `error`, message) applies; the paused notice is replaced by the error.
- **Mini quality regression:** if answer quality is unacceptable, set `LIFELENS_REALTIME_MODEL=gpt-realtime-2.1` — no code change.
- **`detail` field rejected at runtime:** unlikely per docs, but if the live smoke test errors on `input_image.detail`, drop the field from screen captures first (auto is the default anyway) and keep the photo downscale, which saves regardless.

## 14. Rollback strategy

- Model: `LIFELENS_REALTIME_MODEL=gpt-realtime-2.1` (env only, no deploy).
- Lifecycle/budgets/dedupe: constants are top-of-file; raising `IDLE_DISCONNECT_MS`/`COLLAPSE_DISCONNECT_MS` to effectively-infinite or `max_output_tokens` upward are one-line reverts. Full rollback is a single revert of the implementation commit(s); no data migrations, no contract changes, no stored-state impact.

## 15. Phase B roadmap — local-first hybrid (documentation only, do not implement)

- **Local STT:** whisper.cpp `base` multilingual (Telugu-English code-switching) via a Node binding in a main-process worker; `tiny` as the low-end fallback; ~1–2 s per utterance on a typical laptop CPU. Gate with voice-activity detection (e.g. Silero via `onnxruntime-node`) so only speech is transcribed.
- **Routing:** STT text enters exactly where typed text already does — `classifyUserIntent` (`src/shared/intent.ts`) → IntentTracker → SearchOrchestrator. The mock path (`handleMockUserRequest`) already proves file search, capture requests, and clarifications work with zero API calls. A main-process `voice-router.ts` **(proposed)** formalizes it for `local_file_search`, `reminder`, and `open_target`.
- **Local TTS:** Windows SAPI (`System.Speech`) as the zero-install default; Piper as an optional later quality upgrade; renderer `speechSynthesis` remains the fallback (mock mode already uses it).
- **Escalation:** `general_question` / `visible_screen_question` / ambiguous intents lazily open a realtime session through the Phase A `ensureConnected()` machinery and are torn down by the same idle timer. OpenAI is then billed only during explicit escalations.
- **Hardware:** everything above runs on an ordinary 8 GB CPU-only laptop; explicitly **no** local 7–8B LLM requirement.

## 16. Codex implementation checklist

1. [ ] A1 model default + `.env.example` + both main realtime tests.
2. [ ] A3 transcription constant + tightened test assertion.
3. [ ] A5 dedupe (`lastSentInstructions`, instruction-only updates) + tests.
4. [ ] §8 budgets: session `max_output_tokens: 1024`, `pickResponseBudget`, all six `response.create` sites, SYSTEM_INSTRUCTIONS policy swap + tests.
5. [ ] A2 client: greet option, `onSessionEnded`, idle timer + `touchActivity` + `pendingCallIds`, stale-send guard, `disconnect()` extensions + fake-timer tests.
6. [ ] A2 app: collapse grace timer, `ensureConnected`, greet-once ref, paused notice, "Reconnecting…" badge.
7. [ ] A4 images: `detail` fields, `encodeCaptureImage({ maxWidth })`, `analyze_photo` 1024 cap, `MAX_CAPTURE_BYTES` 150 000 + tests (incl. tools.test.ts width bound).
8. [ ] Docs: DECISIONS.md cost-controls section; STATUS.md dated entry.
9. [ ] `npm.cmd run typecheck` && `npm.cmd test` && `npm.cmd run build` green; mock-mode manual pass (§11 step 2).
10. [ ] Live smoke test (§11 steps 3–8) with an operator-provided key.
11. [ ] Benchmark (§12) on old main and new branch; measured table written to `docs/STATUS.md`.

## 17. Definition of done

- All checklist items complete; all existing tests pass unmodified except the assertions this plan explicitly tightens.
- Collapsing or idling verifiably closes the WebRTC connection (not merely mutes), and reconnect is a single user action away with no repeated greeting.
- Spoken and typed long-form requests complete without truncation; confirmations stay short.
- Mock mode behavior is byte-for-byte unchanged from the user's perspective.
- `docs/STATUS.md` contains the measured before/after benchmark table; no unmeasured savings claims exist anywhere in the repo.
- `gpt-realtime-2.1` remains reachable via `LIFELENS_REALTIME_MODEL` with no code edit.

### Open questions left for Codex (implementation judgment)

1. Whether to add the optional `input_audio_buffer.speech_started` activity touch (§13) — recommended if trivially available in the event stream during the live smoke test.
2. Exact placement/styling of the paused notice and "Reconnecting…" badge within the existing panel markup (`LifeLensApp.tsx` render section / `components.css`) — cosmetic.
3. Whether the collapse timer lives in `LifeLensApp` (as designed) or is folded into `RealtimeClient` with a `reason: 'collapsed'` — either is acceptable if the state transitions in §7 hold.
4. Confirm at live-smoke time that `gpt-realtime-2.1-mini` accepts `reasoning.effort` at the `client_secrets` endpoint and that `input_image.detail` is accepted over the data channel (both documented, neither yet exercised against the live API from this codebase).
