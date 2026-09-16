# Lumi architecture

Lumi is a Windows desktop agent that can hold a voice or text conversation,
look things up on a reviewed website, prepare a booking, and — only after a
trusted click — perform it exactly once, even across crashes. Models suggest.
The durable controller decides and acts.

```text
            Voice / Screen / Typed request (user)
                              │
            ┌─────────────────▼──────────────────┐
            │ Realtime voice provider            │  OpenAI Realtime (WebRTC)
            │ (renderer; transport only)         │  Gemini Live (relayed by main)
            └─────────────────┬──────────────────┘
                              │ closed tool call, bound to a completed user turn
            ┌─────────────────▼──────────────────┐
            │ Lumi intent + task controller      │  VoiceTaskController (main)
            │  · strict plan / command parser    │  TaskRequestInterpreter (typed)
            │  · bounded plans (≤ 4 steps)       │
            │  · relative dates → calendar dates │
            └─────────────────┬──────────────────┘
                              │ typed operations only (no URL, selector, script)
            ┌─────────────────▼──────────────────┐
            │ Durable task runtime (Python)      │  FastAPI + PostgreSQL
            │  tasks · ordered timeline · ledger │  M1–M2
            └─────────────────┬──────────────────┘
       ┌──────────────┬───────┴─────────┬─────────────────┐
       ▼              ▼                 ▼                 ▼
  Policy /       Model router      Memory with       Trusted approval
  risk tiers     (cost/capability  provenance        (renderer click →
                  aware, main)     (main)             main → ledger)
                              │
            ┌─────────────────▼──────────────────┐
            │ Reviewed browser tools             │  isolated worker process,
            │  semantic operations only          │  closed registry, allowlisted
            └─────────────────┬──────────────────┘  origins (M3)
                              │
            ┌─────────────────▼──────────────────┐
            │ Verification + reconciliation      │  receipts, OUTCOME_UNKNOWN,
            │                                    │  read-only lookup (M2–M3)
            └────────────────────────────────────┘
```

## Processes

| Process | Owns | Never has |
| --- | --- | --- |
| Renderer (React) | UI, microphone/speaker, realtime transport, trusted approval card | Any credential except OpenAI's short-lived realtime secret; any path to the runtime, worker or Google |
| Preload | Fixed, typed IPC methods (`window.lifeLens`) | Generic IPC, Node APIs |
| Electron main | IPC validation, voice/text controller, model router, Gemini relay, memory, diagnostics, runtime supervision | Browser automation, database access |
| Agent runtime (Python) | Tasks, events, action ledger, approvals, attempts, recovery, reconciliation | Provider keys, the Electron `.env`, a shell |
| Browser worker (Python + Chromium) | One reviewed operation per dispatch against allowlisted origins | Database, approvals, credentials other than its own token |
| PostgreSQL | The single source of truth | — |

Milestones: M1 durable tasks · M2 action ledger and recovery · M3 isolated
browser worker · M4 secure desktop bridge · M5 voice → durable controller ·
M6 providers, routing, context, memory, dates, compound plans, second
workflow, packaging, evals (this document).

## Request lifecycle (compound voice request)

1. The user says *"Find me a dermatologist Saturday evening under ₹1000 and
   prepare the cheapest available option."*
2. The realtime provider (OpenAI or Gemini) transcribes the turn and emits one
   `appointment_plan` tool call. The renderer waits for the turn's *completed*
   transcript and binds the call to that turn id.
3. The shared wire parser (`src/shared/plan-wire.ts`) accepts only enums,
   bounded integers, `HH:MM` strings and a relative-day *kind*. Main parses it
   again (`parseVoiceTaskCommand`).
4. `VoiceTaskController.runPlan` resolves "Saturday" to a calendar date with
   the trusted clock and time zone, fills gaps from remembered preferences
   (never overriding what was said), creates **one** durable task, runs a
   read-only search, picks the cheapest recorded result by rule, and prepares
   **one** booking from what the worker observes *now*. Then it stops.
5. "Book it" maps to `show_for_approval`: the card is focused, nothing is
   approved. Only a click on **Approve and book** in the trusted panel calls
   approve + execute, bound to the reviewed action revision and digest.
6. Execution, lost responses, `OUTCOME_UNKNOWN` and read-only reconciliation
   behave exactly as in M2–M5.

The typed path ("Ask Lumi" box) is the same from step 3 onward; step 2 is
replaced by the model router (`intent_extraction`) with a deterministic
English fallback.

## Key modules added in M6

| Area | Files |
| --- | --- |
| Voice providers | `src/renderer/src/voice/{voice-provider,openai-realtime-provider,gemini-live-provider,pcm-audio}.ts`, `src/main/voice/{gemini-live-relay,scripted-gemini-socket}.ts`, `src/shared/voice-relay-contracts.ts` |
| Text providers and routing | `src/main/models/{provider,text-providers,model-router,model-config,google-auth,scripted-provider}.ts` |
| Context and memory | `src/main/models/context-builder.ts`, `src/main/agent/agent-memory.ts` |
| Plans and dates | `src/shared/{plan-wire,relative-dates,rule-interpreter}.ts`, `src/main/services/voice-task-controller.ts`, `src/main/agent/task-request-interpreter.ts` |
| Second workflow | `services/agent/app/services/clinic_info.py`, `read_doctor_profiles` in the fixture adapter |
| Observability | `src/main/agent/diagnostics.ts` |
| Packaging | `scripts/build-agent-runtime.mjs`, `src/main/agent/packaged-runtime.ts`, `services/agent/app/migrate.py` |
| Evals | `scripts/run-evals.mjs`, `src/main/voice/providers.live.test.ts` |

See also [SECURITY.md](SECURITY.md), [PROVIDERS.md](PROVIDERS.md),
[PACKAGING.md](PACKAGING.md), [EVALS.md](EVALS.md),
[DEVELOPMENT.md](DEVELOPMENT.md) and the detailed runtime design in
[AGENT-RUNTIME.md](AGENT-RUNTIME.md).
