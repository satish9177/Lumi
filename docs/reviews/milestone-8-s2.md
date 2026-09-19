# Milestone 8a S2 — manual login and human takeover

Implementation report. Branch `lumi-agent-v2`.

> **S2 allows a user to manually authenticate in a Lumi-owned browser while
> Lumi is suspended. S2 still does not let an AI model read the authenticated
> page.**

| | |
|---|---|
| Starting SHA | `6961e7d0e176b9af2b5944e9c8fbefd6135dfd74` (M8a S1 complete, including the documentation correction) |
| Architecture review | `docs/plans/milestone-8.md` §§8, 22b, 27 (S2 exit criteria) |
| Final SHA (implementation) | `48e9484` (`feat(agent): add manual login takeover (M8a S2)`, 52 files) |
| Final SHA (documentation) | `11b535d0002bab321dbef6603eaa94a8aa5bb9fe`, then this correction commit recording that SHA |
| Migration | `0006` → `0007` |

**Commit structure.** Two commits: `48e9484` (`feat(agent): add manual login
takeover (M8a S2)`, 52 files — migration, domain/service/repository/browser
code, API routes/schemas, Electron main/preload/renderer/IPC, the synthetic
fixture, and all new/modified tests) and this documentation commit (`docs:
record milestone 8a S2 manual login`, this report plus `docs/SECURITY.md` and
`docs/PACKAGING.md`).

---

## 1. What was built

```text
a headed, human-driven interval on an existing S1 profile
        ↓
the human signs in themselves, inside Chromium's own UI
        ↓
one deterministic check runs when the human says "I'm signed in"
        ↓
site-scope AND no remaining credential surface  =>  AUTHENTICATED
otherwise                                       =>  stays NEEDS_LOGIN, with a reason
        ↓
Lumi's own automation (planner, provider, observation, dispatch) is
structurally unreachable for the life of the takeover
```

Four fixed operations exist and no others, all scoped to a takeover on an
existing profile — there is still no operation that reads a page, fills a
field or names a URL:

```text
openLoginWindow(profileId, expectedRevision)                POST /browser-profiles/{id}/takeover
getLoginTakeover(profileId, attemptId)                       GET  /browser-profiles/{id}/takeover/{attemptId}
confirmSignedIn(profileId, attemptId, expectedRevision)      POST /browser-profiles/{id}/takeover/{attemptId}/confirm
cancelLogin(profileId, attemptId, expectedRevision)           POST /browser-profiles/{id}/takeover/{attemptId}/cancel
```

`listBrowserProfiles()` (from S1) is how the trusted card learns a profile
exists and its status; it is unchanged.

---

## 2. Migration `0007`

One new table, `login_attempts` (`alembic/versions/0007_create_login_attempts.py`),
additive only — `browser_profiles`, `task_grants`, `step_authorizations`,
`action_attempts` and `browser_dispatches` keep their S1 shape exactly.

```text
id, profile_id, runtime_generation, worker_generation,
profile_revision, status, started_at, expires_at,
completed_at, cancelled_at, created_at, updated_at
```

Constraints, all enforced by the database, not just the service:

* `status IN ('OPEN','UNCONFIRMED','COMPLETED','CANCELLED','EXPIRED','INTERRUPTED')`
* `profile_revision >= 1`
* `expires_at > started_at`
* `(completed_at IS NOT NULL) = (status = 'COMPLETED')`
* `(cancelled_at IS NOT NULL) = (status = 'CANCELLED')`
* a **partial unique index** on `profile_id` where `status IN ('OPEN','UNCONFIRMED')`
  — the database itself refuses a second concurrently-open attempt on one
  profile, not just the service's own pre-check.
* `worker_generation`/`runtime_generation` are `RESTRICT` foreign keys into
  the S0/S1 generation tables, so an attempt can never outlive the identity
  that opened it becoming unreferenceable.

