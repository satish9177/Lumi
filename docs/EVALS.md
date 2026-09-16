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

Result on 2026-09-17: **43/43 passed**.

## Electron acceptance (real desktop app)

```powershell
npm.cmd run build
cd services\agent
$env:LUMI_ELECTRON_E2E = '1'
uv run pytest tests\test_electron_acceptance.py tests\test_voice_acceptance.py tests\test_m6_acceptance.py
```

| File | Scenario |
| --- | --- |
| `test_electron_acceptance.py` | M4: panel booking, changed price, missing slot, lost response + hard kill |
| `test_voice_acceptance.py` | M5: voice booking, refinement, voice-initiated lost response |
| `test_m6_acceptance.py` | M6 A (compound voice, OpenAI protocol), A (compound voice over the Gemini Live relay), B (provider failover + restart), clinic info + preferences + ambiguous date, C (compound voice + lost response + hard kill) |

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

```powershell
$env:LUMI_LIVE_PROVIDER_TESTS = '1'
$env:LUMI_VERTEX_ENABLED = '1'            # and/or OPENAI_API_KEY / DEEPSEEK_API_KEY
$env:LUMI_LIVE_AUDIO_DIR = '<dir>'        # en_compound.wav, en_stop.wav, en_in.pcm, te_mixed.pcm, te_pure.pcm
npx vitest run src/main/voice/providers.live.test.ts
```

Providers without credentials are reported as skipped. Use synthetic
utterances only; the report records the synthetic transcripts and structured
results, never real speech. Results and limitations: [PROVIDERS.md](PROVIDERS.md).
