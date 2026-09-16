# Milestone 6 — final hardening

Base: `lumi-agent-v2` at `aec5f57` (M5 merged). Branch `claude/m6-final-hardening`.

## Inspection findings (before any change)

- Realtime voice is one 2,100-line renderer class (`realtime.ts`) that speaks
  the OpenAI data-channel protocol directly: `sendEvent({type: ...})` and a
  `handleServerEvent` switch over OpenAI event names. The scripted harness
  speaks the same OpenAI protocol.
- Model calls outside realtime live in Electron main (`screen-reasoning.ts`,
  `scam-check.ts`) and call the OpenAI Responses API directly. The Python
  runtime runs no model at all and must keep not seeing provider keys (its
  settings deliberately never read the Electron `.env`).
- The durable criteria are `{specialty, day, HH:MM bounds, INR ceiling}`.
  Relative days are left to the model. One durable step per utterance.
- Packaged builds do not supervise the runtime at all (`if (!app.isPackaged)`).
  The runtime needs PostgreSQL (`DATABASE_URL`), the fixed venv python path, and
  Playwright's Chromium.
- Credentials available on this machine: Google ADC (authorized user) with a
  Vertex-enabled project. No OpenAI or DeepSeek key. Vertex probe results:
  `gemini-2.5-flash`, `gemini-2.5-flash-lite` (text) and
  `gemini-live-2.5-flash-native-audio` (Live, WebSocket with an
  `Authorization` header, which Electron 38's Node 22 `WebSocket` supports).

## Design

```text
voice provider (OpenAI WebRTC | Gemini Live via main relay | scripted)
        │ Lumi-owned VoiceProviderEvent / commands
        ▼
RealtimeClient (turn binding, tool routing, lifecycle)      text box in panel
        │ closed VoiceTaskCommand (incl. bounded plan)            │ text
        ▼                                                          ▼
main: VoiceTaskController ◄── TaskPlanExecutor ◄── TaskRequestInterpreter
        │                         (≤4 typed steps,     (ContextBuilder → ModelRouter
        │                          stops before          → strict JSON → TaskPlan,
        │                          approval)              rules fallback)
        ▼
AgentTaskController → runtime (tasks, search, criteria incl. dates, prepare,
                                 info lookup, approval, reconcile)
```

1. **Voice provider abstraction** (`src/renderer/src/voice/`). `RealtimeVoiceProvider`
   covers connect/close, session configuration, instruction updates, listening,
   user text/image, tool results, response create/cancel. Providers emit
   Lumi-owned events (`turn.committed`, `transcript.completed`, `response.started`,
   `response.text`, `response.done`, `tool.call`, `interrupted`, `error`,
   `closed`). `RealtimeClient` keeps every semantic (turn binding, dedup, idle,
   narration) and no longer names an OpenAI event.
   - `OpenAIRealtimeProvider`: WebRTC + data channel (live) or the scripted
     channel (tests). Unchanged wire behaviour.
   - `GeminiLiveProvider`: renderer audio capture/playback + a typed preload
     relay. Electron main owns the Vertex WebSocket and the Google access token
     (ADC authorized-user refresh or service-account JWT). The renderer never
     sees a Google credential; the relay accepts only closed message kinds.
2. **Model providers** (`src/main/models/`): `ModelProvider` with
   `OpenAITextProvider`, `GeminiVertexProvider`, `DeepSeekProvider`, a scripted
   provider for tests, and `ModelRouter` driven by a routing table keyed by task
   class (`intent_extraction`, `constraint_extraction`, `summarization`,
   `conversation`, `screen_understanding`, `difficult_reasoning`). Failover only
   ever repeats the *model call*; no model output executes anything.
3. **Context budgeting** (`context-builder.ts`): per-class input/output budgets,
   bounded sections (security rules, utterance, durable task state, recent
   events, memory with provenance, recent turns with older turns collapsed).
4. **Memory** (`agent-memory.ts`): preferences and episodic summaries in main's
   user-data directory, each with provenance. Precedence: current instruction >
   remembered preference; browser-observed facts > any memory.
5. **Relative dates** (`src/shared/relative-dates.ts`): deterministic resolver
   using the trusted local time zone from main. Durable criteria gain
   `date_from`/`date_to`; the runtime filters observed slots by the site-local
   date. "next Saturday" on a weekday is ambiguous and is asked back.
6. **Compound requests**: a `run_plan` voice command and the typed text path
   share `TaskPlanExecutor`: `search|refine` → `choose` (cheapest / earliest /
   number / time / doctor) → `prepare` → `show_for_approval`. At most 4 steps,
   fixed order, stops before any approval.
7. **Second workflow**: clinic information lookup (read-only) — a
   `clinic_info` durable task, reviewed `read_doctor_profiles` operation,
   `task.info_lookup_completed` event, voice/text commands. No action, no
   approval: it is a READ_ONLY, SAFE_TO_RETRY path.
8. **Packaged runtime**: `scripts/build-agent-runtime.mjs` assembles a
   relocatable CPython (uv-managed standalone build), locked dependencies,
   Chromium and the app code into `dist/agent-runtime`, shipped as an
   electron-builder extra resource. Main starts it in packaged builds from
   `resources/agent-runtime`, reads the database URL from a trusted user config
   file, runs migrations through a fixed entry point, and can run the bundled
   demo clinic site.
9. **Observability**: redacted structured diagnostics ring buffer in main.
10. **Evals**: `npm run eval` (deterministic TS + Python suites) and opt-in live
    provider smoke tests.

## Invariants at risk

| Change | Invariant | Guard |
| --- | --- | --- |
| provider abstraction | speech never approves | no approve tool/command in any provider; tests over tool lists of both providers |
| Gemini relay | renderer has no credentials | token lives in main; relay projections; boundary test |
| model router fallback | no duplicate external effect | router retries model calls only; plan execution happens once per request id |
| compound plans | approval only by trusted click | plan grammar has no approve/execute step; executor backend type omits them |
| dates | model does not invent calendars | model emits a phrase class; code computes dates; ambiguity asks |
| packaged runtime | no credentials bundled, no writable code | build script excludes `.env`; runtime under install dir; config read by main only |
