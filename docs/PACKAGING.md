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
      agent/                           app/, alembic/, alembic.ini, evals/sites (fixtures)
        app/data/                      pinned Public Suffix List snapshot
      ms-playwright/                   full Chromium for the worker (no headless shell)
      manifest.json                    Python/Playwright/browser/PSL identity, digests, sizes
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
4. copies **full Chromium** matching this Playwright from the local cache (or
   downloads it) with `playwright install --no-shell chromium`, refuses the
   build if a headless shell reaches the bundle, and launches the bundled
   `chrome.exe` once with nothing but the bundle on `PLAYWRIGHT_BROWSERS_PATH`;
5. fails the build if any `browser-profiles` directory is inside the bundle;
6. byte-compiles everything;
7. imports the runtime, migration, worker, demo site **and every native
   extension** with an empty environment, and refuses to finish if any `.env`
   file is present;
8. writes `manifest.json`, including measured directory sizes.

electron-builder ships `dist/agent-runtime` as `resources/agent-runtime`.

### Browser: full Chromium, not the headless shell (Milestone 8a S1)

Until S1 the bundle carried `chromium-headless-shell` only, because the worker
ran headless and nothing needed a window. Manual sign-in (M8a S2) needs a
visible one, so the bundle now carries **full Chromium instead of** the shell —
one binary, one revision, one version matrix, run headless for M7a/M7b and
headed later for sign-in.

Two things make that work, and both are load-bearing:

- `scripts/build-agent-runtime.mjs` installs with `--no-shell`, so the shell is
  not bundled alongside. Shipping both would be ~270 MB of a binary nothing
  launches, and would leave "which browser did this run use?" unanswerable.
- `managed_launch_options` passes `channel: "chromium"`. Without it,
  `launch(headless=True)` looks for `chromium_headless_shell-<revision>` and
  fails outright in a packaged build. With it, development and the packaged app
  launch the *same* binary, so a cached headless shell on a developer machine
  cannot hide a packaging break.

Measured on this machine, from the Playwright cache for revision `1243`
(Chromium 153.0.8010.12):

| Component | Before (headless shell) | After (full Chromium) |
| --- | --- | --- |
| Browser directory | 271 MB | 433 MB |
| Plus `winldd` helper | 1 MB | 1 MB |

A net change of **about +162 MB** to the browser portion of the bundle. The
installer figure is recorded in
[reviews/milestone-8-s1.md](reviews/milestone-8-s1.md) from an actual packaging
run rather than estimated here.

### Browser profile directories never enter an artifact

Persistent browser profiles live under `%LOCALAPPDATA%\Lumi\browser-profiles`,
outside the install directory, the repository and `dist/`, so a profile in the
bundle is impossible by construction. The build asserts it anyway — it fails if
any `browser-profiles` directory is found under `dist/agent-runtime` — and
`browser-profiles/` is in `.gitignore` so a developer who relocates the base
into the checkout cannot commit one. `tests/test_profile_paths.py` asserts the
resolved base is under `%LOCALAPPDATA%` and under none of the build trees.

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
| `headless` | `true` (default). Full Chromium is bundled since M8a S1, so headed mode is supported by the bundle; no M8a S1 code path uses it yet. |

### The egress broker and the bundled browser

Milestone 8 S0 routes every managed-browser connection through a loopback
egress broker inside the worker process (see
[SECURITY.md](SECURITY.md)). It adds **no packaging change**: the broker is
Python, it binds an ephemeral loopback port at worker start, and it needs no
new binary, no certificate store entry and no configuration file. At S0 it was
verified against the binary the bundle shipped then — `chrome-headless-shell.exe`
from `chromium_headless_shell-<revision>`.

**That binary changed in M8a S1**, and the broker was re-verified against the new
one: `chrome.exe` from `chromium-<revision>`, launched with
`channel: "chromium"` in both headless and headed mode.

**The M8a packaging prerequisite is resolved.** That prerequisite is now done: the bundle carries full
Chromium and the headless shell has been dropped, with the measured size change
recorded above. The broker was re-verified against `chrome.exe` launched with
`channel: "chromium"`, which is the binary the bundle now ships.

Every persistent browser profile is launched through the same broker, with the
same `proxy`, `--proxy-bypass-list=<-loopback>` and QUIC-disabling arguments.
There is no unbrokered persistent context and no loopback exemption.

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
