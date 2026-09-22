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
npm.cmd run package:release # signed release plus post-build verification
```

`package:dir` and `package` are development packaging paths. They may produce
unsigned output and remain unchanged. An unsigned package is **not a release
artifact** and **must not be used for real-account testing**.

`package:release` is the only release path. Before it runs either build step it
requires these trusted environment values:

- `LUMI_SIGNING_CERT_SHA1`: the complete 40-hex certificate-store thumbprint;
  whitespace is removed and the value is uppercased before an exact match.
- `LUMI_SIGNING_TIMESTAMP_URL`: an explicit `http:` or `https:` RFC 3161
  endpoint without credentials, a query string or a fragment. There is
  deliberately no default.
- `LUMI_SIGNING_EXPECTED_SUBJECT`: optional exact signer subject. If present,
  post-build verification requires an exact match.

The release path runs the same `build` and `build:agent-runtime` steps as
`package`, then invokes electron-builder 26 with release-only configuration:
`forceCodeSigning: true`, certificate-store selection by the exact SHA-1
thumbprint, `signingHashAlgorithms: ['sha256']`, and the explicit RFC 3161
server. It also forces `directories.output` to `release/<version>` and passes
`publish: 'never'`, so builder cannot publish before independent verification.
The inherited package build configuration is rejected before work if it
contains publishing, lifecycle/artifact hooks, custom signing hooks, file-based
certificate credentials, Azure signing, an NSIS script, or disabled executable
signing. The SHA-1 thumbprint identifies the certificate; it is not a SHA-1
file-signature digest. No certificate file, private key, password, token or
vendor credential is read by Lumi's wrapper. The expected production model is
a Windows certificate-store certificate whose private key operations remain in
the provider's HSM through its CNG/KSP integration (for example, an eSigner
store integration).

After packaging, the release command verifies exactly these distributed files:

- `release/<version>/win-unpacked/Lumi.exe`;
- the single expected `release/<version>/Lumi Setup <version>.exe` installer.

The release directory may contain no other top-level `.exe`, both expected
artifacts must be newer than the recorded start of this release build, and
electron-builder must report the canonical installer path in its returned
artifact list. Stale files are never deleted automatically; they fail the gate.

For each file, fixed PowerShell calls `Get-AuthenticodeSignature` and requires
`Status == Valid`, a signer certificate, and the exact configured thumbprint
(and exact subject when configured). A fixed, argument-array SignTool command
then runs `verify /pa /all /tw /v`; command failure, warnings, an untrusted
chain, a missing timestamp, missing/non-parseable output, or a missing tool all
fail the release. A signer with `Subject == Issuer` is also rejected. The
PowerShell timestamp certificate is treated only as a hint and is not proof;
the gate establishes only that a trusted timestamp is present and verified by
SignTool `/tw`. It does not independently prove the timestamp protocol or
server. The forced RFC 3161 signing configuration plus artifact freshness ties
that verification to this build. Before building, the same PowerShell check
also requires the vendored SignTool itself to be `Valid` and Microsoft-signed.

The result is written outside the application at
`release/<version>/signing-report/release-signing-report.json`. It contains
artifact paths and SHA-256 hashes, signature status, public signer identity and
validity dates, SignTool `/tw` timestamp result, whether freshness was checked,
verification time and pass/fail. It
does not contain environment dumps or credentials. A failure still produces a
report when artifact discovery and report output are available, then exits
non-zero.

`build:agent-runtime` (run by all three packaging commands):

1. copies the uv-managed CPython that `services/agent/.venv` is based on;
2. installs exactly `uv export --frozen --no-dev` into it;
3. copies the runtime code and the demo clinic site, excluding `.env*`,
   tests and caches;
4. copies **full Chromium** matching this Playwright from the local cache (or
   downloads it) with `playwright install --no-shell chromium`, refuses the
   build if a headless shell reaches the bundle, and launches the bundled
   `chrome.exe` **twice** — once headless, once headed — with nothing but the
   bundle on `PLAYWRIGHT_BROWSERS_PATH`;
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

### Headed launch, proven from the bundle alone (Milestone 8a S2)

S1 proved only that the bundled `chrome.exe` launches *headless* from the
bundle — nothing in M7a/M7b/S1 needed a window. Manual sign-in (S2) is the
capability that motivated shipping full Chromium in the first place, so the
build now also launches the same bundled binary **headed**, through the same
`channel: "chromium"`, from the same bundle-only `PLAYWRIGHT_BROWSERS_PATH`
and scrubbed environment as the headless check, opens a page, and closes it.
A packaging break in headed mode (a missing dependency the headless path
does not exercise, for instance) now fails the build instead of surfacing
only the first time a person tries to sign in.

Run on this machine (`npm run package:dir`):

```text
[agent-runtime] verifying the bundled Chromium launches headless from the bundle alone
[agent-runtime] bundled Chromium 153.0.8010.12 (chromium-1243)
[agent-runtime] verifying the bundled Chromium also launches headed from the bundle alone
[agent-runtime] bundled Chromium headed launch verified (153.0.8010.12)
```

`manifest.json` now carries `browser.headedVerified: true` alongside the
version each build actually launched — the same binary, in both modes, from
the bundle alone, with no developer browser cache to fall back on.

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

### Windows UI Automation backend (Milestone 9 S1)

The desktop worker runs on the **existing bundled Python**: no second executable and no helper binary. The lock adds `pywinauto` (0.6.9), `comtypes` (1.4.17) and `pywin32` (312) with the marker `sys_platform == 'win32'`, so other platforms do not install them.

`build-agent-runtime.mjs` (step 4c, before byte-compiling) proves the backend from the bundle alone with a scrubbed environment: it imports the backend **first** (it selects COM's multithreaded apartment before anything initialises COM on the thread; importing `pythoncom` first makes it a single-threaded apartment and the backend's own initialisation then fails), then the native `pywin32` extensions explicitly (`pythoncom`, `pywintypes`, `win32api`, `win32gui`, `win32process`, `win32event`), imports `_ctypes`, constructs `PywinautoBackend`, reads the process's own integrity level, and asserts that comtypes generated `UIAutomationClient.py` into the bundled `comtypes/gen`. It also fails the build if a native binary of Lumi's own (`.exe`, `.dll`, `.pyd`, ...) appears under `agent/`, if the desktop test fixture or harness is present, or if migration `0012` or `app/desktop/worker.py` is missing. The manifest (`version: 3`) records the backend and the three dependency versions.

**comtypes wrapper generation.** comtypes generates Python wrappers for `UIAutomationCore` on first import and checks them against the type library's modification time. The wrappers generated at build time are bundled (and byte-compiled). On a machine whose `UIAutomationCore.dll` has a different timestamp comtypes regenerates them once (about 20 s cold on the developer machine), writing into `comtypes/gen` if it is writable and otherwise into `%APPDATA%\Python\Python312\comtypes_cache` (resolved through a Windows API, so it does not depend on the worker's scrubbed environment). The runtime's startup timeout for the worker is 90 s to cover it. This is a first-use latency and a small per-user cache write, not a correctness issue; it has not been measured on a second machine (see the S1 review's residual risks).

The backend asks for the type library with `comtypes.client.GetModule("UIAutomationCore.dll")` rather than importing `comtypes.gen.UIAutomationClient`, which exists only after generation and so worked on a developer machine but failed on a clean bundle (found by this build check). `pywin32` also ships `Pythonwin.exe`, `pythonservice.exe` and script launchers that Lumi never runs; they are third-party and are recorded here as candidates for pruning.

`pywin32`, `comtypes` and `pywinauto` are third-party; their `.pyd`/`.dll` files fall under the existing third-party classification of the release-signing policy. **No change to the Authenticode gate was needed**, and this section does not alter the release status: the production certificate is still absent and the real-account release gate is still BLOCKED.

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
| `headless` | `true` (default); governs the runtime worker's default context only. Full Chromium is bundled since M8a S1 and its headed launch is verified by every build since S2 (above); manual sign-in (S2) opens its own takeover window headed regardless of this setting, since that is the one operation a human must be able to see. |

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

### Third-party executable signing policy

electron-builder 26.15.3 would normally call `signIf` for every `.exe` copied
through `extraResources`, and it also walks executable files in
`resources/app.asar.unpacked`. With a real Lumi certificate, the default would
therefore replace vendor signatures on bundled executables. The release-only
configuration prevents that with the supported `win.signExts` suffix policy:
positive Lumi-owned suffixes are listed before a catch-all `!.exe` exclusion.
This relies on the v26 `shouldSignFile` precedence inspected in
`node_modules/app-builder-lib/out/winPackager.js` and is pinned by tests.

The packaged executable-code classes are:

1. **Lumi-owned, must sign:** `Lumi.exe`, the exact versioned NSIS installer,
   and electron-builder's temporary `__uninstaller.exe` before it is embedded.
2. **Third-party, retain vendor provenance:** CPython (`python.exe`,
   `pythonw.exe`), Playwright Chromium (`chrome.exe` and helpers), ffmpeg, Node,
   NSIS `elevate.exe`, native `.pyd`/`.dll` dependencies, and other executable
   runtime content. These are not selected for Lumi signing; existing vendor
   signatures are neither removed nor replaced.
3. **Actually passed to Lumi signing by the release configuration:** only paths
   ending in `Lumi.exe`, the exact `Lumi Setup <version>.exe`, or
   `__uninstaller.exe`. The final verifier independently checks `Lumi.exe` and
   the installer. The embedded uninstaller is not separately extracted and
   verified by this gate.

The filename policy is intentionally narrow. A future Lumi-owned executable
requires an explicit reviewed suffix and verification decision. Conversely, a
third-party file colliding with one of the allowlisted suffixes would be a
release-review blocker; the current runtime has no such collision.

### What this gate does not establish

`Valid`, `/pa`, a trusted timestamp verified by SignTool `/tw`, and a
non-self-issued leaf are necessary technical
checks, not proof that the intended public production account was purchased or
approved. An enterprise/private root could be trusted locally, and this script
does not independently prove public-CA issuance. The real-account gate also
requires a release manager to confirm a publicly trusted code-signing CA and
the intended Lumi publisher identity. The JSON therefore never claims that the
real-account gate is satisfied; it says only `release signature verified`.

No production certificate has been acquired or exercised in this repository.
Until that human identity/CA check and a real signed-artifact run occur,
real-account release remains **BLOCKED**.

## Known limitations

- PostgreSQL is not bundled.
- Development packages may be unsigned; no production certificate has been
  acquired, so the real signed-release path has not yet been exercised.
- The bundled demo clinic site is the deterministic test fixture; no real
  clinic website is supported.
- Only Windows x64 is packaged.

**M8a S3 packaging note.** Authenticated reading adds no new binary: it uses the bundled full Chromium and the S0 broker. Profiles live under the user data directory (`LUMI_BROWSER_PROFILE_ROOT`), not in the bundle. No new packaged-build run was performed for S3.

### Desktop bounded semantic actions (Milestone 9 S4)

No new native dependency and no new executable: `SetValue`/`Select`/`Invoke` are three more calls through
the same UIA backend S1-S3 already bundle. `build-agent-runtime.mjs` additionally fails the build if
migration `0015` (`desktop_dispatches`' widened operations/identity columns, the widened grant kind,
`desktop_action_plans`) or any of the four new planning-service files (`app/services/desktop_planning.py`,
`app/domain/desktop_planning.py`, `app/repositories/desktop_planning.py`,
`app/api/desktop_planning_schemas.py`) is missing from the runtime bundle. Packaged output was verified
(`package:dir`): the bundled UIA backend import check (S1's, unchanged) still passes from the bundle alone,
and no new fixture, secret or unexpected executable appears anywhere in the packaged tree. No signing
change.

### Desktop focus, scroll and registered launch (Milestone 9 S3)

No new native dependency and **no new executable**: starting a registered application is `subprocess.Popen` of a program the user registered, not a bundled helper. `build-agent-runtime.mjs` additionally fails the build if migration `0014` (`desktop_dispatches`), `app/desktop/effects.py`, `effects_win32.py`, `registry.py`, `app/services/desktop_actions.py` or `app/domain/desktop_actions.py` is missing from the runtime bundle. The user's registered-application list (`LUMI_DESKTOP_REGISTERED_APPS`) is configuration and is never bundled; no fixture or test file ships. No signing change.

### Desktop disclosure (Milestone 9 S2)

No new native dependency, no new executable and no signing change. `build-agent-runtime.mjs` additionally fails the build if migration `0013` (`desktop_disclosures`, `desktop_answers` and the widened grant kind) or `app/services/desktop_disclosure.py` is missing from the runtime bundle.
