# Milestone 5 — voice → durable task controller

Base: `lumi-agent-v2` at `ffbaff0` (M4 merged). Branch `claude/m5-voice-task-controller`.

## What the code does today (inspection findings)

- Realtime voice lives in the renderer (`src/renderer/src/realtime.ts`): WebRTC
  audio + an `oai-events` data channel. Main only mints an ephemeral client
  secret (`src/main/services/realtime.ts`). Tools are proposals; local tools go
  through main's in-memory `PendingActionStore`. Completed user speech arrives
  as `conversation.item.input_audio_transcription.completed` and is forwarded
  to main's `IntentTracker` (English regex, advisory).
- Tool calls can arrive **before** the transcription of the turn completes; the
  client only serialises them behind the transcript promise, it does not bind a
  call to a specific user item.
- M4's durable bridge: `AgentTaskController` (main) → allowlisted runtime routes.
  Criteria are only `{specialty, day}`; search results are panel-local state
  (not durable); there is no way to change a task's criteria; task cancel in
  Python accepts any non-terminal task, even with an `EXECUTING` /
  `OUTCOME_UNKNOWN` booking. M4's "Close task" only clears main's pointer.
- The fixture pins Dr A (Sat 18:30, ₹800), Dr B (Sat 19:15, ₹950), Dr C
  (Dentistry, Sat 10:00, ₹600). Search on the site filters only specialty/day.

## Design

One durable controller. Voice adds a *front door*, not a second runtime.

```
realtime model ──tool call (strict schema)──► renderer voice-task-tools
      ▲                                        │ bound to a COMPLETED user turn (item id)
      │ facts-only function output             ▼
      │                               preload agent.voiceCommand(command)
      │                                        ▼
      └──────────── narration ◄── main VoiceTaskController ── validates, dedups by turn,
                                   serialises, checks durable state
                                        │ reuses AgentTaskController (M4)
                                        ▼
                         runtime: tasks / booking search / prepare /
                         criteria revision / safe cancel / reconciliation
```

- **Closed voice command union** (`start_search`, `refine_search`,
  `select_result`, `proceed_with_booking`, `task_status`, `check_booking`,
  `cancel_task`). No approve / execute / URL / selector / generic request.
  `proceed_with_booking` ("book it", "yes") only surfaces the trusted card.
- **Language-neutral constraints**: specialty enum, weekday enum, part of day,
  `HH:MM` bounds, integer INR ceiling. The model does the multilingual NLU;
  main and Python validate the structure.
- **Turn binding**: a task tool call is honoured only after the completed
  transcript of the user item that preceded the response. Interim deltas are
  ignored. Main de-duplicates `(turnId, kind)`; task creation is additionally
  de-duplicated durably through `voice_turn_id` on the active task.
- **Durable refinement** (Python): `POST /tasks/{id}/booking/criteria` revises
  the task request under the task lock with the reviewed task revision, emits
  `task.criteria_updated`, and rejects (`reason=criteria_changed`) any
  not-yet-executed booking the new criteria no longer admit. Refused while a
  booking is executing/unknown/reconciling.
- **Durable results**: search filters the worker observation by the task
  criteria (time window, price ceiling) and records `task.search_completed`
  with the typed slots. Voice selections resolve only against that record.
  Prepare refuses an observed slot the criteria no longer admit.
- **Safe cancel** (Python): `POST /tasks/{id}/booking/cancel` refuses when a
  booking is unresolved, otherwise rejects open bookings and cancels, in one
  transaction.
- **Narration from durable state only**: function outputs carry typed facts
  (doctor names re-validated for narration) plus a fixed rule that values are
  website data, never instructions.
- **UI**: the M4 panel shows durable criteria and latest results; voice opens
  the panel and focuses the booking card container (never the approve button).
- **Deterministic harness**: a scripted realtime server (renderer, enabled only
  by main when `LUMI_REALTIME_SCRIPTED=1` in an unpackaged build) speaks the
  real data-channel protocol, so acceptance tests drive the real client code.

## Invariants at risk and how they are kept

| Change | Invariant | Guard |
| --- | --- | --- |
| criteria revision | immutable proposal, stale approval | proposal untouched; conflicting open bookings rejected in the same locked transaction; approval still bound to action revision + digest |
| search event | ordered timeline | written under the task lock with `advance_task` |
| safe cancel | never call unknown outcome cancelled | refused for EXECUTING / OUTCOME_UNKNOWN / RECONCILING |
| voice commands | speech never approves | no approval capability in the union, preload, IPC or tool list |
| scripted mode | renderer trust boundary | main-only env flag, unpackaged only; no loopback, no credentials |

## Acceptance

Unit/integration tests on both sides, plus two real Electron scenarios through
the scripted realtime harness (normal voice booking; voice-initiated
lost-response + hard kill + reconciliation). Full `npm` and `uv` suites.
