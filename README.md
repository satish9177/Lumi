# Lumi

**A Windows desktop agent that talks with you, looks things up on a website, prepares a booking — and only books after you click, exactly once, even if everything crashes.**

Lumi started as an OpenAI Build Week 2026 screen companion ([demo video](https://www.youtube.com/watch?v=znW03ult8w8), [Build Week notes](docs/BUILD-WEEK.md)) and grew, over six milestones, into a voice/text agent with a durable task runtime. Supported platform: **Windows 10/11 x64**.

```text
Voice / Screen / Typed request
            ↓
Realtime provider (OpenAI Realtime or Gemini Live)   ← transport only
            ↓
Lumi intent + task controller                        ← strict typed plans, dates, ≤ 4 steps
            ↓
Durable task runtime (Python + PostgreSQL)           ← tasks, timeline, action ledger
            ↓
Policy / trusted approval / memory / model router
            ↓
Reviewed browser tools (isolated worker, Chromium)   ← semantic operations only
            ↓
Verification + reconciliation                        ← receipts, OUTCOME_UNKNOWN, read-only lookup
```

**Models suggest; the durable controller owns execution.** No model output — spoken, typed or read from a webpage — can approve, execute, pick a URL or click a selector. The only approval path is a click on the trusted card.

## What it does

- **Say or type a compound request.** *"Find me a dermatologist Saturday evening under ₹1000 and prepare the cheapest available option."* Lumi creates one durable task, resolves "Saturday" to a calendar date in your time zone, searches a reviewed clinic site read-only, picks the cheapest recorded result by rule, prepares one booking from what the browser sees now, and stops.
- **"Book it" is not approval.** It focuses the booking card. Pressing **Approve and book** approves exactly the reviewed proposal (digest- and revision-bound) and submits it once.
- **Crash-safe.** If the process dies after the website accepted the booking, the action becomes `OUTCOME_UNKNOWN`; "check it" runs a read-only lookup. It never books twice.
- **English, Telugu and code-switched speech** map onto the same structured criteria (validated live with Gemini Live — see [PROVIDERS.md](docs/PROVIDERS.md)).
- **Two voice providers, three text providers.** OpenAI Realtime or Gemini Live on Vertex AI; OpenAI, Gemini (Vertex) and DeepSeek for text, routed by task class with failover that never repeats an action.
- **Second workflow.** Read-only clinic information (hours, fee, languages, address, walk-ins) on the same durable controller, with no approval because nothing changes.
- **Memory with provenance.** "Remember I prefer evenings" fills gaps in later requests; what you say now wins; the website decides prices.
- **Packaged.** The installer ships its own Python runtime, dependencies and Chromium; the app migrates the database and starts everything itself.
- The original companion features remain: one-shot screen capture with a separately confirmed GPT-5.6 review, scam-warning checks, approved-folder file search, and on-device photo/OCR/people search ([details](docs/LOCAL-PHOTO-SEARCH.md)).

## Quick start (development)

```powershell
npm.cmd ci
Copy-Item infra\.env.example infra\.env; docker compose -f infra\docker-compose.yml up -d --wait
cd services\agent; Copy-Item .env.example .env; uv sync; uv run playwright install chromium; uv run alembic upgrade head
uv run python -m evals.sites.appointments.server --port 8801   # demo clinic site (own shell)
cd ..\..; npm.cmd run dev
```

Open Lumi → **Book appointment** → type *"Find a dermatologist Saturday evening under 1000 and prepare the cheapest one"*. With no model keys, typed requests use deterministic English rules and voice runs in mock mode. Full guide: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Providers

| Purpose | Configure (Electron main only) |
| --- | --- |
| OpenAI Realtime voice, OpenAI text, GPT-5.6 screen review | `OPENAI_API_KEY` |
| Gemini Live voice and Gemini text on Vertex AI | `LUMI_VERTEX_ENABLED=1` + Google ADC (`gcloud auth application-default login` or a service account); `LUMI_VOICE_PROVIDER=gemini` for voice |
| DeepSeek text | `DEEPSEEK_API_KEY` |
| Routing overrides | `LUMI_MODEL_ROUTES` (JSON) |

Keys never reach the renderer, the Python runtime or the browser worker. Gemini Live audio is relayed through Electron main so the Google token stays there. Details, routing table and live results: [docs/PROVIDERS.md](docs/PROVIDERS.md).

## Packaged app

```powershell
npm.cmd run package        # release\0.1.0\Lumi Setup 0.1.0.exe
```

Create `%APPDATA%\Lumi\agent-runtime.json` with your PostgreSQL URL and `"clinicSite": "demo"`. The app runs migrations, starts the bundled runtime, worker, Chromium and demo clinic site, and stops them on quit. See [docs/PACKAGING.md](docs/PACKAGING.md), including why distribution builds must be code-signed.

## Verification

```powershell
npm.cmd run typecheck; npm.cmd test; npm.cmd run build
cd services\agent; uv run pytest; uv run mypy
npm.cmd run eval                                   # 43 deterministic eval cases
$env:LUMI_ELECTRON_E2E='1'; uv run pytest tests\test_electron_acceptance.py tests\test_voice_acceptance.py tests\test_m6_acceptance.py
$env:LUMI_PACKAGED_E2E='1'; uv run pytest tests\test_packaged_app.py
```

What each suite proves and how to run the opt-in live provider checks: [docs/EVALS.md](docs/EVALS.md). Latest results: [docs/reviews/milestone-6.md](docs/reviews/milestone-6.md).

## Documentation

| | |
| --- | --- |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Processes, request lifecycle, module map |
| [SECURITY.md](docs/SECURITY.md) | Trust boundaries, credentials, model output, memory |
| [AGENT-RUNTIME.md](docs/AGENT-RUNTIME.md) | Durable tasks, ledger, approvals, recovery, browser execution (M1–M6) |
| [PROVIDERS.md](docs/PROVIDERS.md) | Voice/text providers, routing, context budgets, live validation |
| [PACKAGING.md](docs/PACKAGING.md) | Bundled runtime, configuration, Application Control |
| [EVALS.md](docs/EVALS.md) | Deterministic, desktop, packaged and live evaluations |
| [DEVELOPMENT.md](docs/DEVELOPMENT.md) | Local setup and development switches |
| [services/agent/README.md](services/agent/README.md) | Runtime API and configuration |

## Privacy and safety

- Screen capture is user initiated and never continuous; the GPT-5.6 review needs its own confirmation.
- Every state-changing action needs an explicit confirmation in the trusted UI; main re-validates every IPC payload.
- Browser work is limited to reviewed operations on allowlisted sites; page text is data.
- File search stays inside folders you approved; photo indexing, OCR and people matching run locally ([DECISIONS.md](docs/DECISIONS.md)).
- Diagnostics record ids, codes and counts — never prompts, transcripts, form values or keys.

## Known limitations

- The only supported website is the deterministic demo clinic site. Nothing is claimed about real clinic websites, logins, payments, OTP or CAPTCHA.
- PostgreSQL is not bundled. The packaged build is unsigned.
- OpenAI Realtime and DeepSeek were not live-validated in Milestone 6 (no keys on the validation machine); Gemini Live and Gemini text were.
- Gemini Live cannot change instructions mid-session or take per-response instructions; the model's free speech is steered, not enforced — the card and timeline are the authority.
- The deterministic fallback understands English only.
- One browser worker at a time; one active task in the panel.
- Windows only.

## Status

Complete for the current project scope (Milestones 1–6). See [docs/STATUS.md](docs/STATUS.md) and [docs/reviews/milestone-6.md](docs/reviews/milestone-6.md).

## License

MIT — see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
