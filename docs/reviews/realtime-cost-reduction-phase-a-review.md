# Independent review — Realtime API cost reduction, Phase A

- **Review date:** 19 July 2026 (Asia/Kolkata)
- **Reviewer:** Claude Code (independent code and architecture review; no implementation, test, or doc-content changes were made outside this new review file)
- **Canonical specification:** [docs/plans/realtime-cost-reduction-phase-a.md](../plans/realtime-cost-reduction-phase-a.md)
- **Reviewed state:** uncommitted working tree on `main` at HEAD `0ed3245`

## 1. Executive verdict

**APPROVE WITH NON-BLOCKING CONCERNS.**

The uncommitted working tree faithfully implements every Phase A requirement (A1–A5, §8 budgets, lifecycle §7, tests §10, docs §9) with no Phase B work, no scope creep into the non-goal systems, and honest documentation that claims no unmeasured savings. Typecheck, the full 205-test suite, the production build, and `git diff --check` all pass on this tree as re-run during this review. Two medium-severity lifecycle gaps (stale call IDs crossing into a newer session; unbounded disconnect deferral while a confirmation is abandoned) and several low-severity items should be addressed by Codex, but none blocks committing this diff or proceeding to the live smoke test.

## 2. Release blockers

None.

## 3. High-severity findings

None.

## 4. Medium-severity findings

### M1. Stale call IDs from an ended session can be sent into a newer session

- **Severity:** Medium
- **Affected:** `src/renderer/src/realtime.ts:738-742` (`sendFunctionCallOutput` guard); `src/renderer/src/LifeLensApp.tsx` (`pendingAction` confirmation card and `pendingScreenCaptureCallId`, which survive `onSessionEnded`); `FileSearchController` resolutions and the async `withPolicyDecision` promise (`realtime.ts:960-971`).
- **Expected:** Old asynchronous callbacks can never send results into a *newer* session (review criterion C).
- **Actual:** The stale-send guard checks only `mode !== 'live' || dataChannel?.readyState !== 'open'`. It correctly no-ops while disconnected, but there is no session-identity (generation) check. A call ID captured in one session — a still-displayed confirmation card, a stored `pendingScreenCaptureCallId`, or a slow search resolution — that fires after a disconnect **and** reconnect finds the new channel open and sends a `function_call_output` whose `call_id` the new server conversation has never seen. The server replies with an `error` event, which `handleServerEvent` surfaces via `onError` and `onState('error')`.
- **Reachability:** The `pendingCallIds` deferral protects the idle and collapse teardown paths (they refuse to disconnect while a call is outstanding), so the exposure is via the error-disconnect path (`connectionState === 'failed'`) and the always-available manual “Connect voice” button, both followed by confirming a still-visible card from the old session. This exact hole predates Phase A, but Phase A makes teardown/reconnect a routine event rather than a rarity, so exposure is materially increased.
- **Concrete risk:** After a legitimate, locally successful confirmed action, the user sees an error banner and the companion enters the `error` state. No data or safety impact — the main-process action executes correctly and trust gating is unaffected.
- **Recommended correction:** Add a monotonically increasing session generation counter in `RealtimeClient`, incremented in `disconnect()`. Record the generation when a call ID enters `pendingCallIds`/leaves `handleToolCall`, and have `sendFunctionCallOutput` (or its callers) drop outputs whose generation is not current. Additionally, clearing `pendingAction` and `pendingScreenCaptureCallId` in the `onSessionEnded` handler would remove the App-level vectors.

### M2. Abandoned pending work defers disconnect indefinitely

- **Severity:** Medium
- **Affected:** `src/renderer/src/realtime.ts:1099-1123` (`handleIdleTimeout` / `hasPendingWork` re-arm), `src/renderer/src/LifeLensApp.tsx:88-96` (`disconnectAfterPendingWork` 1-second retry loop, `COLLAPSE_RETRY_MS`).
- **Expected:** Pending responses and function calls defer disconnect *safely* — i.e., for a bounded time.
- **Actual:** A tool call that is never answered keeps `pendingCallIds` non-empty forever: a confirmation card the user ignores, or a screen-context request waiting on a source pick (`pendingScreenCaptureCallId`), never resolves. The idle timer then re-arms every 4 minutes indefinitely, and after collapse the App retries `endSessionWhenIdle('collapsed')` every second indefinitely. The session stays connected and muted — and per the plan’s own §2.2, a muted WebRTC track still transmits billable silence frames.
- **Concrete risk:** In the “model proposes an action, user walks away / collapses the orb” scenario, the always-on billing problem Phase A exists to eliminate silently returns, uncapped.
- **Recommended correction:** Bound the deferral: after a hard cap (e.g., 2–3 idle re-arms or ~10–15 minutes of deferral), locally decline outstanding calls (`sendFunctionCallOutput(callId, { ok: false, … })` while the channel is still open, or simply drop them) and disconnect. The local confirmation card can stay on screen; combined with the M1 fix, a later confirm still executes locally and no-ops toward the departed model, which the plan already declares acceptable (§13, “Tool output after teardown”).

## 5. Low-severity findings

### L1. Photo downscale caps width only, not the long side

- **Severity:** Low
- **Affected:** `src/main/services/capture.ts:85-95` (`encodeCaptureImage` `maxWidth` option), `src/main/services/tools.ts:97-102`.
- **Expected (review criterion E):** photo long side limited to 1024 px.
- **Actual:** Only width is capped; a portrait photo (e.g., 900×1800) keeps its full height. This **matches the plan exactly** (§A4.2–3 specifies `maxWidth: 1024` with width-only resize), so it is a plan-versus-checklist discrepancy, not an implementation deviation.
- **Concrete risk:** Minimal. With `detail: 'low'` the server resizes to a fixed small budget regardless of input dimensions, so image-token cost is unaffected; the only impact is transport bytes, already bounded by the 150 KB cap and the quality ladder.
- **Recommended correction:** Optional — generalize to `maxDimension` (cap the long side) if Codex wants the checklist wording to hold literally. Not required for correctness or cost.

### L2. The stale-send guard can also silence a genuinely broken live channel

