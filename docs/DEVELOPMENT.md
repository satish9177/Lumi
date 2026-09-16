# Local development

## Prerequisites

Windows 10/11 x64, Node.js 20.19+ or 22.12+, npm, Docker Desktop (for
PostgreSQL), [uv](https://docs.astral.sh/uv/).

## First run

```powershell
npm.cmd ci

# PostgreSQL on loopback
Copy-Item infra\.env.example infra\.env          # set LUMI_POSTGRES_PASSWORD
docker compose -f infra\docker-compose.yml up -d --wait

# Python runtime
cd services\agent
Copy-Item .env.example .env                       # DATABASE_URL and TEST_DATABASE_URL
uv sync
uv run playwright install chromium
uv run alembic upgrade head
cd ..\..

# Demo clinic site (a separate shell; the runtime's browser worker visits it)
cd services\agent; uv run python -m evals.sites.appointments.server --port 8801

npm.cmd run dev
```

In development, Electron main starts the runtime from `services/agent/.venv`
and points its worker at `LUMI_APPOINTMENT_FIXTURE_ORIGIN`
(default `http://127.0.0.1:8801`).

## Configuration (root `.env` / `.env.local`, main process only)

See [.env.example](../.env.example) and [PROVIDERS.md](PROVIDERS.md). Without
any model key, voice runs in its mock mode and typed requests use the
deterministic English rules.

Development-only switches (ignored by packaged builds):

| Variable | Effect |
| --- | --- |
| `LUMI_REALTIME_SCRIPTED=1` / `gemini` | Scripted realtime voice over the OpenAI protocol / over the Gemini relay |
| `LUMI_SCRIPTED_MODELS` | Scripted text providers, e.g. `deepseek:timeout,gemini:rules` |
| `LUMI_FIXED_NOW`, `LUMI_TIMEZONE` | Pin the calendar clock / override the time zone (the time zone override also applies in packaged builds) |
| `LUMI_DIAGNOSTICS=1` | Echo redacted diagnostics to the console |
| `LUMI_USER_DATA_DIR` | Isolated profile |
| `LUMI_AGENT_DATABASE_URL` | Override the runtime database |
| `LUMI_BROWSER_HEADED=1` | Show the worker's Chromium |

## Checks

```powershell
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
cd services\agent; uv run pytest; uv run mypy
npm.cmd run eval
```

Two Vitest files (`real-inference.test.ts`, `tokenizer-pack.test.ts`) need the
local CLIP model pack under `%APPDATA%`, and one accessibility assertion is
sensitive to CRLF checkouts; these three are known environment baselines.
