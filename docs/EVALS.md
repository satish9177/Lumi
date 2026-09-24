# Evaluations

## Deterministic suite (CI-safe)

```powershell
npm.cmd run eval          # TypeScript + Python cases
npm.cmd run eval -- --ts  # TypeScript cases only (no database or browser needed)
```

`scripts/run-evals.mjs` runs the cases below through Vitest and pytest,
prints a scorecard, and writes `dist/evals/eval-report.json`. Every case is a
named automated test; nothing calls a paid model. Providers are scripted, the
realtime voice is scripted, and the browser drives the local fixture site.
The Python cases need `TEST_DATABASE_URL` and Playwright's Chromium.

| Category | Cases |
| --- | --- |
| Voice / task | ordinary conversation creates no task; English request becomes a dated durable search; ambiguous date is asked back; refinement withdraws an excluded booking; compound request prepares exactly one booking; compound request stops before approval; attempted voice approval only surfaces the card; replayed turn runs nothing; replayed request after restart creates no second task; voice turn ids unique across reconnects; late transcript binds the call to its own turn |
| Browser | normal booking exactly once; price changed after approval; slot disappeared; hostile page text; worker crash after submit; response lost after the external effect; second workflow leaves no ledger footprint; date window applied to observed slots |
| Recovery | hard kill → `OUTCOME_UNKNOWN`; read-only reconciliation; recovered action never executes again; no duplicate submission under concurrency |
| Provider | malformed/hostile output refused; timeout failover and cooldown; fallback keeps one task and one action; all providers down → deterministic fallback; refusal not shopped; realtime session drop reported; errors carry no provider body |
| Memory / context | stale preference overridden; website fact overrides memory; long history within budget; task state summarised; tampered memory ignored |
| Security | renderer never sees provider credentials; Gemini token stays in main; model cannot request browser/HTTP/shell; closed tool vocabulary; voice cannot approve; hostile page text cannot become an instruction; hostile profile text never spoken; preload exposes only fixed channels |
| Inspection (M7a) | refused destinations never reach the runtime; read once only after approval; "yes/approve/go ahead" only points at the card; duplicate request after restart; lost response answered later without reopening; rating not rank; missing rating not guessed; ungrounded/action-bearing output refused; hostile model refused; approved providers only; URL policy; hostile page exfiltration closed; forbidden redirects; document epoch; URL-only input; bound observation + consumed approval; stale revision + concurrency; kill before dispatch; worker crash; runtime crash; runtime re-verifies grounding; stale observation |