- **Severity:** Low
- **Affected:** `src/renderer/src/realtime.ts:738-742`.
- **Expected (review criterion C):** valid active-session errors are not accidentally swallowed.
- **Actual:** If the data channel closes unexpectedly while `connected` is still `true` (before the `connectionState === 'failed'` handler runs), tool outputs silently vanish with no user feedback, where previously `sendEvent`’s throw surfaced an error. The window is narrow and the `onconnectionstatechange` handler covers real failures shortly after, but the intermediate state gives no signal.
- **Concrete risk:** A confirmed action’s result is occasionally never spoken and nothing indicates why.
- **Recommended correction:** In the guard, when `this.connected` is still `true` but the channel is not open, emit a debug log (or a single non-error notice) instead of a pure silent return, so live smoke testing can observe the case.

### L3. Unplanned change to the capture prompt text

- **Severity:** Low
- **Affected:** `src/renderer/src/realtime.ts:508` (`sendCapture` message text: “give a short answer with dates, links, and next actions” → “include useful dates, links, and next actions”).
- **Expected:** The plan’s instruction changes are limited to the `SYSTEM_INSTRUCTIONS` last-sentence swap (§8).
- **Actual:** The per-capture message text was also reworded. It is *consistent* with the §8 intent (a “short answer” directive would fight the 2048 long-form ceiling), but it was not specified, and it changes model behavior for every capture explanation.
- **Concrete risk:** None identified; behavior change is in the intended direction. Flagged so it is a conscious decision in the commit, not an accident.
- **Recommended correction:** Keep it; mention it in the commit message.

### L4. Confirmation budget (192) also governs spoken search-result enumeration

- **Severity:** Low
- **Affected:** `src/renderer/src/realtime.ts:761-765` (`sendFunctionCallOutput` `response.create`), reached from `completeFileSearch`.
- **Expected:** Search results are read back by number without truncation.
- **Actual:** The plan itself assigns site #5 the confirmation budget, and the implementation follows it — but this same budget covers the response that enumerates a multi-file search result list aloud, which is longer than an acknowledgement.
- **Concrete risk:** A spoken result list may truncate at ~192 audio tokens on live hardware.
- **Recommended correction:** Watch for truncation at §11 step 4 of the live smoke test; if observed, give `completeFileSearch`’s path the `normal` (512) budget while leaving confirmations/greeting at 192. Plan §8 already sanctions raising ceilings over adding complexity.

### L5. Collapse timer gap when a connect is in flight at fire time

- **Severity:** Low
- **Affected:** `src/renderer/src/LifeLensApp.tsx:88-96` (`disconnectAfterPendingWork` returns without rescheduling when `!client?.isLiveConnected()`).
- **Expected:** A collapsed session is always disconnected after the grace period.
- **Actual:** If the 60-second collapse timer fires while a connection attempt is still in flight, the function returns without retrying, and the session that finishes connecting moments later carries no collapse timer. In practice this is nearly unreachable — the credential fetch (10 s), SDP negotiation (10 s), and channel-open wait (15 s) bound a connect attempt well under 60 s, and connects are only initiated from expanded-panel actions — and the safety net holds: the connect-time `touchActivity()` arms the idle timer, the mic is muted (`track.enabled = this.listening` plus `turn_detection` from `this.listening` in the initial session update), so the session self-terminates within 4 minutes.
- **Concrete risk:** Up to ~4 minutes of billed muted audio in a rare interleaving.
- **Recommended correction:** Optional — have `disconnectAfterPendingWork` reschedule (`COLLAPSE_RETRY_MS`) when a connect promise is pending instead of returning.

## 6. Requirement-by-requirement compliance

Statuses: ✅ verified in code and covered by a passing test · ✔ verified in code, no direct automated test · ⏳ pending live verification.

