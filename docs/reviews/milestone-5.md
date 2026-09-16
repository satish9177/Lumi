# Milestone 5 — voice → durable task controller: verification

Branch `claude/m5-voice-task-controller`, base `ffbaff0`. Plan:
`docs/plans/milestone-5.md`. Design summary: `docs/AGENT-RUNTIME.md`
("Voice task controller").

## Invariants re-checked against the change

| M1–M4 invariant | Status |
| --- | --- |
| Immutable proposal, digest-bound single-use approval | Unchanged. Refinement never edits a proposal; it rejects an excluded open booking in the same locked transaction. |
| Approval by action id + reviewed revision only | Unchanged. Voice has no approve/execute command, tool, IPC method or route; `VoiceTaskBackend` omits both (and a test proxy asserts they are never touched). |
| Persist attempt before dispatch; never retry unknown | Unchanged. Voice "book it" on an unknown outcome only narrates it; "check it" calls the existing read-only reconciliation. |
| Cancellation honesty | New safe cancel refuses `EXECUTING`/`OUTCOME_UNKNOWN`/`RECONCILING` and confirmed bookings. The M1 cancel route is untouched and not reachable from main. |
| Ordered timeline | New events (`task.criteria_updated`, `task.search_completed`) are written under the task lock with `advance_task`. |
| Renderer has no runtime/worker credentials or addresses | Unchanged; boundary test now also scans `voice-task-contracts.ts`. CSP unchanged. |
| Page text has no authority | Search results are typed slots; spoken doctor names are re-validated; function outputs carry a fixed data-only rule; hostile fixture text never reached speech, tool calls or recorded results. |

## Verification (2026-09-16, Windows)

| Check | Result |
| --- | --- |
| `npm run typecheck` | Pass |
| `npm run build` | Pass (CSP meta present) |
| `npm test` | 1610 passed, 17 skipped; 2 known baseline failures only: `real-inference.test.ts` and `tokenizer-pack.test.ts` (local CLIP assets missing from `%APPDATA%`). The M4-era `accessibility.test.tsx:73` CRLF failure did not occur in this checkout. |
| `uv run pytest` | 405 passed, 5 skipped (the five opt-in Electron tests, run below) |
| `uv run mypy` | Pass, 88 files |
| `LUMI_ELECTRON_E2E=1 uv run pytest tests/test_voice_acceptance.py` | 3 passed |
| `LUMI_ELECTRON_E2E=1 uv run pytest tests/test_electron_acceptance.py` | 2 passed (M4 regression) |

No migration was added (the new data lives in the existing `tasks.request`
JSONB and `task_events`), so `alembic upgrade head` is unchanged at `0003`.

One observation: in the first full run of the voice acceptance file, the second
test's Electron launch never loaded its renderer within 60 s. That test passed
alone and the whole file passed on the next full run; the cause was not
identified.

## Electron acceptance counts (PostgreSQL + fixture authoritative state)

| Scenario | Tasks | Actions | Attempts | Approvals granted | Consequential dispatches | Browser submissions | Bookings | Lookups | Duplicates |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A: voice request → "6:30 one" → "book it"/"yes"/"go ahead" → reconnect → trusted click | 1 | 1 | 1 | 1 | 1 (submitted) | 1 | 1 | 0 | 0 |
| A, before the click | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| B: voice-initiated, lost response, hard kill, restart, "what happened", "book it"/"yes"/select/cancel refused, "check it" | 1 | 1 | 1 | 1 | 1 (closed unknown) | 1 | 1 | 1 | 0 |
| Refinement: prepare Dr B → "under 900" withdraws it → prepare Dr A → voice cancel | 1 | 2 (both rejected) | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Scenario A also proves: interim transcripts create nothing; a replayed
transcript + tool call create nothing; the card region (not a button) holds
focus; hostile page text is present on the site throughout and never reaches
speech or tool calls; the timeline is gap-free.

## Known limitations / deferred to M6

- The scripted harness's language understanding is a small English rule set.
  Real multilingual (Telugu / code-switched) understanding depends on the live
  realtime model mapping speech onto the enum/HH:MM/integer tool fields; it was
  not exercised against the live model in this milestone.
- The model's free speech is steered, not enforced: facts are typed and the
  instructions forbid claiming outcomes, but a live model could still misspeak.
  The trusted card and timeline remain the authority.
- One durable step per utterance: compound requests ("find … and take the
  first") need a second utterance.
- Relative days ("tomorrow") are left to the model; there is no date resolver.
- Only the reviewed appointment fixture; INR-only voice price ceilings.
- Voice-turn memory in main is in-process (bounded); after a main restart, only
  task creation is protected durably (`voice_turn_id`). Realtime sessions do not
  survive a restart, so their turns cannot be replayed into a new one.