Deliberately absent, by design, not oversight: no column for page text, a
URL, a credential signal or a raw account identity. `app/db/tables.py`'s
`login_attempts` table matches the migration exactly, asserted by
`test_migrations_match_table_definitions` (S0/S1's own convention, unchanged).

---

## 3. The takeover state machine

`app/domain/login_takeover.py` defines the closed vocabulary; `app/services/login_takeover.py`
(`LoginTakeoverService`) is the only writer.

```text
OPEN          the takeover is active; the human owns the browser
UNCONFIRMED   the trusted "I'm signed in" click landed; the deterministic
              post-takeover check is running or has not yet completed
COMPLETED     the check ran to completion (whatever it found)
CANCELLED     the trusted "Cancel" click ended it — no authentication claim
EXPIRED       the hard timeout elapsed — no authentication claim
INTERRUPTED   the owning process died before the attempt could complete —
              no authentication claim
```

There is deliberately **no `FAILED` state**. A click on "I'm signed in"
followed by a completed check that still finds a login surface is
`COMPLETED` — the attempt did what an attempt does (bound an interval,
produced one fresh answer). Whether that answer was "authenticated" lives
entirely on `browser_profiles.status`, never on the attempt row. This is the
same separation S1 drew between a lease and an authentication claim, applied
one layer up.

`start_takeover` → `LoginAttemptRepository.create()` (row born `OPEN`) →
either `begin_confirm()` (CAS `OPEN → UNCONFIRMED`) then `complete()` (CAS
`UNCONFIRMED → COMPLETED`, recording the verdict), or `cancel()` (CAS
`OPEN/UNCONFIRMED → CANCELLED`), or the sweep's `expire_due()` (CAS
`OPEN/UNCONFIRMED → EXPIRED`), or reconciliation's `mark_interrupted()` (any
stale-generation open row → `INTERRUPTED`). Every transition is a
compare-and-swap `UPDATE`, matching S0/S1's own idiom throughout the
codebase — there is no code path that reads-then-writes an attempt's status.

---

## 4. Headed Chromium lifecycle

`ProfileSessionStore.start_takeover` / `.confirm_takeover`
(`app/browser/profile_session.py`):

1. The profile is opened **headed** (`open_profile(..., headed=True)`) —
   *before* the attempt row is created, so a refusal to open never leaves an
   orphaned `login_attempts` row.
2. `TakeoverNetworkGuard` (§7) is installed on the context.
3. The one tracked tab navigates to `https://{profile.site}/`. **This scheme
   is hardcoded** — the only override is a `scheme` parameter that exists
   solely for this milestone's own test suite and is never exposed on the
   wire (see §21's residual limitation on why the Electron acceptance test
   cannot exercise this path against a fixture).
4. A navigation failure closes the profile immediately and creates no
   attempt row at all (`NAVIGATION_FAILED` → `login_navigation_failed`,
   before any row exists).
5. The human drives the visible window: typing credentials, an OTP, clicking
   through a CAPTCHA-shaped challenge, following an SSO redirect — all
   inside Chromium's own UI. Lumi's own code does not read any of it.
6. "I'm signed in" → `confirm_takeover`: the deterministic check (§10–§12)
   runs against whichever page is still tracked, then the context (main tab
   **and every popup**) is closed unconditionally, whatever the outcome.
7. "Cancel" ends the takeover the same way, with **zero** check run and
   **zero** authentication claim of any kind — the context still closes, but
   nothing about the page was ever inspected.

Only one profile may have an open takeover at a time (the partial unique
index in §2); a second "Sign in manually" click on the same profile is
refused (`login_attempt_already_open`) before anything is touched.

---

## 5. API and boundary changes

### Runtime HTTP (internal)

```text
POST /browser-profiles/{id}/takeover                      → open_login_window
GET  /browser-profiles/{id}/takeover/{attemptId}          → get_login_takeover
POST /browser-profiles/{id}/takeover/{attemptId}/confirm  → confirm_signed_in
POST /browser-profiles/{id}/takeover/{attemptId}/cancel   → cancel_login
```

No route accepts a URL, a credential, a cookie, page content, a browser
argument or an executable path. `site` never appears in a request body — the
profile's own bound, immutable site (fixed at S1 creation) is what the
runtime hands the worker; nothing a caller names here.

### Runtime → worker

`app/browser/protocol.py`: `TakeoverStartRequest`/`Response`,
`TakeoverConfirmRequest`/`Response`, plus `ProfileSessionRequest.headed`. All
`extra="forbid"` Pydantic models, matching every existing worker message.

### Electron main, preload, renderer

Five fixed IPC channels, each named for exactly what it does, none generic:

```text
lifelens:agent:list-browser-profiles
lifelens:agent:open-login-window
lifelens:agent:confirm-signed-in
lifelens:agent:cancel-login
lifelens:agent:get-login-takeover
```

`BrowserProfileController` (`src/main/services/browser-profile-controller.ts`)
is a class of its own, not a method group on `AgentTaskController` — see §9
for why that separation is what makes voice exclusion structural. Every
mutation takes an id (or two) plus the revision the trusted card last showed;
`listBrowserProfiles` takes no argument; there is still deliberately no
`createBrowserProfile` reachable from the renderer.

### Refusal codes

`login_attempt_already_open`, `login_attempt_not_found`,
`login_attempt_not_open`, `login_attempt_expired`, `login_navigation_failed`,
`login_profile_not_open`, plus the pre-existing `profile_*` codes reused
unchanged. Each has app-authored wording in
`describeProfileRefusal` (`src/main/services/browser-profile-wire.ts`) —
website text never reaches this function.