| Research (M7b) | step vocabulary refuses selectors, scripts and addresses; a search query cannot carry private data out; public hosts allowed but no forbidden destination; ungrounded answers refused; no step before the trusted grant; single-use step authorizations; expired and revoked scopes authorise nothing; the timeline says authorized, never approved; duplicated planner requests execute nothing twice; budgets stop the task; one session serves the task; link refs followed without naming an address; stale refs refused without a request; the worker applies its own policy; hostile pages open no channel; mutation requests never leave the browser; forbidden redirects never requested; tabs bounded; multi-hop research ends grounded; a decoy cannot ground the answer; unsupported operations refused; the budget stops an endless corridor; a worker restart stales every ref; a runtime crash is unknown, never repeated; a planner cannot name an address, a selector or a script; a persuaded model cannot record an invented figure; nothing searched before the trusted click; a refused step is re-observed, never repeated; an unconfirmed step is reported, never repeated; links reach the desktop as refs and hosts |
| Composer (M7b) | a research request becomes a permission card, not a realtime turn; research searches only after the trusted Allow click; an unconfigured research request is still owned by the agent; an ordinary question still reaches realtime |
| Orchestration (M11) | task completion across all three composed capabilities (public_research, project_start, project_status); correct capability selection (dispatch always calls that capability's own real entry point); a real-but-uncomposed or unknown capability id refuses rather than executing or crashing; malformed/extra-field planner output refused; planner calls counted and paused at the bound; each task-backed capability pauses for its own approval, never auto-approved; a stale revision refused; a repeated capability choice pauses as a loop, not a second step; a project_start refused by the capability's own effect lock surfaces that same refusal, never bypassed; resume re-checks durable state rather than re-choosing; an ambiguous run outcome pauses with its own honest reason (`outcome_unknown`), never mislabeled `approval_required`; the step budget pauses rather than silently widening; Stop clears the pause reason so a paused orchestration can still be stopped; the planner is shown only trusted controller facts, never a raw private value |

Results: **43/43** on 2026-09-17 (Milestones 1-7a), **118/118** on
2026-09-18 (with the Milestone 7b cases above), and **138/138** on
2026-09-24 (with the Milestone 11 orchestration cases above). Milestone 11's
composed capability set is `public_research`/`project_status`/`project_start`
only (see `docs/reviews/milestone-11-s5.md`); the remaining catalog stays
real-but-uncomposed, so breadth is proven by correct refusal of every
uncomposed/unknown/malformed choice rather than by executing domains
(documents, accounts, desktop) that are not wired to the orchestrator yet.

## Electron acceptance (real desktop app)

```powershell
npm.cmd run build
cd services\agent
$env:LUMI_ELECTRON_E2E = '1'
# One file per run. Each case launches Electron, Chromium, the runtime and a
# fixture site, and the 60-second launch deadline is easy to miss when four
# files share one machine -- that shows up as "the Lumi renderer never loaded".
uv run pytest tests\test_electron_acceptance.py
uv run pytest tests\test_voice_acceptance.py
uv run pytest tests\test_m6_acceptance.py
uv run pytest tests\test_inspection_acceptance.py
```

| File | Scenario |
| --- | --- |
| `test_electron_acceptance.py` | M4: panel booking, changed price, missing slot, lost response + hard kill |
| `test_voice_acceptance.py` | M5: voice booking, refinement, voice-initiated lost response |
| `test_m6_acceptance.py` | M6 A (compound voice, OpenAI protocol), A (compound voice over the Gemini Live relay), B (provider failover + restart), clinic info + preferences + ambiguous date, C (compound voice + lost response + hard kill) |
| `test_inspection_acceptance.py` | M7a: approved page read + grounded answer, typed "approve" is not approval, missing rating, hostile page and hostile model, restart then answer from the saved page, hard kill mid-read → unknown, never retried |

Milestone 7b research is covered by runtime-level acceptance rather than a
separate Electron E2E file: `services/agent/tests/test_research_ledger.py`
drives real processes (runtime, isolated worker, Chromium, fixture site and a
canary) through the whole pipeline, and `src/renderer/src/composer-routing-research.test.ts`
covers renderer → preload → main → runtime ownership.

`LUMI_FIXED_NOW` pins the calendar to 16 September 2026 in these tests
(unpackaged builds only) so "Saturday" keeps meaning the fixture's Saturday.

## Packaged application

```powershell
npm.cmd run package:dir
cd services\agent
$env:LUMI_PACKAGED_E2E = '1'   # optional: $env:LUMI_PACKAGED_EXE = '<installed Lumi.exe>'
uv run pytest tests\test_packaged_app.py
```

See [PACKAGING.md](PACKAGING.md).

## Live providers (manual, opt-in, paid)

Run from the repository root in PowerShell. Google uses Application Default
Credentials (`gcloud auth application-default login`, project from
`LUMI_VERTEX_PROJECT`, `GOOGLE_CLOUD_PROJECT` or the ADC quota project); keys
stay in the shell and are never written to the repository.

```powershell
# 1. Generate the synthetic utterances (Vertex AI gemini-2.5-flash-tts via ADC)
npm.cmd run live:audio            # writes dist\live-audio\*.wav + manifest.json

# 2. Run the live checks
$env:LUMI_LIVE_PROVIDER_TESTS = '1'
$env:LUMI_VERTEX_ENABLED = '1'
$env:LUMI_LIVE_AUDIO_DIR = "$PWD\dist\live-audio"
$env:OPENAI_API_KEY = '...'       # optional; OpenAI text is reported skipped without it
$env:DEEPSEEK_API_KEY = '...'     # optional; DeepSeek text is reported skipped without it
npx vitest run src/main/voice/providers.live.test.ts
```

The structured report goes to `live-provider-report.json` (git-ignored;
override with `LUMI_LIVE_REPORT`).

### Audio fixtures

`scripts/live/make-test-audio.ts` speaks the fixed phrases in
`src/main/voice/live-audio-fixtures.ts` and writes each one as a canonical PCM
WAV in the exact format Lumi streams to Gemini Live: **16 000 Hz, mono,
signed 16-bit little-endian PCM** (the TTS model's 24 kHz output is
resampled). Only the PCM samples are sent (`realtimeInput.audio`,
`audio/pcm;rate=16000`); the WAV header never leaves the test.

| File | Language | Phrase | Used by |
| --- | --- | --- | --- |
| `en_compound` | en-IN | Find me a dermatologist on Saturday evening under 1000 rupees and prepare the cheapest one. | main scenario (tool call, transcript) |
| `en_stop` | en-IN | Stop, stop. Wait a moment, please stop talking. | barge-in over the spoken answer |
| `en_in` | en-IN | Find me a dermatologist Saturday evening under 1000 rupees. | multilingual (asserted) |
| `te_mixed` | te-IN | Saturday evening dermatologist appointment choodu, 1000 rupees lopala. | multilingual (recorded) |
| `te_pure` | te-IN | శనివారం సాయంత్రం చర్మ వైద్యుడి అపాయింట్‌మెంట్ 1000 రూపాయల లోపల చూడు. | multilingual (recorded) |

Options: `npm.cmd run live:audio -- --out <dir>`, `LUMI_LIVE_TTS_MODEL`,
`LUMI_LIVE_TTS_VOICE` (default `Kore`), `LUMI_VERTEX_LOCATION`. TTS output
is not bit-identical between runs; `manifest.json` records model, voice,
duration and SHA-256 per file.

Any other synthetic source works if it produces `<name>.wav` in that format
or headerless `<name>.pcm` (16 kHz mono s16le), e.g.
`ffmpeg -i in.wav -ar 16000 -ac 1 -c:a pcm_s16le en_in.wav`. Never use
recordings of real people.

When live tests are enabled, the Gemini Live suite checks the directory and
all five fixtures before connecting and fails with an explanation if
`LUMI_LIVE_AUDIO_DIR` is unset, the directory or a file is missing, or a WAV
is malformed or not 16 kHz mono PCM16. It never skips.

Only the English utterances are asserted; whether Telugu and code-switched
speech produced the same criteria is recorded in the report
(`gemini_multilingual_equivalent`). Results and limitations:
[PROVIDERS.md](PROVIDERS.md).
