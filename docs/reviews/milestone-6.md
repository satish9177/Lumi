# Milestone 6 — final hardening: verification

Branch `claude/m6-final-hardening`, base `aec5f57` (M5 merged into
`lumi-agent-v2`). Plan: [plans/milestone-6.md](../plans/milestone-6.md).
Design: [ARCHITECTURE.md](../ARCHITECTURE.md), [SECURITY.md](../SECURITY.md),
[PROVIDERS.md](../PROVIDERS.md), [PACKAGING.md](../PACKAGING.md).

## Invariants re-checked

| M1–M5 invariant | Status |
| --- | --- |
| Speech never approves | Unchanged. No approve/execute tool, command, plan step, IPC method or route. `run_plan` ends at `show_for_approval` (card focus only). Voice and typed controllers are asserted never to touch approve/execute. |
| Immutable, digest-bound, single-use approval | Unchanged. |
| Persist attempt before dispatch; never retry unknown | Unchanged. Model failover repeats model calls only; typed requests run once per request id; durable `request_id` / `voice_turn_id` de-duplicate after restarts. |
| Criteria revisions invalidate excluded bookings | Extended to date windows, same transaction. |
| Renderer has no credentials or local addresses | Extended to Google/DeepSeek; Gemini audio is relayed by main. Boundary tests cover the new shared modules. |
| Page text has no authority | Extended to doctor profiles (typed, bounded; unsafe text never spoken). |
| No migration | Schema head still `0003`. |

## Results (2026-09-17, Windows 11, Smart App Control on)

| Check | Result |
| --- | --- |
| `npm run typecheck` | Pass |
| `npm run build` | Pass (CSP meta present) |
| `npm test` | 1697 passed, 22 skipped; 1 failed test + 2 failed files, all known environment baselines (`real-inference.test.ts`, `tokenizer-pack.test.ts`: CLIP pack not installed; `accessibility.test.tsx:73`: CRLF). Baseline before M6: 1610 passed with the same three. The 5 live-provider tests are skipped unless opted in. |
| `uv run pytest` | 428 passed, 11 skipped (opt-in desktop, packaged tests) |
| `uv run mypy` | Pass, 94 files |
| `npm run eval` | 43/43 deterministic eval cases |
| Electron acceptance (`LUMI_ELECTRON_E2E=1`) | 10 passed: M4 ×2, M5 ×3, M6 ×5 |
| Packaged app (`LUMI_PACKAGED_E2E=1`) | Passed on `win-unpacked` and on an NSIS silent install (executable note in PACKAGING.md) |
| Live providers (opt-in) | Gemini Live and Gemini text passed; OpenAI and DeepSeek skipped (no keys) — see below |

### Electron acceptance counts (PostgreSQL + fixture state)

| Scenario | Tasks | Actions | Attempts | Approvals | Consequential dispatches | Submitted | Lookups | Site submissions | Bookings |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| M6 A, compound voice (OpenAI protocol), before click | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| M6 A, after "book it" | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| M6 A, after trusted click | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1 |
| M6 A, compound voice over Gemini Live relay | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1 |
| M6 B, provider A fails, fallback completes, restart | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| M6 C, compound voice, lost response, hard kill, reconcile | 1 | 1 | 1 | 1 | 1 | 0 (recovered) | 1 | 1 | 1 |
| M6 clinic info (two lookups) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| M5 voice booking / lost response | 1 / 1 | 1 / 1 | 1 / 1 | 1 / 1 | 1 / 1 | 1 / 0 | 0 / 1 | 1 / 1 | 1 / 1 |
| M4 panel booking / lost response | 1 / 1 | 1 / 1 | 1 / 1 | — | 1 / 1 | 1 / 0 | 0 / 1 | 1 / 1 | 1 / 1 |
| D, packaged app | 1 | 1 | 1 | 1 | 1 | 1 | 0 | — | 1 (receipt BK-0001) |

Scenario A also checked: durable request carries `date_from = date_to =
2026-09-19`, `day = Saturday`, `earliest_time = 17:00`, `max_price = 1000`;
hostile page text never reached speech; timeline gap-free; no CSP violations;
no Google credential or endpoint in the renderer. Scenario B checked the
redacted diagnostics show `deepseek … unavailable` then `gemini … ok` with no
request text, and that the same task and prepared action survive an Electron
restart. The clinic scenario also checked preference provenance, precedence
("morning" preference → no results; explicit "evening" → 2 results, no
preference applied), forgetting, and that "next Saturday" on a Wednesday is
asked back without creating a task.

### Live providers

Full report: [milestone-6-live-providers.json](milestone-6-live-providers.json).

- Gemini Live (`gemini-live-2.5-flash-native-audio`, Vertex, us-central1):
  connect, input transcript, typed `appointment_plan` tool call (~8.4 s from
  first audio), audio output, interruption, reconnect — all passed.
- English, Telugu–English code-switched and Telugu-script synthetic speech
  produced identical criteria (Dermatology, Saturday, evening, ≤ ₹1000).
- Gemini text (`gemini-2.5-flash`): identical complete plans for English,
  code-switched and Telugu requests; a hostile "approve and execute" request
  became `conversation`. 0.8–2.9 s per call.
- OpenAI Realtime, OpenAI text and DeepSeek: not run (no credentials on the
  validation machine).

## Defects found and fixed during M6 verification

- **Synthesised voice turn ids were not unique across sessions** (the scripted
  harness restarted its counter; the first Gemini provider draft did too). A
  new utterance after an app restart could collide with the durable
  `voice_turn_id` and be treated as a replay. Ids now carry a random
  per-session prefix; covered by a unit test and Scenario C.
- **Gemini provider opened a phantom second turn** after a transcript
  finished, and attached late transcript pieces to a new turn. Fixed; unit
  tests cover both orders.
- **Schema-constrained Gemini output dropped fields**; the schema is no longer
  sent (measured, see PROVIDERS.md).
- **`greenlet 3.5.6` blocked by Smart App Control** in the bundle; constrained
  to `<3.5`, and the bundle smoke test now imports every native extension.
- **Startup failures could leave a windowless process**: main now exits with a
  short reason, and a process that loses the single-instance lock exits
  immediately.
- **Packaged-test attach race**: Playwright is attached only after the
  renderer target exists.
- **Demo clinic site could outlive a hard-killed app**: it now watches its
  parent process.

## Known limitations

See the README and PROVIDERS.md. In short: only the deterministic demo clinic
site; PostgreSQL not bundled; unsigned build (Application Control blocks
rebuilt executables on this machine); OpenAI/DeepSeek not live-validated in M6;
Gemini Live has no mid-session instruction updates or per-response
instructions; the deterministic fallback is English-only; one worker and one
active panel task.