| # | Requirement | Implementation | Test | Status | Notes |
|---|---|---|---|---|---|
| A1 | Default model `gpt-realtime-2.1-mini` | `src/main/services/realtime.ts:4` | `src/main/services/realtime.test.ts` “defaults to gpt-realtime-2.1-mini…” | ✅ | Asserts the mocked `client_secrets` request body; `vi.resetModules()` + dynamic import handles the module-load-time read. |
| A1 | `LIFELENS_REALTIME_MODEL` override / flagship reachable | same file | “honors the gpt-realtime-2.1 flagship override” | ✅ | Env stubs isolated via `vi.unstubAllEnvs()` + original-value restore in `afterEach`. |
| A1 | `.env.example` updated | `.env.example:2-3` | n/a | ✔ | Default + flagship comment as specified. |
| A2 | Collapse mutes instantly | `LifeLensApp.tsx:85` (`setListening(expanded)` unchanged) | mic-privacy tests (updated mocks) | ✅ | Narrow `turn_detection`-only update preserved (`realtime.ts:281-293`). |
| A2 | Collapse disconnect after 60 s | `COLLAPSE_DISCONNECT_MS = 60_000` (`realtime.ts:44`), collapse effect (`LifeLensApp.tsx:88-107`) | client-level `endSessionWhenIdle` test only | ✔ | No App-level test of the timer arming/clearing (see §7). |
| A2 | Idle disconnect after 4 min | `IDLE_DISCONNECT_MS`, `touchActivity`/`handleIdleTimeout` (`realtime.ts:1099-1123`) | “ends an inactive live session after four minutes” | ✅ | Fake timers; asserts `onSessionEnded('idle')`, channel `close()`, `isConnected() === false`. |
| A2 | Pending response defers disconnect | `hasPendingWork()` (`realtime.ts:1113-1115`) | “re-arms the idle timer while a response is active” | ✅ | See M2 for the unbounded-deferral concern. |
| A2 | Pending function call defers disconnect | `pendingCallIds` add/delete (`realtime.ts:906, 937, 375, 739`) | “re-arms while a tool call is unanswered…” | ✅ | All exit paths delete: `sendFunctionCallOutput` (first line, before the guard), `completeFileSearch`, unknown-tool early return. |
| A2 | Timers inert in mock mode | `isLiveConnected()` gate in `touchActivity` | “never arms cost-saving teardown in mock mode” | ✅ | Connects a real mock session and advances 2× the idle window. |
| A2 | Timer cleanup on disconnect/unmount | `disconnect()` → `clearIdleTimer()` (`realtime.ts:542`); collapse-effect cleanup (`LifeLensApp.tsx:101-105`); unmount effect calls `disconnect()` (`LifeLensApp.tsx:129`) | client-level only | ✔ | Effect cleanup clears the latest timer id (shared `let` binding — retry reassignment is covered). |
| A2 | Rapid collapse/reopen leaves no stale timer | React effect cleanup on `expanded` change | none | ✔ | Structurally sound; untested (see §7). |
| A2 | Concurrent reconnects share one attempt | `connectPromiseRef` (`LifeLensApp.tsx:158-201`); `connect()` begins with `disconnect()` | none | ✔ | Promise cleared only when it is still the current one; failure rethrows so callers surface errors. |
| A2 | Lazy reconnect: questions / captures / photo confirm | `ensureConnected()` used in `askQuestion` (:306), `captureScreen` (:260), `confirmPendingAction` photo branch (:579-584); `openCompanion` keeps its check | none | ✔ | Matches the plan’s three named call sites exactly. |
| A2 | Greeting once per app run | `greetAfterInitialSessionUpdate` (`realtime.ts:799-801`); `hasConnectedOnceRef` + `greet:` flag (`LifeLensApp.tsx:190-197`) | “does not repeat the greeting when a reconnect opts out” | ✅/✔ | Client behavior tested, but by forcing the private field — the `connect({greet})` plumbing and the App ref are untested (§7). |
| A2 | Reconnecting / paused UI consistent | status-row conditional + `pausedNotice` (`LifeLensApp.tsx:661, 702`); `onSessionEnded` handler clears to `idle` + notice; exact plan string used | none | ✔ | `connectVoice` clears the notice and error on each attempt; failure path replaces notice with error, per plan §13. |
| A2 | Reconnect failure doesn’t stick | `connectVoice` catch → `error` state + message, `finally` clears `isConnecting`; `askQuestion`/`captureScreen` catches | none | ✔ | Verified by reading all `ensureConnected` callers. |
| A2 | Local UI survives teardown | `onSessionEnded` sets state + notice only | none | ✔ | Results, thumbnails, transcript, capture preview untouched. |
| A2/C | Stale sends silently no-op after teardown | guard at `realtime.ts:739-742` | “silently ignores tool outputs that arrive after disconnect” | ✅ | Asserts zero events **and** zero `onError` calls. See M1 for the newer-session gap and L2 for the swallow edge. |
| A3 | `gpt-4o-mini-transcribe` exact | `realtime.ts:46` | tightened initial-payload assertion (exact string) | ✅ | |
| A3 | Transcript → IntentTracker intact | `handleUserTranscript` → `intentUpdate` chain unchanged (`realtime.ts:878-895`); fires only on `input_audio_transcription.completed`, never assistant text | existing voice-intent plumbing suite (passing) | ✅ | Trust gating unchanged; `requestFileSearch` still serializes behind `intentUpdate`. |
| A4 | Screen `detail: 'auto'` | `realtime.ts:510, 1094` | “marks screen captures as auto detail…” asserts outbound payload | ✅ | `sendCaptureForRequest`’s part verified by reading code; test covers `sendCapture`. |
| A4 | Photo `detail: 'low'` | `realtime.ts:339` | photo test asserts exact outbound `input_image` part | ✅ | |
| A4 | Photo ≤1024 px wide | `tools.ts:102`; `capture.ts:87-91` | `capture.test.ts` maxWidth test; `tools.test.ts:180` bound tightened to 1024 | ✅ | Width-only — see L1. `tools.test.ts` verifies the real `analyze_photo` output, not just the helper. |
| A4 | Aspect ratio preserved, valid dimensions | width-only `resize`, `Math.round`, guards `Number.isFinite && > 0` | aspect assertion (`toBeCloseTo`) | ✅ | No zero/negative dimension path. |
| A4 | 150 KB cap, transport-only, documented | `capture.ts:8-9` + comment | existing byte-cap tests (use exported constant) | ✅ | Comment states bytes ≠ tokens, as required. |
| A5 | Initial update complete | `sendSessionUpdate` (`realtime.ts:706-729`): instructions, tools, tool_choice, `max_output_tokens: 1024`, transcription, turn_detection | initial-payload test | ✅ | `turn_detection: this.listening ? … : null` in the initial payload is a beyond-plan addition that correctly closes the collapse-during-connect race. |
| A5 | Later updates instruction-only, deduped | `updateLiveSessionInstructions` (`realtime.ts:692-704`) | “deduplicates unchanged instructions…” asserts exactly 2 updates, no `tools`/`audio` keys | ✅ | |
| A5 | Cache reset on disconnect / fresh reconnect config | `disconnect()` resets `lastSentInstructions` (`realtime.ts:547`) | none directly | ✔ | Functionally safe even without the reset (initial update is unconditional); untested (§7). |
| A5 | Listening updates not suppressed | `setListening` narrow update untouched | existing mic-privacy tests | ✅ | |
| §8 | Session default 1024 | `realtime.ts:717` | initial-payload assertion | ✅ | Comment documents the VAD-only-control limitation, as required. |
| §8 | Confirmation ≈128–256 | `RESPONSE_BUDGETS.confirmation = 192`: greeting (:668), function-call outputs (:764) | greeting + `completeFileSearch` budget tests | ✅ | See L4 for the search-enumeration nuance. |
| §8 | Normal 512 | `sendUserRequest` → `pickResponseBudget('question', text)` | “uses the normal ceiling for a simple typed request” | ✅ | |
| §8 | Long-form 1600–2400 | 2048 at `analyzeSelectedPhoto` (:345), `sendCapture` (:516), `sendCaptureForRequest` (:1090), cue-matched questions | detailed-request, capture, and photo budget tests | ✅ | All six §2.6 sites carry a budget; no missed `response.create`. |
| §8 | Cue regex reasonable | `LONG_FORM_CUE` (`realtime.ts:52`) | positive case tested; negatives untested | ✔ | Mirrors the plan’s example list verbatim (adds `summarise` spelling). Worst case a wrong *ceiling*, never truncation of legitimate detail below 512. |
| §8 | Correct payload placement | session-level `session.max_output_tokens`; response-level `response.max_output_tokens` | payload-shape tests | ⏳ | Matches plan/docs; never exercised against the live API — plan open question 4. |
| §8 | Instruction policy swap | `SYSTEM_INSTRUCTIONS` last sentence → the three specified sentences (`realtime.ts:76-78`) | prefix-invariant tests still pass | ✅ | No timestamps/filenames invariants intact. |
| §13 | `speech_started` activity touch | `realtime.ts:806-809` | none | ✔ | Optional hardening implemented. |
| Non-goals | No Phase B; no changes to IntentTracker/orchestrator/search/ranking/contracts/mock | `git diff --name-only`: none of those files modified | full suite passes | ✅ | 12 modified files match plan §9’s map exactly; no local STT/TTS/routing code anywhere in the diff. |
| Docs | DECISIONS / STATUS | see §10 below | n/a | ✅ | |