---

## 6. Network mode during a takeover ("TAKEOVER", vs. `AGENT_READ`)

Neither name is a formal enum value anywhere in code — both are this
codebase's own documentation shorthand (`app/browser/takeover_guard.py`'s own
docstring names them this way), parallel to how S0/S1 never named a
`BrokerMode.TAKEOVER`. What actually exists is a second Playwright-level
request-interception class, `TakeoverNetworkGuard`, installed in place of
`PublicNetworkGuard` for the life of a takeover.

`PublicNetworkGuard` (M7a/M7b, "`AGENT_READ`") fetches every request itself,
allows only `GET`/`HEAD`, allows only a handful of resource types, and never
lets Chromium follow a redirect natively.

`TakeoverNetworkGuard` ("`TAKEOVER`") is deliberately wider: it does **not**
fetch requests itself and does **not** restrict method or resource type —
Chromium handles the human's traffic exactly as an ordinary browser would,
because a login form is a `POST`, a CAPTCHA is an image, and an SSO chain is
a browser-followed redirect through an identity provider Lumi cannot
enumerate in advance. What it still refuses, at the request-interception
layer, before a request leaves the browser process: any scheme other than
`http`, `https`, `data:` or `blob:`.

**`file:` is a documented, reactive exception.** Chromium's local-file loader
never reaches the layer Playwright's `route()` intercepts, confirmed
empirically by `test_a_file_url_is_unavailable`. There is no preventive
Playwright API for this. The guard is instead notified the instant the top
frame commits (`framenavigated`) and immediately retreats a forbidden-scheme
navigation to `about:blank`. The forbidden page's content is genuinely
rendered for a short, real interval first — stated as a residual limitation
in §22, not hidden.

Everything else stays exactly what S0 already enforces and this guard does
not re-implement: the destination (still the S0 broker, still
`--proxy-bypass-list=<-loopback>`, still requiring a public address or an
exactly-configured test origin — TAKEOVER does not touch the broker's policy
at all), downloads (`accept_downloads=False`, unchanged from S1), service
workers and permissions (blocked/empty, unchanged from S1). Popups are
tracked but never adopted as a durable resource; the whole context — main
tab and every popup — closes when the takeover ends, whatever the outcome.

