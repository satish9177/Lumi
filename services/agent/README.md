# Lumi agent runtime

A Python sidecar that stores Lumi agent tasks and their event history durably in
PostgreSQL. This is Milestone 1: tasks can be created, read and cancelled, and
they survive the runtime restarting. It does no planning, runs no LLM or browser,
and does not execute anything yet. See [`docs/AGENT-RUNTIME.md`](../../docs/AGENT-RUNTIME.md).

## Setup (Windows, PowerShell)

Prerequisites: Docker Desktop, [uv](https://docs.astral.sh/uv/).

```powershell
# 1. Development PostgreSQL (loopback only)
Copy-Item infra\.env.example infra\.env          # then set LUMI_POSTGRES_PASSWORD
docker compose -f infra\docker-compose.yml up -d --wait

# 2. Runtime configuration
cd services\agent
Copy-Item .env.example .env                       # use the same password
uv sync

# 3. Schema (the runtime never creates tables itself)
uv run alembic upgrade head

# 4. Run
uv run uvicorn --factory app.main:create_app --host 127.0.0.1 --port 8765
```

The runtime refuses to start if `DATABASE_URL` is missing or invalid, the
database is unreachable, or the schema is not at the Alembic head.

`infra/postgres/init` creates `lumi_agent_test` only when the data volume is
first initialized. For an existing volume, create it manually.

## Checks

```powershell
uv run pytest        # needs TEST_DATABASE_URL (a database ending in _test)
uv run mypy          # strict
```

## API

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/health` | `200 {"status":"ok","database":"ok"}`, or `503` when the database is unreachable |
| `POST` | `/tasks` | `201` task in `CREATED`, revision 1, with a `task.created` event |
| `GET` | `/tasks/{id}` | `200` task, `404 task_not_found`, `422` malformed UUID |
| `GET` | `/tasks/{id}/events?after_sequence=0&limit=100` | `200` events ordered by `sequence` |
| `POST` | `/tasks/{id}/cancel` | see cancellation rules |

`POST /tasks` body: `{"request": {"type": "appointment_search", "text": "..."}}`.
`type` is required (`^[a-z][a-z0-9_.]*$`); other JSON fields are stored verbatim;
the request is capped at 64 KB.

### Cancellation rules

| Current status | Result |
| --- | --- |
| `CREATED`, `PLANNING`, `READY`, `EXECUTING`, `VERIFYING`, `PAUSED` | `200`, becomes `CANCELLED`, revision + 1, one `task.cancelled` event |
| `CANCELLED` | `200`, unchanged: same revision, no new event (idempotent) |
| `SUCCEEDED`, `FAILED` | `409 task_not_cancellable`, nothing written |

An optional body `{"expected_revision": N}` makes the cancel conditional: a
mismatch returns `409 stale_revision` with `current_revision`.