## 7. Missing or weak test coverage

1. **No `LifeLensApp`-level tests at all** (none existed before either; the plan’s test plan §10 did not require them). Consequently untested: collapse-timer arming/clearing/1-second retry, rapid collapse/reopen, `ensureConnected` sharing one in-flight connect, `hasConnectedOnceRef` greet-once wiring, `onSessionEnded` → paused notice, “Reconnecting…” badge.
2. **Greet-once is tested by forcing the private field** `greetAfterInitialSessionUpdate` rather than through `connect(credential, { greet: false })`. A regression in the `options.greet ?? true` plumbing or the App ref would pass the suite.
3. **No fresh-config-after-reconnect test:** nothing proves `lastSentInstructions` resets on `disconnect()` (i.e., that an instruction update identical to the pre-disconnect value is *not* suppressed in a new session).
4. **No test for the M1 gap** — a call ID from session N answered while session N+1’s channel is open.
5. **`sendCaptureForRequest`** (`detail: 'auto'`, long-form budget, held-call output without response) has no direct test; only `sendCapture` is covered.
6. **`input_audio_buffer.speech_started` touch** untested.
7. **Long-form cue negatives** untested (e.g., a request merely containing “story” as a filename gets 2048 — harmless, but undocumented by tests).
8. Tests that pass but prove less than they appear to: the mock-mode timer test proves the idle timer never *arms* in mock (sufficient); the deferral tests force `responseActive` via private-field pokes rather than a `response.created` event round-trip (acceptable, slightly weaker).

## 8. Live verification checklist (all pending — no API key was available; nothing below is claimed)