Measured behavioural note: full Chromium (S1's packaging choice) issues one
extra favicon request per navigation the old headless shell did not; this
does not change anything TAKEOVER mode refuses.

---

## 7. Broker preservation

The S0 egress broker is **untouched** by S2. `TakeoverNetworkGuard` is a
Playwright-side request filter that sits *in front of* the same brokered
persistent context S1 already launches — same `channel: "chromium"`, same
`--proxy-server`, same `--proxy-bypass-list=<-loopback>`, same
QUIC-disabling arguments, same connection-time destination pinning inside the
broker itself. There is no unbrokered persistent context anywhere, headed or
headless, and no loopback exemption was added.
`tests/test_takeover_network.py` (12 tests) proves this directly: a private
IPv4 destination, a cloud-metadata destination, and a dead broker are all
still refused exactly as S0 already refused them, now reached from a headed
takeover tab instead of a headless research tab.

---

## 8. Agent-suspension mechanism, proven structurally

`LoginTakeoverService`'s constructor takes exactly
`(self, engine, runtime_generation, browser, profiles, ttl_seconds)` — no
planner, no provider, no model router of any kind to inject, asserted by
`test_the_login_takeover_service_takes_no_planner_or_provider_dependency`
(reads the constructor's own `inspect.signature`, not a comment).

Five modules that participate in a takeover —
`app/services/login_takeover.py`, `app/repositories/login_attempts.py`,
`app/domain/login_takeover.py`, `app/browser/takeover_guard.py`,
`app/browser/credential_signals.py` — are scanned, with docstrings and
comments stripped first (`test_no_credential_extraction.py`'s own idiom, so a
module may explain the rule without tripping it), for the literal tokens
`ModelRouter`, `Planner`, `planner`, `provider`, `openai`, `gemini`,
`deepseek`, `anthropic`, `google_auth`. None appear
(`test_the_takeover_modules_import_nothing_model_shaped`, parametrized over
all five). This is "unreachable," not "unused": there is nothing in these
modules' source that could name a model or a provider even accidentally.

That structural proof is backed by a real one:
`test_a_takeover_creates_no_task_action_dispatch_or_observation`
(`tests/test_login_takeover_service_browser.py`) drives a real password+OTP
login through a real headed Chromium and a real Postgres, and asserts
`tasks`, `actions`, `browser_dispatches`, `page_observations` and
`research_observations` row counts are **identical** before and after — not
merely that the new tables are untouched, but that nothing else in the whole
schema moved either.

---

## 9. Screen-capture exclusion

`src/main/services/capture.ts`: a module-level flag, `takeoverActive`, that
every capture entry point checks and refuses against
(`CaptureRefusedError`, code `capture_refused_takeover_active`) before doing
any work. There is no reliable way to identify *which* `desktopCapturer`
source is the takeover window — its title changes with every page the human
navigates to — so the design refuses **every** capture while **any**
takeover is open, rather than trying to exclude one source from a list.

`BrowserProfileController.track()` sets this flag from the same result it
uses to update the trusted card (`openLoginWindow`, `confirmSignedIn`,
`cancelLogin`, and the panel's own `getLoginTakeover` poll), so the flag can
be stale by at most one poll interval — and only in the fail-safe direction:
it can refuse a capture no takeover still needs protected, never grant one
while a takeover is genuinely open. See §22 for the one restart-time gap this
leaves.

---

## 10. Voice restriction

Structural, not conventional. `VoiceTaskBackend` (`voice-task-controller.ts`)
is `Pick<AgentTaskController, ...>` naming only booking-task methods.
`BrowserProfileController` is not a member of `AgentTaskController` at
all — it is its own class, constructed separately and given to the IPC layer
directly (`agent-ipc.ts`), never to the voice controller. There is no `Pick`
surface that could name `openLoginWindow`/`confirmSignedIn`/`cancelLogin`
even by future accident; a realtime tool call cannot reach these methods
because the type they would have to be picked from does not contain them.

---

## 11. Credential-surface detector

`app/browser/credential_signals.py`, run against exactly one tracked page,
after a takeover ends, before any observation of any kind could be built
from it. Every check is a bounded DOM **count**
(`locator(...).count()`), never a text read — the code answers "how many
elements match," never "what do they say."

```text
PASSWORD_FIELD       input[type="password"], [autocomplete="current-password"]
NEW_PASSWORD_FIELD    [autocomplete="new-password"]
ONE_TIME_CODE_FIELD   [autocomplete="one-time-code"], input[name*="otp" i]
WEBAUTHN_HINT         [autocomplete="webauthn"]
```

**Over-inclusive on purpose.** A false positive costs one extra "finish
signing in yourself" message. A false negative would mean an authenticated
transition on a page that still needs credentials, which is the failure this
module exists to prevent.

**Not a proof, stated here and in `docs/SECURITY.md`:** a login form built
without `type=password` — a custom canvas, an image-based keypad, a
neutral-named cross-origin iframe — is not detected. Passkey/WebAuthn prompts
are native browser UI the DOM cannot see at all. See §22.

---

## 12. Post-takeover site-scope check

`_site_scope(page, site)` (`app/browser/profile_session.py`), a closed
three-value enum, never a host string:

```text
IN_PROFILE_SITE     the tracked page's registrable domain matches the
                    profile's bound site
OTHER_PUBLIC_SITE    a page exists, but its registrable domain is some
                    other public site (including any IP-literal page,
                    since an IP has no registrable domain)
NO_PAGE             the tracked page/tab no longer exists — the human
                    closed it
```

The comparison goes through the same pinned Public Suffix List S1 already
uses for `canonical_site()` — one comparison function, one PSL snapshot, no
second implementation to drift from the first. `_site_scope` tracks exactly
one page (the tab `start_takeover` opened); a login that finishes in a
**popup** rather than the original tab is a known limitation, §22b.

---

## 13. Authentication determination

`confirm_takeover` computes both signals and combines them with one rule,
in `app/services/login_takeover.py`:

```text
scope != CHECKED or scope == NO_PAGE        → login_no_page
scope != IN_PROFILE_SITE                     → login_not_on_profile_site
credential_surface is non-empty              → login_credential_surface_present
otherwise                                     → AUTHENTICATED, fingerprint recorded
```

`AUTHENTICATED` requires **both** conditions — in-profile-site **and** no
remaining credential surface — never either alone. Clicking "I'm signed in"
is not itself an authentication claim; it only starts this check, and only
this check's outcome ever writes `browser_profiles.status`. The `UNCONFIRMED`
intermediate status exists exactly so a process death between the click and
the check's completion (§16) can never be read back as a successful sign-in.

---

## 14. Account-fingerprint foundation

`account_fingerprint(page)` reads exactly one bounded attribute —
`[data-lumi-account-id]`'s `data-lumi-account-id` value — hashes it with
SHA-256 (`hash_identity`, `app/domain/login_takeover.py`) in the same
function that read it, and returns only the hash or `None`. The raw string
is never returned, logged or stored; `None` means "no stable signal found"
and every caller must treat that as `unknown`, never a fabricated identity.

This is a **fixture convention, not a real-site standard.** `ACCOUNT_IDENTITY_SELECTOR`
is a Lumi-authored attribute the synthetic fixture's `/account` page carries;
a real site has no such attribute, and designing what a real site's
equivalent signal should be is explicitly deferred to S3 (§22d). S2 builds
and proves the mechanism — read one bounded value, hash it immediately,
never persist the raw form — against the one fixture that exercises it.

---

## 15. Timeout behaviour

`DEFAULT_TAKEOVER_TTL_SECONDS = 900` (15 minutes), configurable
`60`–`3,600` s (`LUMI_LOGIN_ATTEMPT_TTL_SECONDS`). A background watchdog
(`_sweep_expired_takeovers`, `app/main.py`) runs for the life of the runtime
process, sweeping every `LUMI_LOGIN_ATTEMPT_SWEEP_INTERVAL_SECONDS` (default
15 s, independent of the timeout itself) and calling
`LoginTakeoverService.sweep_expired()`, which CAS-transitions every
past-`expires_at` `OPEN`/`UNCONFIRMED` row to `EXPIRED` and closes its headed
window. A watchdog exception is logged and swallowed — it must never take
the runtime down — and `test_the_expiry_sweep_closes_the_window_and_settles_the_record`
proves the window closes and the profile is left at `NEEDS_LOGIN`, never
`AUTHENTICATED`, on a hard timeout.

---

## 16. Crash and restart behaviour

`LoginTakeoverService.reconcile_interrupted()` runs once at startup (`app/main.py`,
before the sweep watchdog starts): every `OPEN`/`UNCONFIRMED` row bound to a
**stale** `worker_generation` — i.e., from a process generation that is not
the one now starting — is marked `INTERRUPTED`. A row from the **current**
generation is left alone (`test_reconciliation_never_touches_an_attempt_from_the_current_generation`),
which matters because reconciliation runs once per process start and must
not undo a takeover the same process just opened.

The failure mode this closes: a worker crash between the "I'm signed in"
click (`UNCONFIRMED`) and the check completing never leaves the profile
readable as `AUTHENTICATED` — it is either still `UNCONFIRMED` (recovered to
`INTERRUPTED` on the next start) or the exception path in `confirm_takeover`
already left it `UNCONFIRMED` with a best-effort profile close. A genuinely
completed login is only ever recovered by the human repeating the takeover
and the deterministic check running again — never by re-reading stale state
as if it were a fresh answer. `test_an_attempt_from_a_stale_generation_is_marked_interrupted`
proves the transition; `test_a_worker_failure_during_confirm_leaves_the_attempt_unconfirmed`
proves the exception path.

---

## 17. Synthetic fixture design

`evals/sites/account_fixture/app.py`, extended from S1's session-only fixture
to a full synthetic login surface. Constants are fixture-only and never real:
`FIXTURE_USERNAME`, `FIXTURE_PASSWORD` (`S2-Fixture-Passw0rd-Only`),
`FIXTURE_OTP` (`482913`), `SSO_TOKEN` (`demo-sso-token`).

```text
GET/POST /login              username + password
GET/POST /login/otp          one-time code
GET/POST /login/challenge     a CAPTCHA-shaped surface, click-through only
GET      /login/sso           starts a redirect to a second origin (the IdP)
GET      /idp/authorize       the second origin, issues a token, redirects back
GET      /login/sso/callback  the first origin, completes the SSO chain
GET      /login/offsite       a redirect to a destination outside the site
GET      /switch-account      swaps which account's session cookie is active
GET      /session/expire      invalidates the session cookie server-side
GET      /logout              ends the session
GET      /account             the signed-in page; carries [data-lumi-account-id]
GET/GET  /storage/write, /storage/read   page-script storage, for the
                                          pre-existing no-credential-extraction
                                          proof, not new to S2
```

The SSO chain is a **real** cross-origin browser-followed redirect (a second
uvicorn instance as the IdP, `idp_origin` passed to `create_site`), not a
same-origin simulation — `test_sso_redirect_through_a_second_origin_ends_back_in_scope`
drives it and asserts the takeover ends back on the account origin. The
CAPTCHA-shaped `/login/challenge` route exists to prove Lumi never inspects
it and never fails to recognise the eventual signed-in state — a human
clicks through, and the check downstream sees only the final page.

No real password, OTP, token or account exists anywhere in this fixture or
in this milestone's code.

---

## 18. Exact zero-observation / zero-provider assertions

Two independent kinds of proof, deliberately not one:

1. **Structural** (§8): five takeover-participating modules scanned for
   model/provider-shaped tokens after docstrings/comments are stripped;
   `LoginTakeoverService.__init__`'s parameter set inspected directly.
2. **Real, measured** (`tests/test_login_takeover_service_browser.py`):
   * `test_a_takeover_creates_no_task_action_dispatch_or_observation` —
     `tasks`, `actions`, `browser_dispatches`, `page_observations`,
     `research_observations` row counts identical before/after a real
     password+OTP login against a real database.
   * `test_a_real_login_leaves_no_planted_secret_in_the_database` — every row
     of `login_attempts`, `browser_profiles`, `runtime_generations` and
     `browser_worker_generations` concatenated into one string and scanned:
     `FIXTURE_PASSWORD`, `FIXTURE_OTP` and the **raw** `ACCOUNT_ID` (only its
     hash may appear) are all asserted absent — and the same three strings
     are asserted absent from `repr()` of the service's own return value, so
     even a value this test already holds in memory carries none of it.

---

## 19. Packaged headed-mode result

S1 proved only that the bundle's `chrome.exe` launches **headless** from the
bundle alone. S2 is the capability that motivated shipping full Chromium
over the headless shell in the first place, so
`scripts/build-agent-runtime.mjs` now launches the same bundled binary
**headed** too — same `channel: "chromium"`, same bundle-only
`PLAYWRIGHT_BROWSERS_PATH`, same scrubbed environment as the headless check —
opens a page, and closes it. A packaging break reachable only in headed mode
now fails the build instead of surfacing the first time a person tries to
sign in.

Run on this machine (`npm run package:dir`, exit 0):

```text
[agent-runtime] verifying the bundled Chromium launches headless from the bundle alone
[agent-runtime] bundled Chromium 153.0.8010.12 (chromium-1243)
[agent-runtime] verifying the bundled Chromium also launches headed from the bundle alone
[agent-runtime] bundled Chromium headed launch verified (153.0.8010.12)
```

`manifest.json` now carries `browser.headedVerified: true` alongside the
version each build actually launched. No orphaned `chromium-1243` process
was left behind after two consecutive `npm run package:dir` runs (checked via
`Get-CimInstance Win32_Process`). Full details and the updated build-step
list: [PACKAGING.md](../PACKAGING.md).

---

## 20. Exact test results

All Python figures are from `services/agent` with a real PostgreSQL.

| Suite | Result |
|---|---|
| `tests/test_login_takeover.py` | **23 passed** (state machine, scripted fake worker) |
| `tests/test_login_takeover_browser.py` (browser) | **9 passed** (real headed Chromium, no database) |
| `tests/test_login_takeover_service_browser.py` (browser) | **2 passed** (real headed Chromium **and** real database) |
| `tests/test_takeover_network.py` (browser) | **12 passed** |
| `tests/test_login_takeover_acceptance.py` (browser, `LUMI_ELECTRON_E2E=1`) | **1 passed** (see §22a for its honestly-reduced scope) |
| **New S2 tests, total** | **47 passed** |
| `uv run mypy` | **Success: no issues found in 155 source files** |
| Full `uv run pytest -m "not browser"` | **797 passed, 1 failed** — the failure is the pre-existing `.env`-dependent baseline in §21, not an S2 regression |
| Full `uv run pytest -m browser` | **208 passed, 17 skipped** in 20 m 3 s. The 17 skipped are the `LUMI_ELECTRON_E2E`/`LUMI_PACKAGED_E2E`-gated Electron/packaged acceptance tests, not run in this particular invocation (each was run and passed separately — see the two rows below). One `PytestUnhandledThreadExceptionWarning` (`httpx.ReadError: [WinError 10054]`) was emitted during teardown of an unrelated pre-existing browser test; it did not fail the run and is consistent with the same Windows keep-alive teardown-timing hazard §22 already documents as mitigated, not root-caused. |
| `npm.cmd run typecheck` | **clean** |
| `npm.cmd run build` | **clean** (main, preload, renderer) |
| `npm.cmd test` (vitest) | **1966 passed, 1 failed, 22 skipped** — the failure is the pre-existing CSS-whitespace baseline in §21 |
| `npm.cmd run eval` | **118/118 eval cases passed** |
| `npm.cmd run package:dir` | **succeeded**, twice, headed launch verified both times |
| Electron acceptance (`LUMI_ELECTRON_E2E=1`) | **1 passed** — `test_login_takeover_acceptance.py`, plus the pre-existing S0/S1 Electron acceptance tests still passing |

Every new S2 browser/process test cleans up through
`tests/broker_teardown.py`'s `quiesce_broker`/`bounded` helpers (see §22e);
`pytest-timeout` (thread method) is a permanent safety net under all of them.

---

## 21. Known baseline failures

Four tests fail on this machine before and after S2, for reasons that have
nothing to do with this slice — three of them the exact same ones S1's own
review already recorded and re-checked here, one newly checked this slice:

| Test | Cause | Checked how |
|---|---|---|
| `src/renderer/src/accessibility.test.tsx` — *keeps the level legible without colour* | The same stale CSS-whitespace assertion against the scam card's stylesheet S1 already recorded. `components.css` is untouched by S2. | Re-ran in isolation; fails at the same line; no S2 diff touches this file. |
| `src/main/vision/real-inference.test.ts` | `ENOENT … vision-models/clip-vit-base-patch32-q8/vocab.json` — the local vision model pack is not installed on this machine. | Re-ran in isolation; the error is a missing local asset, unrelated to browser profiles. |
| `src/main/vision/tokenizer-pack.test.ts` | The same missing model pack (`model_load_failed`). | As above. |
| `services/agent/tests/test_booking_preparation.py::test_booking_routes_without_a_worker_answer_503` | This developer's `services/agent/.env` sets `LUMI_PUBLIC_INSPECTION_HOSTS=github.com,example.com`, so `_worker_source` always builds a `ManagedBrowserWorker` and `browser_worker_not_configured` is unreachable; the route answers `browser_worker_unavailable` instead. | Overriding `LUMI_PUBLIC_INSPECTION_HOSTS=""` for one run makes it pass; restoring the `.env` value makes it fail again, deterministically, in isolation. `_worker_source`'s condition gains one more `and not settings.auth_test_origins` term in S2, but that term defaults empty and is not what trips this — `public_inspection_hosts` alone already does. |

`src/main/services/screen-reasoning.test.ts` failed once during this slice's
own vitest run and passed on an immediate re-run in isolation — a flake, not
a new baseline failure, consistent with how S1's review already treated an
unrelated flake it found (§21 there).

---

## 22. Residual limitations

1. **The Electron acceptance test cannot exercise credential-fill →
   confirm → `AUTHENTICATED` end to end.** `start_takeover` hardcodes
   `https://{profile.site}/` on the real wire path — its `scheme` override
   is test-infrastructure-only and the route the real UI calls never
   supplies one. No fixture in this repository can be both broker-dialable
   (the S0 broker's `configured_origins` needs a literal loopback origin)
   and TLS-answerable (a real profile's `site` can never be an IP literal,
   since `canonical_site()` refuses one before a row exists, and an
   IP-literal test fixture has no registrable domain for the PSL to match
   against) without either a self-signed-cert fixture plus a Chromium
   certificate-trust override, or a narrow relaxation of that hardcoded
   scheme — both of which touch the same security-gating code this
   milestone deliberately left untouched. `tests/test_login_takeover_acceptance.py`
   proves the trusted UI wording, the real IPC/HTTP path, a clean
   `login_navigation_failed` refusal with zero half-open state, and clean
   process teardown. The credential-fill → confirm → `AUTHENTICATED`
   transition, and the site-scope/credential-surface state machine behind
   it, are proven only at the worker/pytest level: 39 tests
   (`tests/test_login_takeover_browser.py`,
   `tests/test_login_takeover_service_browser.py`) via an `http://`-scheme
   override plus a monkeypatched `_site_scope` that compares `host`/`host:port`
   instead of a PSL-derived domain — documented in both files' own
   module docstrings as test-infrastructure-only, never touching production
   code.
2. **Single-tracked-tab site-scope.** `_site_scope` (§12) follows exactly the
   tab `start_takeover` opened. A login flow that finishes in a **popup**
   window rather than navigating the original tab is not counted — the
   guard tracks popups (§6) but the site-scope check does not consider them.
   Real SSO providers that pop up rather than redirect would need this
   extended in S3.
3. **The credential-surface detector's known blind spots**, stated in its
   own module (§11): a login form built without `type=password` or a
   matching `autocomplete` value (a custom canvas keypad, an image-based
   entry, a neutral-named cross-origin iframe) is invisible to it; native
   passkey/WebAuthn browser prompts are OS/browser UI the DOM cannot see at
   all. The detector is deliberately over-inclusive elsewhere to offset
   false negatives it cannot detect, not to eliminate this class.
4. **The account-fingerprint fixture convention is S2-only.**
   `[data-lumi-account-id]` is a Lumi-authored attribute this milestone's
   synthetic fixture carries; no real site has it. What a real site's
   equivalent stable-identity signal should be is an explicit S3 design
   question, not answered here. S2 proves the mechanism (read one bounded
   value, hash immediately, never persist the raw form) against the one
   fixture built to exercise it.
5. **The Windows Playwright-teardown keep-alive timing issue is mitigated,
   not root-caused.** Full Chromium's lingering idle keep-alive connection to
   the broker proxy was observed, twice independently, to hang
   `playwright.stop()` on Windows during fixture teardown in
   `test_login_takeover_browser.py` and (separately, confirming the same
   class of issue exists in **pre-existing S0 code**, untouched by S2)
   `test_egress_broker_browser.py`. The fix applied —
   `tests/broker_teardown.py`'s `quiesce_broker`/`bounded` helpers, now used
   by six fixture files across S0/S1/S2 — waits for the broker's own
   `active_connections` to reach zero before stopping Playwright, and bounds
   every teardown step so a future regression fails one test loudly instead
   of hanging the whole suite. `pytest-timeout` (thread method) is a
   permanent safety net under all of them regardless. This is a **mitigation
   of an observed timing hazard**, not a proof of its root cause inside
   Playwright's or Chromium's internals — treat it as a working fix for a
   symptom that reproduced deterministically, not as a closed investigation.
6. **Electron main restart mid-takeover does not re-arm capture exclusion.**
   `capture.ts`'s `takeoverActive` flag and `BrowserProfileController`'s
   `openAttempts` set are both in-memory and rebuilt empty on a main-process
   restart. There is currently no wire path for the renderer to rediscover a
   still-open attempt's id afterward: `AgentBrowserProfileView` carries no
   open-attempt reference, and the `login_attempt_already_open` refusal a
   repeated "Sign in manually" click would hit also does not carry one
   (`projectProfileRuntimeError` maps it to a plain refusal message). Capture
   exclusion would only re-arm if something already held that attempt id and
   called `getLoginTakeover` again — which, as of this slice, nothing does
   automatically after a restart. A takeover that was genuinely still open
   across a main-process restart is a narrow window (a takeover has a bounded
   ≤1-hour TTL and restarts are not routine), but it is real and unfixed here.

---

## 23. Authenticode release-gate status

**Unchanged by S2, and still a gate.** The build remains unsigned — see S1's
review §17/§23 for the measurement (`chrome.exe`, `Lumi.exe` and the
installer are all `NotSigned`) and the correction to the plan's signing
assumption. S2 makes this gate *more* load-bearing, not less: the same
unsigned Chromium binary S1 already shipped now runs with a **visible**
window during a real sign-in, and this report does not claim packaged M8a is
ready for a real account. **Lumi must be Authenticode-signed before an M8a
build touches a real user account.** Nothing about S2 changes what that
requires; the repository still has no signing infrastructure.

---

## 24. What was not started

Explicitly **not** built, not stubbed, not partially implemented:

* **S3** — no `authenticated_read` grant, no `ACCOUNT_READ`, no site-scoped
  navigation for a model, no `reveal`, no disclosure manifest, no redaction,
  no provider disclosure card, no authenticated answer of any kind.
  `account_fingerprint`/`account_label_hash` are written by S2's own
  confirmation check now (unlike S1, where they were nullable and unwritten),
  but nothing reads an authenticated page on Lumi's behalf yet — S2 only
  proves *that* a human signed in, never what a model may do with it.
* **M8b (S4–S6)** — no element observation, no refs, no form epochs, no
  `protected_values`, no manifest digest, no field writes, no freeze entry,
  no dirtiness tracking, no handover.
* **M9, M10** — untouched.
* No new planner operation, grant kind, permission card or model-facing field
  was added anywhere in this slice — the five new IPC methods (§5) are all
  takeover-lifecycle operations a human drives directly, none of them reach a
  planner or a provider (§8).

---

## 25. Exit criteria

| # | Criterion (plan §27, S2) | Status |
|---|---|---|
| 1 | A human completes password, OTP and SSO-redirect logins on the fixture | **Met** — §4, §17; `test_password_and_otp_login_ends_authenticated_with_no_credential_surface`, `test_sso_redirect_through_a_second_origin_ends_back_in_scope` |
| 2 | Zero planner calls and zero provider calls during takeover, asserted by counters, not by inspection | **Met** — §8, §18 |
| 3 | No observation row, no diagnostic and no event payload contains any text from a login page | **Met** — §3 (attempt row schema has no text column), §18's real row-text scan |
| 4 | A credential surface during an agent step produces the signals-only record and a `login_required`-shaped pause | **Met** — §13's `login_credential_surface_present` refusal path; `test_a_click_alone_is_not_authentication_a_remaining_credential_surface_refuses` |
| 5 | Killing the worker mid-login yields `NEEDS_LOGIN`, never "signed in"; a genuinely completed login is recovered by re-observation alone | **Met** — §16; `test_an_attempt_from_a_stale_generation_is_marked_interrupted`, `test_a_worker_failure_during_confirm_leaves_the_attempt_unconfirmed` |
| 6 | The takeover watchdog expires and returns to `NEEDS_LOGIN` | **Met** — §15; `test_the_expiry_sweep_closes_the_window_and_settles_the_record` |
| 7 | Takeover ending off-scope refuses to continue | **Met** — §12–§13; `test_finishing_off_the_profiles_own_site_refuses_and_never_authenticates` |
