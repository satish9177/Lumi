# Packaging Lumi with its Python runtime

The installed app starts the real agent runtime with no repository checkout,
no developer shell and no manually started server. PostgreSQL remains an
external prerequisite (a local Docker container or any PostgreSQL 16+).

## Layout

```text
Lumi/                                  (install directory)
  Lumi.exe
  resources/
    app.asar                           Electron main, preload, renderer
    agent-runtime/                     built by scripts/build-agent-runtime.mjs
      python/                          relocatable CPython 3.12 (uv standalone build)
        Lib/site-packages/             locked production dependencies only
      agent/                           app/, alembic/, alembic.ini, evals/sites (demo clinic)
      ms-playwright/                   Chromium headless shell for the worker
      manifest.json                    Python/Playwright versions, lock digest, code digest
      requirements.lock.txt
```

## Build

```powershell
npm.cmd run package:dir     # release\<version>\win-unpacked
npm.cmd run package         # plus the NSIS installer
```

`build:agent-runtime` (run by both):

1. copies the uv-managed CPython that `services/agent/.venv` is based on;
2. installs exactly `uv export --frozen --no-dev` into it;
3. copies the runtime code and the demo clinic site, excluding `.env*`,
   tests and caches;
4. copies the matching Chromium headless shell from the local Playwright cache
   (or downloads it);
5. byte-compiles everything;
6. imports the runtime, migration, worker, demo site **and every native
   extension** with an empty environment, and refuses to finish if any `.env`
   file is present;
7. writes `manifest.json`.

electron-builder ships `dist/agent-runtime` as `resources/agent-runtime`.
Measured sizes: Python + dependencies ~230 MB, Chromium ~275 MB; the NSIS
installer is ~263 MB.

## Runtime start in a packaged build

Electron main (`startPackagedAgentRuntime`):

1. reads `%APPDATA%\Lumi\agent-runtime.json` (see below). If it is missing,
   writes `agent-runtime.example.json` next to it and shows
   *"Agent runtime needs setup"* in the task panel; the rest of Lumi works;
2. in demo mode, starts the bundled clinic site on a free loopback port with
   its catalogue moved to the coming Saturday and a parent watchdog;
3. runs `python -m app.migrate` once (Alembic to head);
4. starts `python -m app.server` with the M4 supervisor (fresh bearer token,
   readiness pipe, Windows kill-on-close job, restart budget), passing the
   database URL, the site origin and `PLAYWRIGHT_BROWSERS_PATH` for the
   bundled Chromium;
5. the runtime starts its own browser worker as in M4.

All children are started from fixed paths with fixed arguments, no shell,
`PYTHONDONTWRITEBYTECODE=1`, and a constructed environment.

### `agent-runtime.json`

```json
{
  "databaseUrl": "postgresql+asyncpg://lumi:<password>@127.0.0.1:5432/lumi_agent",
  "clinicSite": "demo",
  "headless": true
}
```

| Field | Values |
| --- | --- |
| `databaseUrl` | required; must be `postgresql+asyncpg://` |
| `clinicSite` | `demo` (default), `none`, or `http://127.0.0.1:<port>` |
| `headless` | `true` (default). Headed mode needs full Chromium, which is not bundled. |

Provider keys for voice and text models are read from the user's environment
by Electron main, exactly as in development (see [PROVIDERS.md](PROVIDERS.md)).
Nothing secret is bundled.

## Tested

`services/agent/tests/test_packaged_app.py` (`LUMI_PACKAGED_E2E=1`) launches
the packaged `Lumi.exe` with an isolated profile and a scrubbed environment
(no `LUMI_*`, provider, database or Playwright variables), from outside the
checkout:

- unconfigured: the app opens, shows the setup notice and writes the example;
- configured: migrations run, runtime + worker + Chromium + demo site start,
  a typed request is answered by the deterministic rules, the cheapest slot is
  prepared, the trusted click books exactly once;
- graceful quit stops every child process; a restart restores the same task;
- the installed runtime writes no `.pyc` files.

It passed against `release\0.1.0\win-unpacked` and against a copy installed by
the NSIS installer with `/S /D=<dir>` (see the executable note below). The
test installation was then removed with the silent uninstaller.

## Windows Application Control

The development machine runs **Smart App Control in enforcement mode**. Two
real effects were observed and are the reason distribution builds must be
Authenticode-signed:

1. `greenlet 3.5.6`'s unsigned `_greenlet.pyd` was blocked when installed as a
   fresh file (the dev virtualenv worked only because uv hard-links from its
   cache). Older, widely distributed releases load, so the lock constrains
   `greenlet<3.5` (`[tool.uv] constraint-dependencies`), and the bundle smoke
   test imports every native extension so a block fails the build.
2. Rebuilt, unsigned `Lumi.exe` files were blocked after a few rebuilds
   while earlier builds had been allowed. electron-builder stamps version
   resources and the asar integrity hash into the executable, so every rebuild
   is a new, reputation-less binary. The installer itself ran and installed
   correctly, but installed that same blocked `Lumi.exe`. For the final
   packaged acceptance runs, `Lumi.exe` in both the unpacked build and the
   NSIS-installed copy was replaced with the stock Electron 38 executable from
   `node_modules/electron/dist` (identical runtime; only the embedded icon and
   version strings differ; embedded asar integrity validation is disabled), so
   everything else — `app.asar`, the bundled Python runtime, Chromium, the
   installer's layout — was exactly what the build produced.

Do not disable Smart App Control or WDAC to run an unsigned build; sign it.

## Known limitations

- PostgreSQL is not bundled.
- Unsigned build (see above).
- The bundled demo clinic site is the deterministic test fixture; no real
  clinic website is supported.
- Only Windows x64 is packaged.