- [ ] `client_secrets` request body shows `"model":"gpt-realtime-2.1-mini"`; mini accepts `reasoning.effort` (plan open question 4).
- [ ] Session connects and greets exactly once per app run; reconnects never re-greet.
- [ ] `session.update` with session-level `max_output_tokens` and `input_image.detail` are accepted live (plan §13 fallback: drop `detail` from screen captures first if rejected).
- [ ] Spoken “find my latest resume”: transcript → trusted auto-run search → results spoken by number, with **no truncation** of the enumeration (watch L4; raise site #5 to 512 if truncated).
- [ ] Collapse: instant mute; connection closes within ~60 s (webrtc-internals); reopen + typed question shows “Reconnecting…”, answers, no repeated greeting.
- [ ] Expanded idle 4+ min: paused notice appears; next question reconnects and answers.
- [ ] Capture + “explain this page in detail”: complete untruncated answer; data channel shows 2048; a confirmation follow-up shows 192.
- [ ] Photo select/clear + folder-approval toggling: `session.update` only on real instruction change, never containing `tools` after the initial one.
- [ ] Mock mode (no key): unchanged behavior, open >5 min with no self-termination.
- [ ] Confirm-after-reconnect scenario (M1): confirm a pending card after an error-disconnect + reconnect and observe whether the server error banner appears.

## 9. Cost-benchmark checklist (pending — requires operator key and usage-dashboard access)

- [ ] Run plan §12’s scripted ~10-minute sequence on pre-change `main` (before) and on this tree (after), same machine/key, sessions separated on the dashboard.
- [ ] Record per run from platform.openai.com/usage: realtime audio in/out tokens, text in/out tokens, image tokens, transcription minutes/cost, request counts, total cost.
- [ ] Replace the eight “Not measured” rows in `docs/STATUS.md` with measured values only; compute % change per line item.
- [ ] No percentage may be claimed anywhere (docs, commit message) until this table is filled from real dashboard data. The current STATUS.md correctly claims none.

## 10. Git and security review

- **HEAD `0ed3245`** (“feat: complete and package LifeLens Windows MVP”) touches exactly `package.json` (build icon, nsis indentation, `npmRebuild: false`, electron `^38.0.0` → `^38.8.6`) and `package-lock.json` — as reported. **No Phase A content is in it, and no `package.json`/`package-lock.json` change is mixed into the Phase A working diff.**
- The uncommitted diff covers exactly the 12 files in plan §9’s file map (6 source, 4 test, `.env.example`, 2 docs) plus untracked `docs/plans/realtime-cost-reduction-phase-a.md` (the plan itself, the only file under `docs/plans/`).
- **No secrets:** no API keys, tokens, or credentials anywhere in the diff; `.env.example` contains placeholders only. No absolute local paths, logs, or generated files. STATUS.md’s narrative mentions the Codex sandbox/build circumstances but exposes nothing sensitive.
- `git diff --check` — clean (no whitespace errors). Git warns LF→CRLF for the modified files, consistent with the repo’s existing state on Windows; not introduced by this change.
- **Verdict:** the uncommitted diff is safe to preserve and to commit as one Phase A change. Recommend `git add docs/plans/` (and this `docs/reviews/` file, if desired) so the specification travels with the implementation commit.

## 11. Commands run and results

All run on this working tree during the review; no code was modified before or after.

| Command | Result |
|---|---|
| `git status --short` | 12 modified files + untracked `docs/plans/` (list matches plan §9) |
| `git diff --stat` | 12 files changed, 664 insertions(+), 93 deletions(-) |
| `git diff --name-only` / `git diff` (full) | Read in full; findings above derive from it |
| `git show --stat --oneline 0ed3245` | `package-lock.json` (2±), `package.json` (18±) only |
| `git diff --check` | Clean |
| `npm.cmd run typecheck` | Passed (`tsc --noEmit`, no output) |
| `npm.cmd test` | **16 files, 205 tests, all passed** (31.1 s) |
| `npm.cmd run build` | Passed — main 98.93 kB, preload 4.90 kB, renderer built |
| Live API calls / benchmark | **Not run** — not authorized, no key, no dashboard data |

## 12. Recommended next action for Codex

1. Fix **M1**: add a session-generation guard in `RealtimeClient` and clear `pendingAction` / `pendingScreenCaptureCallId` in the `onSessionEnded` handler; add the regression test from §7 item 4.
2. Fix **M2**: cap total disconnect deferral (decline-and-disconnect after a bounded number of re-arms), for both the idle timer and the collapse retry loop; add a fake-timer test.
3. Optionally address L2 (debug signal in the guard), L5 (reschedule the collapse retry while a connect is in flight), and the §7 coverage gaps (greet plumbed through `connect` options; fresh-config-after-reconnect; `sendCaptureForRequest` payload).
4. Commit the current diff (plus `docs/plans/`) as the single Phase A change — the fixes above can be a small follow-up commit; nothing here requires reworking the committed baseline.
5. Proceed to the §8 live smoke checklist with an operator key, watching specifically for `input_image.detail` / session `max_output_tokens` acceptance and L4 truncation; then run the §9 benchmark and fill the STATUS.md table with measured values only.

---

### Review summary

- **Verdict:** APPROVE WITH NON-BLOCKING CONCERNS
- **Blockers:** 0 · **High:** 0 · **Medium:** 2 (M1, M2) · **Low:** 5 (L1–L5)
- **Verification re-run by reviewer:** typecheck ✅ · 205/205 tests ✅ · production build ✅ · `git diff --check` ✅ · live smoke and benchmark **pending** (not authorized)
- No implementation, test, or documentation content was modified by this review; only this review file was created. Nothing was committed or pushed.

---

# Targeted M1/M2 Follow-up Review

- **Review date:** 19 July 2026 (Asia/Kolkata), follow-up to the Phase A review above
- **Reviewer:** Claude Code (review only; no implementation, test, or doc-content changes outside this section)
- **Reviewed state:** uncommitted working tree on `main` at HEAD `0ed3245` — now 14 modified files, 1,314 insertions / 234 deletions (the hardening added `src/renderer/src/file-search-controller.ts` and its test to the previously reviewed 12-file diff)

## 1. Verdict

**APPROVE WITH NON-BLOCKING CONCERNS.**

- **M1 (session identity protection): RESOLVED.** The renderer-wide monotonic generation, `(generation, callId)` scoping, and open-channel validation are implemented consistently across every server-scoped asynchronous flow, with regression tests.
- **M2 (bounded teardown): SUBSTANTIALLY RESOLVED, with one residual medium gap (F1).** The indefinite 1-second collapse retry loop and the indefinite idle re-arm are gone; the 60 s / 4 min / +120 s / 3 min / 6 min bounds are implemented and tested. One reachable interleaving (reopen during a collapsed deferral after the idle timer was consumed) can still leave a live session with **no armed teardown timer**, restoring unbounded silent billing in that corner. One-line fix recommended before or alongside the live smoke test.

## 2. M1 verification detail

All ten M1 criteria verified in code:

1. **Monotonic generations:** module-level `nextRealtimeSessionGeneration` (`realtime.ts:59`) is renderer-wide and only ever incremented (`connect`, `realtime.ts:256`). Because `LifeLensApp.connectVoice` constructs a **new** `RealtimeClient` per connect, the module-level (not per-instance) counter is what makes cross-client collisions impossible — correct design.
2. **Invalidation moments:** manual reconnect (`connectVoice` → `disconnect()` returns the ended generation → `expireServerGeneration`, `LifeLensApp.tsx:196-198`); connection error (`failLiveConnection` → `disconnect` → `onSessionEnded('error', gen)`); idle/collapsed/hard-deadline teardown (`endLiveSession`); replacement (`connect()` begins with `disconnect()`); unmount (`disconnect()` in the cleanup effect). All clear `activeGeneration`, `dataChannelGeneration`, `pendingCallGenerations`, and all three timers synchronously.
3. **Every async flow captures and validates its generation:** tool results (`sendToolResult` → `sendFunctionCallOutput` guard), `function_call_output` (`isServerCallActive` at `realtime.ts:805`), policy/approval (`withPolicyDecision` gates both resolve and fallback), file-search completion/resume (`FileSearchController` + `answeredCallIds` keyed `generation:callId`), Telegram (`requestTelegramRecipientSearch` gates result, error, and `finally` paths), captures (`captureScreen`, `loadCaptureSources`, `sendCaptureForRequest` all gate after each await), confirmations (`preparePendingAction`, `confirmPendingAction` via `isPendingProposalCurrent`), deferred callbacks (`requestFileSearch` gates after the `intentUpdate` chain).
4. **Dual validation:** `isServerCallActive` requires `mode === 'live'` ∧ `activeGeneration === gen` ∧ `dataChannelGeneration === gen` ∧ channel `open` ∧ a live `pendingCallGenerations` entry for that call. Both halves of the requirement hold.
5. **Old results silently no-op:** guards return before any send; old-channel messages are additionally dropped by the channel-identity check in `onmessage` and the generation check at the top of `handleServerEvent`, so an old session's server `error` event can never raise a banner.
6. **Current errors not swallowed:** current-generation server `error` events surface (`realtime.ts:856-861`); a send failure after the guard passes still routes to `onError` (`realtime.ts:834-836`). The one pre-existing narrow swallow (channel closed while `connected` is still true → silent no-op, original finding L2) is unchanged and remains low.
7. **Resume-token collisions:** `searchCorrelationId` = `` `${generation}:${callId}` `` is what travels to main as the search `callId` and is matched in `resolve`; the raw server call ID is kept for the Realtime output. Same-ID-two-generations is covered by tests in both `realtime.test.ts` ("rejects an old tool result after manual reconnect and accepts the current call with the same ID") and `file-search-controller.test.ts` ("keeps same-ID search resumes isolated by session generation").
8. **Server-backed UI expiry:** `expireServerGeneration` (`LifeLensApp.tsx:154-180`) cancels generation-matched search controller state (+ `cancelFileSearch`), the search confirmation card, the pending screen-capture picker, the Telegram in-flight lookup, and every generation-matched pending proposal (including the main-process `cancelPendingAction`).
9. **Local UI survives:** expiry only touches generation-matched items; results, thumbnails, transcript, capture preview, and user-initiated (serverCall-less) proposals are untouched.
10. **Stale callbacks cannot touch newer-generation state:** every App-level callback re-checks `isServerCallActive` after its await; `FileSearchController.resolve` early-returns for untracked correlations **before** clearing the spinner, so a stale resolution cannot clear a newer search's state. (The complementary spinner gap is F2 below.)

## 3. M2 verification detail

Constants verified: `COLLAPSE_DISCONNECT_MS = 60_000`, `IDLE_DISCONNECT_MS = 240_000`, `MAX_PENDING_WORK_EXTENSION_MS = 120_000` (`realtime.ts:48-50`); collapsed lifetime = 60 s + 120 s = 3 min; idle lifetime = 240 s + 120 s = 6 min from last activity.

1. **No indefinite loops:** the App's 1-second `COLLAPSE_RETRY_MS` loop is gone; `handleIdleTimeout` defers **once** to an absolute deadline instead of re-arming; `disconnectOrDefer` keeps the *earlier* of two deadlines (`realtime.ts:1233-1235`) so stacked idle+collapse deferrals never extend.
2. **Pending work cannot renew the deadline:** the deferred timer fires unconditionally (`endLiveSession` regardless of `hasPendingWork`), and `touchActivity` no-ops while a deferral is active.
3. **New activity re-arms where intended:** outside a deferral, `touchActivity` resets the 4-minute window; inside a deferral it deliberately does not (absolute deadline, per DECISIONS.md). But see **F1** for the reopen corner where this leaves *no* window at all.
4. **Background completions never extend:** `handleResponseDone` → `finishDeferredDisconnectIfIdle` can only *shorten* a deferral (end early when work drains), never extend it.
5. **Short work gets grace:** tested — "gives short pending work grace, then ends as soon as its response finishes".
6. **Abandoned confirmations expire:** idle fires at 4 min with the call pending → single +120 s deferral → hard teardown → `expireServerGeneration` cancels the card and the main-process pending action. Tested at both the idle and collapse deadlines.
7. **Hard deadline invalidates atomically:** `endLiveSession` → `disconnect()` clears generation + pending work synchronously, then `onSessionEnded` expires the UI in the same call stack.
8. **Late results rejected:** proven by "caps collapsed pending work at three minutes total" (late `sendToolResult` after hard teardown produces zero events) and the after-disconnect no-op test.
9. **Collapse/reopen:** `cancelCollapseDisconnect` clears the collapse timer and a *collapsed* deferral; an *idle* deferral correctly survives reopen (bounded, aggressive-but-safe). **Residual gap F1** below.
10. **No duplicate disconnect callbacks:** `disconnect()` clears all timers synchronously and nulls `activeGeneration`, so a second `endLiveSession` sees `undefined` and skips `onSessionEnded`; single-threaded timer dispatch removes the race.
11. **Disconnect/unmount clear every timer:** `disconnect()` clears idle, collapse, and deferred timers; unmount calls `disconnect()`. Tests assert `vi.getTimerCount() === 0` after teardown.
12. **Mock mode inert:** `touchActivity` and `startCollapseDisconnect` are gated on `isLiveConnected()`; tested ("never arms cost-saving teardown in mock mode").
13. **No duplicate sessions:** `connect()` begins with `disconnect()`; `connectVoice` shares one in-flight promise via `connectPromiseRef`, disconnects and expires the previous client's generation first, and the Connect button is disabled while connecting. Structurally sound; no automated test (see §6).

## 4. New findings

### F1 (Medium) — Reopen during a collapsed deferral can disarm teardown entirely

- **Affected:** `src/renderer/src/realtime.ts:301-309` (`cancelCollapseDisconnect`), `:1189-1198` (`touchActivity` no-op during deferral), `:1200-1210` (`handleIdleTimeout` consumes the idle timer into an existing deferral without re-arming).
- **Scenario (all steps reachable in normal use):** last genuine activity at t=0 leaves an abandoned confirmation card pending. User collapses at t=2:00 → collapse timer fires t=3:00 → pending work → deferral `'collapsed'`, hard deadline t=5:00. The idle timer (armed at t=0) fires at t=4:00 → `disconnectOrDefer('idle', t=6:00)` → the earlier t=5:00 deadline is kept and the idle timer is **consumed without being re-armed**. User reopens the panel at t=4:30 → `cancelCollapseDisconnect` clears the collapsed deferral — the only remaining timer. The session is now live, unmuted, with **no idle timer, no collapse timer, no deferral**, and `touchActivity` is never invoked by the reopen itself. If the user walks away in silence, the session bills silence frames indefinitely — the exact failure class M2 exists to eliminate.
- **Why the bounds tests miss it:** "cancels collapse teardown on rapid reopen" reopens *before* the collapse timer fires, so the idle timer is still armed in that test.
- **Recommended correction (one line):** at the end of `cancelCollapseDisconnect`, call `this.touchActivity()` (the deferral has just been cleared, so it re-arms a fresh 4-minute idle window; it is a no-op in mock and when disconnected). Reopening the panel is genuine user activity, so this also matches the documented policy. Add a fake-timer regression: collapse with pending work → advance past `COLLAPSE_DISCONNECT_MS` → reopen → advance `IDLE_DISCONNECT_MS + MAX_PENDING_WORK_EXTENSION_MS` with no activity → session must have ended.
- **Note:** until fixed, the DECISIONS.md sentence "the idle lifetime at most six minutes from the last activity" does not hold in this corner.

### F2 (Low) — Generation expiry mid-search can leave the panel's search spinner stuck

- **Affected:** `src/renderer/src/file-search-controller.ts:71-118` (`run`'s `stale` path skips `setSearching(false)`), `src/renderer/src/LifeLensApp.tsx:154-180` (`expireServerGeneration` never resets `isSearching`).
- **Scenario:** a model search's `begin` IPC is in flight when the generation is expired (manual reconnect, or hard-deadline teardown of a slow folder scan). The stale return deliberately skips `setSearching(false)` (correct, to protect a newer search), but nothing else resets it: `isSearching` stays `true`, so the panel's "Search files" button stays disabled showing "Searching…" until a later model-initiated search happens to clear it.
- **Recommended correction:** have `expireServerGeneration` reset `isSearching` when `expireGeneration` reported an expired entry (or track a searching-generation and clear on expiry). Cosmetic-recoverable; no cost or safety impact.

### Carried, unchanged from the original review

- **L2** (silent no-op when the channel dies while `connected` is still true) — still present, still low; the new guard swallows it via `isServerCallActive` instead of the old mode check.
- **L1** (photo cap is width-only), **L3** (capture prompt rewording), **L4** (192-token budget also covers spoken search enumeration — watch at live smoke) — unchanged.
- **L5** (collapse timer gap when a connect is in flight at fire time) — **RESOLVED:** `connectVoice` now re-arms `startCollapseDisconnect(collapsedAtRef.current)` after a connect that completes while collapsed, preserving the original absolute deadline (`LifeLensApp.tsx:234-236`).

## 5. Regression review

No regressions found in the hardening:

- **Greeting-once:** `greet: !hasConnectedOnceRef.current` for live, mock always greets; client-level opt-out test passes.
- **Lazy reconnect:** `ensureConnected` still fronts `askQuestion`, `captureScreen`, and the `analyze_photo` confirm branch; `openCompanion` keeps its check.
- **Paused / Reconnecting UI:** `onSessionEnded` sets the exact paused-notice string for `idle`/`collapsed` and error state for `error`; "Reconnecting…" renders when `isConnecting && hasConnectedOnceRef.current`.
- **Transcript → IntentTracker:** `handleUserTranscript` and the `intentUpdate` serialization are untouched; ordering test passes.
- **File-search resume:** the orchestrator round-trip is intact; the correlation ID change is renderer↔main only and opaque to main; the three end-to-end folderless-voice-search integration tests (real `RealtimeClient` + `IntentTracker` + `SearchOrchestrator` + controller) pass.
- **Telegram, capture, photo confirmation flows:** guards added, behavior preserved (mock and live paths verified by reading each callback and by the passing suite).
- **Response budgets, session.update dedupe, mock mode:** unchanged; all original tests still pass, mock never arms timers, and `completedCallIds`/`answeredCallIds` are now correctly generation-keyed so clearing them in `disconnect()` is safe.

## 6. Weak or missing tests

1. **No test for F1** — the only reopen test cancels before the collapse timer fires; the consumed-idle-timer interleaving is untested (add with the F1 fix).
2. **No duplicate-session test** — `connectPromiseRef` sharing and disconnect-before-reconnect are untested (no `LifeLensApp` tests exist at all, as before; `expireServerGeneration`, greet-once wiring, and unmount cleanup are therefore only client-level verified).
3. **Generation plumbing through the real `connect()`** is untested — generation tests install generations via private-field pokes (`activateGeneration`), so the `++nextRealtimeSessionGeneration` assignment path itself is exercised only by reading; acceptable, slightly weaker than driving `connect`.
4. Greet-once still tested by forcing the private field rather than `connect(credential, { greet: false })` (carried).
5. The claims the new tests **do** genuinely prove: old-generation sends blocked after manual reconnect and after error disconnect ✅; current-generation same-ID sends work ✅; same call ID across generations safe (client + controller) ✅; abandoned work reaches the absolute idle and collapse deadlines ✅; active work gets only bounded grace and ends when it drains ✅; reopen-before-fire clears collapse timers with zero timers left ✅; disconnect leaves `vi.getTimerCount() === 0` ✅; mock stays connected ✅; late results after hard teardown are no-ops with no `onError` ✅.

## 7. Commands run and results

| Command | Result |
|---|---|
| `git status --short` | 14 modified files + untracked `docs/plans/`, `docs/reviews/` |
| `git diff --stat` | 14 files changed, 1,314 insertions(+), 234 deletions(-) |
| `git diff --name-only` / `git diff` (full, incl. per-area reads) | Read in full; findings above derive from it |
| `git diff --check` | Clean |
| `npm.cmd run typecheck` | Passed (`tsc --noEmit`, no output) |
| `npm.cmd test` | **16 files, 213 tests, all passed** (21.2 s) |
| `npm.cmd run build` | Passed — main 98.93 kB, preload 4.90 kB, renderer built |
| Live API calls / benchmark | **Not run** — not authorized, no key; nothing live is claimed |

## 8. Live-smoke readiness

**Ready for the live Realtime smoke test**, with two riders:

1. Apply the one-line F1 fix (and its regression test) first or alongside — the smoke test's collapse/reopen step (§11 step 5 of the plan) would otherwise validate a path with a known unbounded corner.
2. During the smoke test additionally watch: L4 truncation of spoken search enumeration; `input_image.detail` and session `max_output_tokens` acceptance (plan open question 4); and the original checklist in §8 of the first review, which remains valid.

The §12 cost benchmark remains pending; STATUS.md's "Not measured" table is still honest and must only be filled from real dashboard data.

---

### Follow-up review summary

- **Verdict:** APPROVE WITH NON-BLOCKING CONCERNS
- **M1:** RESOLVED · **M2:** SUBSTANTIALLY RESOLVED (residual gap F1)
- **Blockers:** 0 · **High:** 0 · **Medium:** 1 new (F1) · **Low:** 1 new (F2) + carried L1/L2/L3/L4 (L5 resolved)
- **Verification:** typecheck ✅ · 213/213 tests ✅ · production build ✅ · `git diff --check` ✅ · live smoke and benchmark **pending** (not authorized)
- No implementation or test code was modified by this follow-up; only this review document was updated. Nothing was committed or pushed.

---

# Final F1/F2 Verification

- **Review date:** 19 July 2026 (Asia/Kolkata), final targeted pass over findings F1 and F2 only
- **Reviewer:** Claude Code (review only)
- **Reviewed state:** uncommitted working tree on `main` at HEAD `0ed3245` — 14 modified files, 1,449 insertions / 236 deletions. Delta since the follow-up review is confined to `src/renderer/src/realtime.ts` (the one-line F1 fix), `src/renderer/src/file-search-controller.ts` (request-identity rework), their two test files, and the two docs. `LifeLensApp.tsx` and all main-process files are byte-identical to the previously approved state.

## Verdict: APPROVE

### F1 — RESOLVED

- `cancelCollapseDisconnect()` (`realtime.ts:301-310`) now ends with `this.touchActivity()`. `touchActivity` (`realtime.ts:1190-1199`) is unchanged: it clears any existing idle timer before arming, so reopen produces **exactly one** idle timer; it no-ops unless `isLiveConnected()` (so disconnected and mock sessions arm nothing); and it no-ops while a deferral is active, so a surviving *idle* deferral remains absolute and unrenewable. The `'collapsed'` deferral is cleared first, so the fix arms a fresh bounded window (4 min + at most 120 s) exactly in the previously leaking state. Session generation is untouched — `cancelCollapseDisconnect` manipulates only timers.
- The exact leak sequence is now regression-tested: **"re-arms bounded idle teardown when reopening after collapsed deferral consumed the old idle timer"** (`realtime.test.ts:1011-1050`) reproduces activity at t=0 with a pending call → collapse at t=2:00 → collapse fire and `'collapsed'` deferral at t=3:00 → idle timer consumed into the deferral at t=4:00 (asserted: exactly one timer, the deferred one) → reopen → asserts `collapseTimer` and `deferredDisconnectTimer` are cleared, `idleTimer` is armed, and `vi.getTimerCount() === 1` → advances a full idle window plus the bounded extension → exactly one `onSessionEnded('idle')`, disconnected, zero timers, and no further callback on further time advance.
- Companion tests: **"re-arms exactly one idle timer after an immediate collapse and reopen"** (no duplicate timers or callbacks on the fast path); **"does not arm an idle timer when reopening after collapse already disconnected"** (reopen after teardown stays timer-free and fires no second callback); the mock-mode test now additionally exercises `startCollapseDisconnect` + `cancelCollapseDisconnect` and asserts zero timers and a still-connected mock session.
- The absolute pending-work deadline machinery is unchanged (`disconnectOrDefer` still keeps the earlier deadline; the deferred timer still fires unconditionally), and DECISIONS.md now documents "reopening counts as activity" with the idle deferral remaining absolute — code and docs agree; the "six minutes from last activity" bound now holds in the previously broken corner.

### F2 — RESOLVED

- `FileSearchController` now owns loading state through a **monotonic request identity**: `nextRequestId` increments per `run()`, `activeSearchingRequestId` records the owner, and `finishSearching(requestId)` (`file-search-controller.ts:207-213`) clears `isSearching` **only** when the finishing request is still the active owner — an older request (stale `begin` return, late resolution, or expiry of an old generation) can never clear a newer server or local search's spinner.
- `expireGeneration` (`:173-192`) collects the request IDs of the expired generation's tracked correlations, removes them, and calls `finishSearching` for each — so expiry clears the spinner exactly when the expired request owns it (the stuck-spinner bug), and no-ops when a newer search is active.
- Late stale sends remain double-barriered: the `isTracked` identity check in `run`/`resolve` drops untracked completions before `completeCall`, and the client's `isServerCallActive` generation guard is the final barrier — verified unchanged.
- New tests prove all three requested properties: **"clears only the expired in-flight generation search loading state"** (spinner true → expiry flips it false exactly once → the late `begin` resolution produces no further `setSearching` calls, no applied results, and no completions) and **"does not let an older stale request clear a newer local search"** (expired old request resolving late leaves the newer local search's `true` state intact; the newer search then completes and clears its own state). The existing same-ID-across-generations isolation test still passes.
- One pre-existing quirk, unchanged in kind from before the rework and non-blocking: a resolution arriving **without** a correlation ID (user-originated resumed search) attributes itself to the currently active request and can clear a newer search's spinner a moment early — identical to the old unconditional `setSearching(false)` in `resolve`, cosmetic only, self-correcting.

### Newly introduced issues

None found. The F1 fix is a single guarded call with no generation, budget, or protocol impact; the F2 rework is confined to the controller's internal bookkeeping and its callback timing.

### Regression spot-checks (all unchanged)

- Session generation handling: `nextRealtimeSessionGeneration` module-level and increment-only (`realtime.ts:59, 256`); all `isServerCallActive` guards intact.
- Lifecycle constants: 60 s / 4 min / 120 s unchanged (`realtime.ts:48-50`); 3-min collapsed and 6-min idle bounds re-proven by the passing fake-timer suite.
- Model and transcription: default `gpt-realtime-2.1-mini` (`src/main/services/realtime.ts:4`), `gpt-4o-mini-transcribe` (`realtime.ts:51`).
- Budgets: 192/512/2048 + session 1024 unchanged (`realtime.ts:53-57`); budget tests pass.
- Images: photo `detail: 'low'` + `maxWidth: 1024`, captures `detail: 'auto'`, 150 KB transport cap — all in place.
- session.update dedupe and mock mode: tests unchanged and passing; mock now additionally proven inert against the collapse-cancel path.

### Verification results

| Command | Result |
|---|---|
| `git diff --check` | Clean |
| `npm.cmd run typecheck` | Passed (`tsc --noEmit`, no output) |
| `npm.cmd test` | **16 files, 217 tests, all passed** (28.8 s) — matches STATUS.md's claim; includes the 4 new F1/F2 regressions |
| `npm.cmd run build` | Passed — main 98.93 kB, preload 4.90 kB, renderer built |
| Live API calls / benchmark | **Not run** — not authorized; nothing live is claimed |

### Live-smoke readiness

**Ready for live Realtime smoke testing with no code riders remaining.** The F1 rider from the follow-up review is discharged. During the smoke test, still watch the carried low-severity items: L4 (192-token budget on spoken search enumeration), `input_image.detail` / session `max_output_tokens` acceptance (plan open question 4), and the §8 live checklist of the first review. The §12 cost benchmark remains pending; STATUS.md's "Not measured" table is still honest.

---

### Final verification summary

- **Verdict:** APPROVE
- **F1 resolved:** yes · **F2 resolved:** yes
- **Newly introduced issues:** none
- **Verification:** typecheck ✅ · 217/217 tests ✅ · production build ✅ · `git diff --check` ✅ · live smoke and benchmark **pending** (not authorized)
- No implementation or test code was modified by this verification; only this review document was updated. Nothing was committed or pushed.
