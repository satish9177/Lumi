# Lumi security model

**Models suggest; the durable controller acts.** Nothing a model says —
realtime, text, or webpage text a model repeats — can approve, execute, choose
a URL, or name a DOM element.

## Trust boundaries

| Boundary | Rule | Enforced by |
| --- | --- | --- |
| Renderer → main | Fixed channels, positional primitives, sender/frame check, every payload re-parsed in main | `ipc-sender.ts`, `agent-ipc.ts`, `preload/index.ts`, boundary tests |
| Main → runtime | Allowlisted routes only, per-process bearer credential, loopback, no redirects | `agent-runtime-supervisor.ts` |
| Runtime → worker | Per-worker token, closed operation registry, allowlisted origins by *site name*, typed input | `app/browser/*` |
| Webpage → Lumi | Page text is data: typed fields only, bounded and shape-checked; free text is spoken only if it looks like a plain value | adapter, `agent-wire.ts`, `narratableText` |
| Model → controller | Closed vocabulary (enums, bounded integers, `HH:MM`, relative-day kinds, short names); unknown keys refused | `plan-wire.ts`, `parseVoiceTaskCommand`, `parseInterpretation` |
| Speech → approval | No approve/execute tool, command, IPC method or route is reachable from voice or text | `VoiceTaskBackend` type omits them; tests assert they are never touched |

## Credentials

| Secret | Lives in | Never reaches |
| --- | --- | --- |
| `OPENAI_API_KEY` | Electron main environment | Renderer (it gets a short-lived realtime client secret only), runtime, worker |
| `DEEPSEEK_API_KEY` | Electron main environment | Everything else |
| Google ADC / service account and access tokens | Electron main (`google-auth.ts`), used for Vertex text calls and the Gemini Live socket | Renderer (Gemini audio is relayed through main), runtime, worker, logs |
| Runtime bearer token | Minted per process by main | Renderer, logs, disk |
| Worker token | Minted per worker by the runtime | Main, renderer, database, logs |
| Database URL | Runtime environment; packaged app reads it from `%APPDATA%\Lumi\agent-runtime.json` in main | Renderer, installer, logs |

The Google token endpoint is fixed in code; a credential file cannot redirect
the exchange. Provider errors are classified into codes and never carry a
provider response body. The packaged runtime bundle is checked for `.env`
files at build time and in the packaged acceptance test.

## Gemini Live relay

A browser WebSocket cannot send an `Authorization` header, and a Google token
must not reach the renderer, so Electron main owns the Vertex socket. The
renderer may send only: `setup` (instructions + tool declarations, size-bounded
and name-checked; main chooses model, project, region and voice), `audio`
(base64 PCM, bounded), `audio_end`, `text`, `image` (PNG/JPEG, bounded) and
`tool_response` (known call id and tool name). Server frames are projected
into a closed event set; thought parts, unknown frames and oversized payloads
are dropped. `harness_say` exists only in unpackaged scripted builds.

## Model output

- Realtime tool calls and text-model JSON go through the same strict parser.
  A malformed or hostile answer is treated as that provider failing; the
  router may try another provider, which is safe because model calls have no
  side effects.
- Plans run at most four steps in a fixed order and always stop before
  approval. "Book it" only surfaces the trusted card.
- Relative dates are resolved by code from a relative-day *kind*; the model
  never invents a calendar date. Ambiguity ("next Saturday" on a weekday) is
  asked back.
- Refusals are not shopped to a more permissive provider.

## Memory

Preferences come only from an explicit user statement, carry provenance, fill
only constraints the current request left open, and never touch booked
values. Episodic summaries are app-authored, sanitised, and labelled as
non-authoritative in model context. The website — read now, by the worker —
is the only authority for prices, times and availability.

## Observability without leakage

Diagnostics are built from a closed field set (ids, codes, counts). Values
that are not code-shaped, or look like credentials, are dropped. No prompt,
transcript, form value or header is recorded. Diagnostics are visible only in
development builds or with `LUMI_DIAGNOSTICS=1`.

## Packaged runtime

The runtime, its interpreter, dependencies and Chromium ship inside the
application's `resources` directory and are byte-compiled at build time; at
run time `PYTHONDONTWRITEBYTECODE=1` keeps the runtime from writing beside its
own code (the packaged acceptance test checks this). Main starts only fixed
modules with fixed arguments and no shell; the renderer has no path, command
or code channel to them. A per-user install directory is writable by that
Windows user, as for any per-user application; an all-users install under
Program Files is not.
The only user-editable input is `agent-runtime.json`, read and validated by
main. Distribution builds must be Authenticode-signed; see
[PACKAGING.md](PACKAGING.md#windows-application-control).

### Release-signing boundary

Development packaging (`package:dir` and `package`) may be unsigned. Unsigned
output is not a release artifact and must not be used with a real account. The
separate `package:release` path fails before building unless an exact full
certificate-store thumbprint and explicit RFC 3161 URL are supplied from the
environment; credential-bearing, query-string and fragment URLs are rejected.
It rejects inherited publish/hooks and alternate signing configuration, forces
`publish: 'never'`, and forces the versioned release output directory. It then
treats Windows—not builder log text—as authoritative: `Get-AuthenticodeSignature`
must return `Valid` with the exact signer, and fixed SignTool verification must
establish the Authenticode policy chain and that a trusted timestamp is present
and verified by `/tw` for both `Lumi.exe` and the NSIS installer. That check
does not independently prove the timestamp protocol or server; the forced RFC
3161 signing configuration and fresh artifact mtimes tie it to this build.
SignTool must itself be `Valid` and Microsoft-signed. Missing tools, stale or
extra artifacts, ambiguous output and every non-`Valid` status fail closed.

The release signing configuration does not pass bundled vendor executables to
the Lumi signer. CPython, Chromium, ffmpeg, Node and native extensions retain
their vendor provenance; Lumi owns and signs its application executable, NSIS
installer and the temporary embedded uninstaller. Details and the exact v26
mechanism are in [PACKAGING.md](PACKAGING.md#third-party-executable-signing-policy).

A self-issued signer is rejected, but local trust can still include a private
enterprise CA. Therefore automation reports only `release signature verified`.
It never opens the real-account gate by itself: a release manager must also
confirm a publicly trusted code-signing CA and the intended Lumi publisher.
No production certificate has been acquired or validated, so real-account
release remains **BLOCKED**.

## Public page inspection (Milestone 7a)

Page inspection is disabled unless trusted configuration names public hosts.
The destination policy is enforced by main, the runtime and the worker, and the
worker's network guard applies it to every request and redirect hop. Page
content is `untrusted_environment` data: it reaches a model only inside a
delimited section, only for providers the user's approval named, and an answer
is stored only if its quotes and numbers are verifiably on the page. Details
and limits: [PAGE-INSPECTION.md](PAGE-INSPECTION.md).

## Public web research (Milestone 7b)

Research is disabled unless trusted configuration enables it. The user confirms
one bounded scope in the trusted UI; each step then consumes a single-use
authorization derived from that grant, and the runtime checks the step against
the scope, the budgets and the destination policy before anything happens. A
planner chooses one member of a closed operation union that has no field for a
selector, a script, a raw address, an HTTP method, a header or a cookie, and
observed links reach it as refs and hosts rather than addresses. The browser
context is task-owned, unauthenticated and disposable, and every semantic ref
dies with the document that issued it.

Research deliberately reaches hosts no allowlist named. Until Milestone 8 that
meant the destination was checked in one place and connected from another — and
a successful check was cached per host for the lifetime of the context — so a
hostile DNS server could answer once for the check and again for the connection.

## Egress broker (Milestone 8 S0)

The managed research browser no longer opens its own connections. Chromium is
launched through a loopback egress broker inside the browser worker
(`app/browser/egress_broker.py`), with `--proxy-bypass-list=<-loopback>` so that
loopback has no private door. For each connection the broker resolves the host,
requires every resolved address to be globally routable, and opens the socket to
an address from that one resolution. The address that passed the check is the
address on the wire.

TLS is never intercepted: a permitted `CONNECT` is spliced, so certificate
validation stays Chromium's and no interception authority exists. The broker can
also be put in a **frozen** mode in which it refuses every connection *before*
resolving anything — the primitive a later network freeze needs, because a DNS
lookup is itself an exfiltration channel.

This is a network-policy boundary for managed browser traffic, not an OS
sandbox, and not all of Lumi's traffic. Details and the full residual list:
[PUBLIC-RESEARCH.md](PUBLIC-RESEARCH.md) and
[reviews/milestone-8-s0.md](reviews/milestone-8-s0.md).

## Persistent browser profiles (Milestone 8a S1)

S1 adds a durable primitive and **no new user-visible capability**: Lumi can
create, open, lease and delete a persistent Chromium profile. There is still no
login flow, no authenticated page reading and no form interaction.

**The invariant, which is the reason a persistent profile is defensible at
all:** Lumi may *operate* a profile, but it must never extract or serialise the
authentication material inside it.

| Lumi owns | The browser owns |
| --- | --- |
| profile id, label, site (eTLD+1), allowed origins | every cookie, of any kind |
| status, revision, Chromium/Playwright/app version | access, refresh, bearer and CSRF tokens |
| the one-owner lease and its expiry | `localStorage`, `sessionStorage`, IndexedDB |
| `revoke_epoch`, account fingerprint *hashes* | Chromium's `Login Data` and `Cookies` |
| the profile directory path, derived and never transported | anything a model could replay |

There is no `storage_state`, no `context.cookies()`, no `add_cookies`, no cookie
or storage export, and no read of any file Chromium wrote inside the profile.
That is enforced by `tests/test_no_credential_extraction.py`, which scans Lumi's
own `app/`, `evals/` and `tests/` for call-shaped patterns, for page script that
reaches storage, and for Chromium's own profile filenames — and which is itself
tested against planted violations.

**Where a profile lives, and what its path may say.**
`%LOCALAPPDATA%\Lumi\browser-profiles\<profile-uuid>` — local, never roaming;
outside the install directory, the repository and `dist/`; and named by an
opaque UUID, so a directory listing does not disclose which accounts the user
holds. The site association is a database row. The path is derived from the id
inside the runtime and the worker and crosses **no** boundary: it is not in any
API response, worker request, log record, diagnostic or model prompt, and no
renderer, main-process or model input may name a `profilePath`, `userDataDir`,
`cookieFile`, `storageState` or `browserExecutablePath`.

**Windows permissions, stated plainly.** `%LOCALAPPDATA%` is already
user-scoped, and on creation Lumi drops the directory's inherited ACL entries
and grants the current user explicitly. That is the whole claim. Chromium
encrypts `Cookies` and `Login Data` with a DPAPI key bound to **the same user**,
so any process running as that user can decrypt the profile. Same-user malware
is outside Lumi's security boundary and no ACL changes that. Treat the profile
directory as secret material, because that is what it is.

**One profile, one site.** A profile is bound to exactly one registrable domain,
decided at creation from a pinned Public Suffix List snapshot (bundled, digest
checked on load and recorded in the packaged manifest; never fetched at run
time). The binding is immutable afterwards — there is no operation that
re-points a profile at another site, and a database trigger refuses one too.
Changing sites means deleting the profile and creating another.

**One owner at a time,** through two independent layers. The authoritative
lease is a `browser_profiles` row taken by a conditional `UPDATE`, bound to a
runtime generation and expiring. The backstop is an exclusive OS file handle the
worker holds inside the profile directory, which covers two Lumi installations
pointed at different databases and a lease that looks stale but is not. A stale
lease is reclaimed **only** when the OS handle is also free; when something
holds it the answer is a refusal, not a guess that the other owner is dead.
Chromium's own `SingletonLock` is neither relied on nor deleted.

**Version safety.** The build that last opened a profile is recorded. A newer
Chromium opens it and updates the metadata; an older one is **refused**
(`profile_browser_downgrade_refused`) before the directory is touched, and
nothing is deleted or repaired — a Chromium profile is forward-compatible only.

**Crash behaviour.** Session restore is never enabled and the crash-restore
bubble is suppressed, so after an unclean shutdown the profile opens with no
tabs. S1 does not implement authenticated task continuation, and claims none.

**Deleting is local.** Deleting a profile removes the directory Lumi created and
marks the row `DELETED`. It makes **no network request of any kind** — no logout
endpoint, no session revocation, no "sign out everywhere" — which is why the
trusted wording is *"This removes the sign-in data stored by Lumi on this
computer. It does not sign you out on the website."* It is ordinary application
deletion, not forensic erasure.

**Still brokered, still bounded.** Every persistent context is launched through
the S0 egress broker with the same proxy, credential, `<-loopback>` bypass and
QUIC-disabling arguments. Service workers stay blocked, downloads are refused,
and no browser permission is granted. Profile lifecycle involves **zero model
and zero provider calls**.

**Research sessions stay separate.** An M7b research session is an
unauthenticated, disposable context with no `user_data_dir`; an authenticated
profile is persistent and bound to one site. The two kinds refuse each other
rather than falling back, and the research code has no route to a profile at
all.

## Manual login and human takeover (Milestone 8a S2)

> S2 allows a user to manually authenticate in a Lumi-owned browser while
> Lumi is suspended. S2 still does not let an AI model read the authenticated
> page.

A takeover is a bounded interval (default 15 minutes, hard-timed-out and
watchdog-swept) in which a human, not Lumi, drives an existing S1 profile's
Chromium window. Full details, the state machine and the exact test evidence:
[reviews/milestone-8-s2.md](reviews/milestone-8-s2.md).

**Network mode.** Neither name is a formal enum value in code — both are
this codebase's own shorthand. During ordinary agent reading (M7a/M7b,
"`AGENT_READ`"), `PublicNetworkGuard` fetches every request itself, allows
only `GET`/`HEAD`, allows only a handful of resource types, and never lets
Chromium follow a redirect natively. During a takeover ("`TAKEOVER`"),
`TakeoverNetworkGuard` is deliberately wider: Chromium handles the human's
traffic exactly as an ordinary browser would — a login `POST`, a CAPTCHA
image, a browser-followed SSO redirect — and the guard refuses only a
non-`http(s)`/`data:`/`blob:` scheme at the request-interception layer.
**`file:` is a documented, reactive exception**: Chromium's local-file loader
never reaches the layer Playwright's `route()` intercepts, so the guard
instead watches for the top frame committing and retreats a forbidden-scheme
navigation to `about:blank` immediately after — the forbidden page's content
is genuinely rendered for a short, real interval first. Neither mode changes
the S0 broker's destination policy: TAKEOVER traffic is still brokered, still
requires a public address or an exactly-configured test origin, still uses
the same proxy, credential and `<-loopback>` bypass arguments S1 already set.

**Zero observation, zero model call, by construction.**
`LoginTakeoverService`'s constructor has no planner, provider or model-router
parameter to inject — there is nothing to call. Five takeover-participating
modules are scanned for model/provider-shaped tokens after stripping
docstrings and comments, in `test_no_credential_extraction.py`'s own idiom.
A real, measured proof backs the structural one: a full password+OTP login
against a real headed Chromium and a real database leaves `tasks`, `actions`,
`browser_dispatches`, `page_observations` and `research_observations` row
counts unchanged, and leaves no trace of the fixture's planted password or
OTP — or the raw account identity, only its hash — anywhere in the database.
`login_attempts` itself carries no page text, URL, credential signal or raw
identity column; only that a bounded interval existed and how it ended.

**The credential-surface detector is a deterministic DOM count, over-inclusive
on purpose, and it is not a proof.** Every check is `locator(...).count()` —
"how many elements match," never "what do they say." A false positive costs
one extra "finish signing in yourself" message; a false negative would mean
an authenticated transition on a page that still needs credentials, which is
the failure this exists to prevent. Stated plainly: a login form built
without `type=password` or a matching `autocomplete` value — a custom
canvas, an image-based keypad, a neutral-named cross-origin iframe — is
invisible to it, and native passkey/WebAuthn prompts are OS/browser UI the
DOM cannot see at all. `AUTHENTICATED` requires **both** an in-profile-site
result from the same pinned-PSL comparison S1 uses **and** an empty
credential-surface result — never either alone — and only the one
deterministic check run at "I'm signed in" may ever write it. The check
follows exactly the one tab a takeover opened; a login that finishes in a
popup rather than that tab is not counted (S3 residual).

**The account fingerprint is a one-way hash of one bounded, fixture-defined
attribute**, read and hashed in the same function, never returned, logged or
stored in raw form; `None` means "no stable signal found" and must be treated
as `unknown`, never a fabricated identity. The attribute convention
(`[data-lumi-account-id]`) is this milestone's own synthetic fixture, not a
real-site standard — what a real site's equivalent signal should be is an
explicit S3 design question.

**Screen capture is refused, not filtered, while any takeover is open.**
There is no reliable way to identify which capture source is the takeover
window (its title changes with every page), so every capture attempt is
refused outright rather than trying to exclude one source from a list. The
guard driving this is three-valued — *unreconciled*, *active*, *clear* — and
only the last of those permits a capture. A main process starts
**unreconciled**, so capture is refused from its first instruction until
main has read durable takeover state from the runtime (`active_takeover` on
each profile in `GET /browser-profiles`: an attempt id, a profile id, a
status and an expiry, and nothing describing the page). An Electron
main-process restart during a live takeover therefore cannot produce a
window in which capture works while a sign-in window is on screen, and
nothing has to remember an attempt id — not the renderer, not main. A
runtime that is unreachable, restarting or answering unparseably leaves the
guard refusing and is retried; a runtime outage is never read as "no
takeover is open", and no timeout expires into permission to capture. Within
a reconciled process the guard can still be stale by at most one poll
interval, in the fail-safe direction only (refusing longer than necessary,
never granting a capture a takeover still needs protected).

**Voice cannot start, confirm or cancel a takeover — structurally, not by
convention.** `VoiceTaskBackend` names only booking-task methods from
`AgentTaskController`; the takeover controller is a separate class never
given to the voice layer, so there is no `Pick` surface that could reach it
even by future accident.

**The fixed IPC boundary, restated.** Five channels, each named for exactly
what it does: list profiles, open a login window, confirm signed-in, cancel,
read one takeover's status. No method takes a hostname, URL, path,
executable, credential or browser argument, and there is deliberately no way
for the renderer to create a profile at all.

## Authenticated account reading (Milestone 8a S3)

The honest name for the capability is **`account_scoped_read`**: Lumi issues
only `GET` and `HEAD` requests, to one site, in a browser carrying the user's
session for that site. Lumi performs no intentional change. **The website may
still record the visit** -- mark something as read, update "last active", extend
a session, count analytics, write account activity -- and Lumi can neither
prevent nor reliably detect that. It is never described as read-only,
no-effect or invisible, and the trusted card says so *before* the Allow button.
The registry names the effect `ACCOUNT_READ`, deliberately not `READ_ONLY`, and
a fixture endpoint that increments `read_count` on `GET` proves the wording
honest (`test_authenticated_service_browser.py`).

**Authority.** An `authenticated_read` grant (the existing `task_grants` and
`step_authorizations`, no second framework) is immutable and bound to one
profile, one site, one account fingerprint, the profile's `revoke_epoch`, exactly
one provider, `GET`/`HEAD`, and budgets no larger than M7b's. Only a trusted
renderer click confirms it; the confirming statement also checks, in SQL, that
the profile is still `AUTHENTICATED`, at the same epoch and fingerprint. Every
step consumes a single-use authorization whose `UPDATE` re-checks the same
profile conditions -- there is no `SELECT`, decide, `UPDATE` window. Steps are
recorded `AUTHORIZED`, never `APPROVED`.

**Before any text is projected, in this order, inside the worker:** the tab is
on a real document inside the profile's site; the credential-surface detector
runs (bounded counts); the account identity is derived, hashed at once and
compared with the grant's fingerprint; only then is text read, **redacted line
by line**, split and bounded. A credential surface returns signals only. A
missing identity is `account_identity_unknown`; a different one is
`account_changed`, which bumps the profile epoch, clears its fingerprint,
returns it to `NEEDS_LOGIN`, revokes its open grants and pauses the task.
Session expiry is `login_required` and leaves the epoch alone, so the same
account may resume the same grant. All four pauses (`left_site_scope` is the
fourth) are decided by code; no planner or provider is consulted, and the
page text never leaves the worker.

**Account identity has no site-agnostic signal.** The S3 design spike
concluded that ordinary pages offer no stable, site-agnostic account identity
that can be read without cookies/storage, a per-site adapter or a guessing
heuristic. The only reviewed signal is the fixture's `data-lumi-account-id`. On
a real site it is absent, so the fingerprint is unknown, a task cannot start
and the safe rule `unknown -> refuse` holds. This is why real-account reading
does not work yet, and it is intended.

**Network.** The agent-read guard is not the takeover guard. It allows `GET`
and `HEAD` only; refuses every other method, WebSocket, download, popup and
non-document top-level navigation outside the profile's registrable site
(`left_site_scope`, refused before it is contacted; redirects are decided per
hop from the `Location` header, never followed by the browser); allows public
third-party subresources (the broker still refuses private, loopback,
link-local and metadata addresses); and keeps QUIC off. An authenticated link
can be a capability, so links are never mirrored anywhere: the worker holds
each address for one document epoch, the runtime persists only refs, redacted
labels and hosts, and there is no URL column in any S3 table.

**Private data.** Account-private text goes to exactly one provider, named by
the grant and chosen from a list main built from its own configuration. The two
model classes that carry it (`authenticated_planning`, `authenticated_answer`)
make `ModelRouter` refuse to run without the grant's recipient rule, refuse an
image, and stop after the first provider attempted -- a failure, a timeout or
an answer that fails grounding ends the run with `model_unavailable`; there is
no second company and no second model. No authenticated screenshot exists
(`max_vision_calls` is the literal `0`). Identifier redaction (email, phone,
Luhn-valid card, nine-plus digit runs) happens in the worker before the text is
returned, hashed, stored or sent; the runtime refuses any observation that
still contains an identifier-shaped run. **Redaction is a reduction in
exposure, not anonymisation**: names, usernames and short numbers are
untouched, and patterns over- and under-match. Grounding runs on the redacted
text the provider received.

**Classification firewall.** Evidence and answers live in `authenticated_observations`
and `authenticated_answers`, whose `classification` is a `CHECK` that admits one
value; nothing is written to `research_*`. The context builder excludes an
authenticated task from every other prompt, episodic memory refuses
`account_private` summaries, diagnostics carry closed fields only, and a
planted-marker test proves the marker reaches the approved provider, the private
tables and nothing else. Deleting a profile removes its evidence and revokes its
grants; task evidence cascades with the task.

**Residual limits.** The website may record every read (above). A lost read is
`OUTCOME_UNKNOWN`, never retried, and only a fresh `observe` may follow. The
redactor and the credential detector are heuristics. The account-identity signal
exists only for the fixture. The real-account acceptance pass in the M8 plan was
**deliberately not performed**: the build is unsigned, and Authenticode remains a
release gate before any real personal account is used.

## Authenticated form observation (Milestone 8b S4)

> **S4 observes form structure only. It cannot type, choose, check, click, upload
> or submit anything.** Lumi can observe a field; Lumi cannot change that field.

A page observation of a signed-in account now also carries a bounded, **value-free**
inventory of the page's form controls: forms (`f1`-`f5`), same-origin frames
(`fr0`-`fr4`), elements (`e1`-`e40`) and options (`op1`-`op25`), as opaque
worker-issued refs with a role, control type, a redacted accessible name,
`valueState` (`empty | filled | unknown`), `required`, `enabled`, `visible`,
`readOnly`, `maxLength`, redacted option labels and `submitLike`. The step
vocabulary is unchanged (`navigate observe reveal tab history stop`); there is no
`prepare_form`, `set_value`, `select_option`, `click`, `focus` or `type`, and the
network authority is unchanged (`ACCOUNT_READ`, GET/HEAD, S0 broker).

- **No current value leaves the worker.** The page-side helper decides "empty or
  filled" *inside the page* and returns one word; the value string never reaches
  Python, the runtime, the database, a log or a provider. The same holds for
  ordinary text fields, not only passwords.
- **No DOM identity leaves the worker.** Not an id, name, class, tag, selector,
  path, HTML, coordinate, dataset, form action/method, raw `autocomplete`, raw
  option `value=` or frame URL. The models are `extra="forbid"`.
- **Exclusion happens while listing.** Password, file, hidden, one-time-code,
  `current-password`, `new-password` and `webauthn` controls never get a ref, and a
  frame containing any credential-shaped control contributes nothing. A page the
  S2 detector flags, and any account-identity failure, yields no inventory at all
  (the S3 gates run first).
- **Same-origin frames only.** A cross-origin frame is never evaluated: not even
  its labels are read.
- **Names and labels are page text.** Redacted with the S3 policy inside the
  worker and re-checked by the model; bounded to 120 characters.
- **Provider disclosure is not widened.** The inventory is stored locally
  (`authenticated_observations.element_inventory`, `account_private`,
  `untrusted_environment`) and is not part of the runtime's response, the planner
  prompt, the answer prompt or `authenticatedObservationLines()` (S5 exposes it only under a separate, confirmed form-planning grant). A planted marker
  is asserted present in the local row and absent from every provider payload,
  public research table, memory path and diagnostic.
- **Stale refs fail closed.** A per-tab monotonic `form_epoch` rises whenever the
  inventory fingerprint changes, so a page that re-renders its form without
  navigating still invalidates every element ref. A ref is valid only for the exact
  document epoch and form epoch, and is re-derived from the live DOM (one control
  at the ordinal, same control count, same semantic identity) before use; no
  nearest match, no fuzzy fallback, no forcing. No `ElementHandle` is held across
  steps; the worker-only locator description is never persisted or returned.
- **Structural no-write guard.** A source scan (AST for Python, a reviewed
  allow-list for the one static DOM helper) fails on any call-shaped mutation
  (`fill type press click check select_option set_input_files dispatch_event
  request_submit ...`), on assignment to anything but two local records, and on a
  second value read. A fixture with input/change/focus/click/keydown/submit/
  autosave counters proves that observing a form triggers none.

**Residual limits.** A DOM replacement with an *identical* observed semantic
fingerprint is indistinguishable by construction; S4 detects structural changes
visible to the reviewed fingerprint, not every re-render. `submitLike` is a
heuristic that can only *remove* a future capability and never means "safe". The
accessible-name algorithm is a simplified worker-authored subset of ARIA, not a
browser accessibility tree. Shadow DOM and cross-origin frames are not inventoried.
A synthetic fixture only: no real account or real form was used, and Authenticode
remains a release gate.

## Form planning and exact disclosure approval (Milestone 8b S5)

> **S5 is approval-only. It performs zero browser writes.** Lumi can now say exactly
> what it *would* place into each approved field, and a person can approve exactly
> that -- but there is still no operation anywhere that can place anything in a field.

**Three separate authorities, never conflated:**

| Authority | What it is | What it permits |
| --- | --- | --- |
| `form_prepare` **grant** | a `task_grants` row (kind `form_prepare`), confirmed by a trusted click | letting **one** named provider see a bounded form structure and *masked* previews of chosen saved details |
| `prepare_form` **proposal** | a planner output, validated by the controller | nothing: it is a *description*, not an operation |
| manifest **approval** | the existing `actions` + `approvals` rows | the user's exact, single-use approval of one disclosure manifest |

**Why a separate grant after S4.** The user allowed S3 account *text* disclosure. They
did not allow up to 40 field labels, option labels or saved-detail previews to be
sent to a provider, and the later manifest approval happens too late to authorise that
model disclosure. Ordinary authenticated reading is byte-for-byte what S3/S4 sent: page
text and links, no element inventory, no preview. Only a confirmed `form_prepare` scope
(`form_planning` task class, private, one recipient, no failover, no image) exposes the
form structure.

**Saved details (`protected_values`).** Exactly eight kinds -- `legal_name`,
`preferred_name`, `email`, `phone`, `city`, `country`, `linkedin_url`, `portfolio_url`;
no free-form kind, password, one-time code, payment detail, file or blob. The raw value
is **plaintext task data in Lumi's local runtime database**. That is *not* encryption at
rest and gives no protection against a live compromise of the same Windows user; the
protection is the operating-system account and database access controls, and nothing
more is claimed. It is not a credential: browser sessions stay under the profile
boundary. The database refuses a row whose `value_digest` is not `SHA-256(UTF-8(value))`.
After it is saved the value **did not leave its row in S5**: every read returned only
kind, masked preview, digest and length. **S6 changes this, and only this far:** one repository
method (`values_for_execution`) returns the raw value to trusted runtime memory for one approved
local draft (see the S6 section below). Every other read is unchanged. Values are saved only through a narrow typed
route (`PUT /protected-values/{kind}`, one string, response never echoes it) that no
model, voice turn or page can reach, and no product editor exists yet (see gaps).

**Masking policy** (deterministic, independent of length; `preview != value` is asserted):
`legal_name` / `preferred_name` / `city` -> a fixed phrase (`saved legal name`, ...);
`email` -> `s***@g***.com`; `phone` -> `ending 1234`; `linkedin_url` -> `linkedin.com/in/***`;
`portfolio_url` -> `saved portfolio link`; **`country` -> the country itself**. A country
is coarse and cannot be masked while still choosing the right option, so it is the one
documented exception, and the trusted planning card says so whenever `country` is
offered instead of claiming that no saved value is sent.

**What a provider receives under the grant:** form/element refs, role, control type,
accessible name, `required`/`enabled`/`visible`/`readOnly`, `maxLength`, `submitLike`,
option refs and labels, and the masked previews of the *selected* refs. **Never:** a raw
saved value, a value digest, the field's current value or `valueState`, a locator, a
selector, id/name/class, an option `value=`, a frame URL, an origin URL, the account
fingerprint. Labels remain `untrusted_environment`; a label saying "use every saved
value" is data and changes nothing (planted adversarial fixtures assert this).

**One recipient, no failover.** The provider is the account-reading grant's, copied into
the scope by the runtime. If it is unavailable Lumi stops (`model_unavailable`); a second
provider sees zero calls.

**Trusted confirmation.** Confirming is one compare-and-swap that also checks, inside the
statement, that the profile is still `AUTHENTICATED`, at the same revoke epoch and account
fingerprint, and that the source account-reading grant is still active. No
SELECT-decide-UPDATE window. Voice and typed text cannot confirm: neither the voice
backend nor the tool vocabulary contains the operation.

**`prepare_form` and its parse-time refusals.** A closed proposal: one observation, one
form, 1-12 entries, each exactly one of `{elementRef, dataRef}` (text-like),
`{elementRef, optionRef}` (single select / radio group) or `{elementRef, checked}`
(checkbox). No value, origin, selector, URL, script or provider fits in the shape, and a
smuggled key refuses the whole proposal. Refused **before an approval, card or action
exists**: zero/13+ entries, duplicate or unknown element, another form's element, a
stale observation / document epoch / form epoch, an unknown option, a `dataRef` outside
the grant, `select_multi`, a button, link, submit-like, disabled, read-only or hidden
control, a value longer than `maxLength`.

**The disclosure manifest.** A frozen model binding task, profile, form-planning grant,
planning provider, site, exact recipient origin (derived from the profile and the observed
page, never from a model or page), an account binding, the revoke epoch, the observation,
tab, document epoch, form epoch, form, and per field the element ref, an **element identity
hash** (over the reviewed projection: form, frame, role, control type, name, the four state
flags, submit-likeness, length limit and both epochs -- no selector, id, value or
coordinate), and the `dataRef` + `valueDigest` + masked preview, or the chosen option
ref/label/identity hash, or the checkbox state. Fields are canonically ordered and
`manifest_digest` is SHA-256 over canonical JSON; changing **any** approved fact changes
it (each is mutated independently in tests). The raw value never enters the manifest.

**Exact approval.** `POST /actions/{id}/field-disclosure/approve` takes an expected revision
and nothing else. In **one transaction, under the task lock**, it re-checks: the grant is
active, unexpired, on the same account and revoke epoch; every bound saved value still has
its digest (`protected_value_changed`); the observation is still the newest for its tab
with no later document/form epoch or different worker (`stale_*`); the origin is unchanged.
It then grants, claims (single-use, re-checked in SQL against revision, digest and expiry)
and terminalises the approval with a finished attempt whose result is `prepared_nothing`.
That attempt creates **zero browser dispatches**, calls **zero worker operations** and
changes **no page**. The generic action routes (`approve`, `attempts`, `reconciliation`,
`approval-request`, and proposing the tool) refuse a disclosure action, so the freshness
checks cannot be bypassed. A consumed approval cannot fund a second use: a second approve
is refused, its approval id cannot be attached to another attempt (unique constraint), and a
changed manifest, saved value, profile or revoke epoch is refused before any consumption.

**What persisted state cannot prove.** Approval-time checks see what Lumi last
*observed*; they cannot see an unobserved DOM mutation. S6's worker-live revalidation,
made immediately before any write, is what covers that -- S5 does not pretend otherwise.

**Diagnostics and memory firewall.** Task events carry ids, digests of scopes/proposals and
counts only (`protected_value_count`, `allowed_data_ref_count`, `form_count`,
`candidate_element_count`); never a label, option, preview, value, origin, fingerprint or
manifest. The form plan, previews and manifest are `account_private`: they are excluded from
other requests' contexts, episodic memory, public research and global diagnostics.

**Structural no-write proof (as of S5).** The S4 source scanner covered every S5 module and
forbade `fill type press click check select_option set_input_files dispatch_event
request_submit ...` everywhere in the worker, and no `form_drafts` table, `frozen_at` column
or `LOCAL_DRAFT` effect existed. **S6 replaces "none anywhere" with "exactly these three
names, in exactly one file"** -- see the S6 section.

**Residual limits.** Values are plaintext in the local database (above). A country preview
is the value. Approval cannot detect a DOM change Lumi did not observe (S6's job). The
element identity hash uses the projected identity, not the worker-internal ordinal. No
product UI for entering saved details exists yet. Only a synthetic fixture was used: no
real account, saved detail or form; the packaged `Lumi.exe` is `NotSigned`, so the
Authenticode gate and the no-real-account gate remain open.

## Network-frozen local form draft (Milestone 8b S6)

> **Lumi fills the form in its own browser with the network frozen, verifies the values are in the
> fields, and hands you the browser. Nothing was sent while Lumi was filling. If the form needs the
> network to accept a value, Lumi stops and tells you. Lumi never submits.**

It is *not* a claim that Lumi works on every form, submits applications, saves a draft on the
website or knows whether a site accepted anything.

**Two-layer freeze (the primary mechanism).**

| Layer | What it does | What it closes |
| --- | --- | --- |
| Playwright guard (`AccountReadNetworkGuard`) | a `frozen` flag is checked **first** in `_handle`, before the request is inspected, fetched, resolved, proxied or followed; an exact in-flight count is taken from the moment a request enters (no `await` between the check and the increment) | every request the page could make, including redirects, beacons, images and third-party fetches |
| Egress broker (`EgressBroker.freeze_and_drain`) | mode `FROZEN` is set **first** (nothing new resolves or dials; a connection that arrives is refused before it is registered), then every relay task is cancelled and the set is awaited to empty | DNS-name exfiltration (`https://<value>.exfil.invalid/`), and a CONNECT tunnel or keep-alive relay that was **already open** |

`freeze()` alone is not enough: a relay opened before it keeps carrying bytes. A byte-counting server test
opens a tunnel, freezes, and shows zero new bytes and zero new resolutions afterwards; a negative control
shows that `freeze()` without the drain leaves the tunnel flowing. Entry order: page settle -> guard frozen
-> guard `in_flight == 0` -> broker frozen -> every relay cancelled -> `active_connections == 0` -> proof.
Any failure restores the open state (broker, then guard) and nothing was written. A request open before the
freeze (a streaming fetch) gives `page_never_settles`: Lumi never freezes on top of an open request.

**Ownership.** The broker is worker-global, so the freeze has one owner (profile, dispatch, worker
generation). While it exists another profile, task, research session, public read or takeover is refused
(`freeze_owned`, or `form_is_dirty` for the owner's own profile), and the release functions are bound to
the owner's dispatch id: no task can thaw another's draft. There is no `setNetworkMode`, boolean or generic
network call anywhere -- a test scans the worker for one.

**Order (durable intent before effect).** exact approval claimed + attempt durable + action `EXECUTING` in ONE
transaction -> dispatch row durable (`frozen_at NULL`) -> worker enters the freeze -> worker proves it ->
runtime writes `browser_dispatches.frozen_at` (compare-and-set, only while `DISPATCHED`, on that worker
generation, while NULL) -> account re-checked from the database -> ONE dispatch writes every field. The worker
ALSO checks, immediately before every field write, that the owner is this dispatch, the guard is frozen, the
broker is frozen, `guard.in_flight == 0` and `broker.active_connections == 0` (`not_frozen` otherwise). A test
observes from the worker's side of the wire that `frozen_at` was already set when the write request arrived.

**Three field primitives, one file.** `set_value` (`fill`), `select_option` (by reviewed option ordinal, never a
raw `value=`) and `set_checked`, inside one approved dispatch, in `app/browser/local_form_draft.py` only. Before
each write the element is re-derived from the LIVE DOM (same count, ordinal and semantic identity), its identity
hash recomputed and compared with the manifest's, and the structure fingerprint compared; after each write the
control is re-derived again, its value read back inside the worker, hashed, and required to equal the approved
value; then the value-free structure is compared once more. A source scan allow-lists those names in that file and
fails on any click, key press, typing, upload, submit, drag, hover, focus, dispatched event or script evaluation
anywhere else in the worker, and on planted violations.

**What stops a fill.** A value the page resets, a dependent select whose options need the network, asynchronous
validation (`aria-invalid` on a control just written), a network-loaded option that is missing, or a form that
re-renders itself: `unsupported_under_freeze` (or `element_changed` for a re-render that tried nothing). Lumi
never turns the network on for one request and never types text into a select. A fill that stops after one or
more verified writes leaves the page dirty and frozen (status `STALE`, task paused, card says the form needs
manual review). A fill that stopped before any verified write destroys any page a primitive touched, while
frozen, and only then thaws.

**Dirty.** From the first primitive, the worker session is dirty: every agent read, navigation, history move, tab
change and profile close is refused (`form_is_dirty`), the runtime refuses planning, reading, closing and
takeover for that profile independently, and **no provider is called** in freeze entry, fill, verification,
discard or handover, so protected values the page reflects in its text cannot reach one. `beforeunload` is never
used as a control.

**Discard never "lifts the freeze and reloads".** A dirty page thawed while alive can autosave the instant a
request can leave. The tab is closed with `run_before_unload=False` while frozen, `is_closed()` is checked, draft
ownership is cleared, and only then does the broker open, then the guard. A negative-control test shows the same
fixture page, thawed alive, does autosave; the real discard test shows zero.

**Handover is the disclosure boundary.** It restores the network while the dirty page is alive, so it needs a
SECOND exact approval (`handover_form`, R2, bound to the draft digest) and re-verifies the live page immediately
before thaw (`draft_changed`, still frozen, if a human changed a field in the visible window). The wide human-mode
guard is installed first (while the broker is still frozen), then the broker opens, then the read guard is
removed; the browser is never closed or reopened. Afterwards Lumi says only: "You took over in the browser window.
Lumi did not submit anything and cannot tell you whether the site accepted or saved it." A lost handover response
is `OUTCOME_UNKNOWN`, never retried, never reconciled.

**Preparation mode (headless -> headed).** A local draft cannot be made in the headless read context and kept by
reopening it headed (closing the context destroys the DOM). So before the manifest exists the worker captures the
current in-site page into its own memory (never returned, never a renderer/model/IPC field), the profile is closed
and reopened **headed**, the worker navigates back to that page internally under the read guard, and a completely
fresh S4 observation is taken through the normal credential/identity gates. Every proposal and approval made from
the headless document is rejected, and an observation the worker did not issue for the current form is refused
(`stale_observation`). If the page cannot be returned to safely the user opens the form themselves.

**Historical S5 approvals are terminal.** A `form-prepare-v1` manifest still parses and its digest still verifies,
but it is never executable; a `prepared_nothing` result is never reinterpreted or replayed. Only a new
`form-prepare-v2` approval created in preparation mode can fund one draft.

**Recovery.** A draft is browser-local and is lost on any restart; the row says what Lumi prepared, not something
restorable. A crash with `frozen_at` set may say the remote effect was impossible under the verified freeze (and
`local_state: lost`); with `frozen_at` NULL nothing is claimed. Either way: `OUTCOME_UNKNOWN`, task paused
`browser_lost`, no retry, no reconciliation. At startup every live draft row is closed as lost.

**Residuals, stated plainly.** (1) The freeze is proven on a synthetic fixture, not real sites; a real site run
demonstrates usability, never absence of effect. (2) A request whose bytes were sent before the freeze cannot carry
values that did not yet exist, and a streaming request is refused outright -- but a request that was in the guard
when it began is only *waited for*, bounded. (3) Handover ends the guarantee: the site may immediately autosave
what is in the form, and the card says so before the click. (4) Values are plaintext in the local database (S5).
(5) Screenshots and vision captures are never taken of a dirty form, but the headed window is visible to anyone at
the machine. (6) Service workers stay blocked, so a queued Background Sync write cannot be flushed at handover.
(7) The Authenticode gate and the no-real-account gate remain open (below).

## Windows desktop observation (Milestone 9 S1)

S1 lets the runtime **read** a Windows application semantically. It cannot operate one. This section is the trust model; `docs/reviews/milestone-9-s1.md` is the evidence.

**Zero input, checked in source.** `app/desktop/` contains no way to change a desktop UI: no invoke, set-value, select, toggle, scroll, focus, activate, foreground, show/hide, move/resize, close, launch, `SendInput`, keyboard/mouse, message posting, clipboard or capture primitive. `tests/desktop_source_scan.py` parses every module with `ast` and fails the suite on any such member, name, definition, import or dynamic-dispatch call, on any process-access right stronger than `PROCESS_QUERY_LIMITED_INFORMATION`, and on ordinal exports and numeric pattern ids. It is itself tested against over a hundred planted violations, including every bypass an independent review found. A deny-list is a tripwire, not a proof, so the two files that touch the OS are also held to an **exact allowlist**: `win32.py` may call only 24 named kernel/user/advapi/dwm query entry points and `uia_backend.py` only the listed COM members and read-only pattern interfaces, so any other call fails the suite and forces a deliberate, reviewed edit. The fixture the tests use counts every message that reaches it from outside, and observation leaves those counters at zero.

**Isolation.** The desktop worker is its own process, started only by the runtime with a fixed argument vector and an allowlisted environment (no database URL, no provider key, no browser profile path, no cookie, no task history, no shell, no executable path). It binds `127.0.0.1` on a port it reports over a pipe only the runtime holds, requires a per-start credential compared in constant time, refuses a request addressed to another worker generation, rejects any non-loopback `Host` and any `Origin`, and answers 404 to every route but two typed ones. The runtime's client ignores proxy settings from the environment and the registry (`trust_env=False`), so the credential and desktop text cannot be routed through a proxy. UI Automation runs on one dedicated MTA thread inside it; a provider that hangs cannot hang the runtime, because the process (not the thread) is the boundary: the worker reports `desktop_observation_timeout`, poisons itself, and the runtime kills and fences that generation. The parent watchdog and the runtime's kill-on-close job end it with the runtime. Any unexpected failure inside the worker is reduced to one text-free code before a framework can log it, so a hostile string cannot leak through a traceback.

**Never a target.** Lumi's own process tree is excluded **by ancestry from trusted process identities** (the runtime and Electron, bound by creation time so PID reuse cannot forge or evade it), never by window title. That covers the renderer, DevTools, the runtime, the browser worker, every Chromium it owns (sign-in, takeover and form-preparation windows) and the desktop worker. When the runtime created its kill-on-close job, every process in that job is also Lumi's, whatever its parent chain: ancestry alone cannot see through a launcher that exited between a trusted root and a Lumi window, and the job can. A process snapshot that fails or comes back empty is a failure (never "nothing descends from Lumi"), windows are enumerated before the snapshot, and a live process the snapshot does not know is treated as Lumi's rather than described. Credential/consent broker processes are denied by image name as a second layer.

**Never inspected.** An elevated (higher-integrity) or integrity-unverifiable process is withheld from the inventory without reading its title, and is re-checked before any traversal (`elevated_window_refused`, `integrity_unverifiable`). The comparison is against `min(Lumi's level, Medium)`, so even an elevated Lumi does not read an elevated window. Lumi requests no administrator right, no `uiAccess`, no secure-desktop access. A surface containing a credential input (`IsPassword`, or an edit named like a password/PIN/OTP/code) is refused as a whole with zero content (`credential_surface`); the password flag is read before any text, so the value is never fetched, and the scan looks past the projection cap.

**Identity.** A window handle is not an identity. Each surface slot binds `(pid, hwnd, process creation time)` and an epoch that only rises, so a recycled HWND or PID, a restarted process or a new occupant of a slot can never inherit an old `(surfaceRef, surfaceEpoch)` (`stale_surface`). The pair is unique **within one worker generation** (a new worker starts every slot again), so the runtime request also names the worker generation the surface was listed under and refuses any other (`stale_worker_generation`) without touching the healthy worker. Control refs belong to one observation of one epoch; a materially changed structure or a newer observation kills them. Re-resolution is by exact match only (`element_missing`, `element_ambiguous`, `element_changed`); there is no nearest-label fallback.

**Local and private.** Every observation is `desktop_private` and `untrusted_environment`. In S1 it goes to **no** provider, voice model, planner, memory, research context or task summary. A planted marker in a fixture edit control is present in `desktop_observations` and in no other table, log or output, and the TypeScript side has no reference to the routes, the storage or the schema (`tests/test_desktop_source.py`, `src/main/agent/desktop-firewall.test.ts`). A later slice that defines an explicit disclosure scope must change those tests on purpose. Diagnostics carry counts, a truncation flag, a duration, a worker generation, an observation id and an error code, never a title, a name, text, a PID/HWND, a path or a control ref.

**What crosses the boundary.** Roles (a closed vocabulary), bounded accessible names and values, and states. Not: window handles, process ids or paths, command lines, bounding rectangles or coordinates, AutomationId, ClassName, FrameworkId, RuntimeId, raw property dictionaries or COM objects. Those may exist in worker memory as a re-derivation locator and nowhere else.

**Residual risks (honest).**
- UI Automation quality is the target application's. Trees can be incomplete or misleading, and same-integrity content is **untrusted** even though it is readable.
- An identical semantic replacement (same roles, names, patterns and enabled states) is indistinguishable, so it does not bump the epoch; static-text labels are left out of the fingerprint on purpose because a clock would otherwise make every read unstable.
- Credential detection has blind spots: a secret field that is neither flagged `IsPassword` nor named like a credential is not detected; a password manager's window is not special-cased.
- Touching a Chromium/Electron/Office application with UI Automation can switch that application into its accessibility mode (a performance side effect, not a UI change).
- UIA providers can hang; killing the worker is containment, not prevention. A window that is merely *big* is not a hang: reads run under a soft time budget and a 300-sibling cap, batch their properties with UIA cache requests, and come back marked `time` or `scan` rather than dying at the 30 s hard deadline. On a Win32 window made of hundreds of separate HWND controls (the slowest case, measured) a full 200-node read still takes about 5 s.
- A partial (time-truncated) read compares only the nodes it and the previous read both saw, so a slow window does not needlessly bump its epoch; the cost is that a change beyond the covered prefix goes unnoticed until a read reaches it.
- The credential scan is bounded (1000 elements, depth 24, 300 siblings per parent): a credential input beyond that is not seen, and the observation declares `scan` truncation instead of claiming completeness. Credential detection by name is English-centric with a few common translations.
- A window replaced by another with the same HWND *inside the same process* before any refresh is not distinguished (Windows handle values carry a uniqueness counter, so this is rare).
- pywinauto's package import loads its own keyboard, mouse and application modules into the worker even though nothing here references them; Lumi's code has no call path to them (source scan), but a supply-chain compromise of a dependency would.
- Desktop text at rest is retained for at most 25 observations and 24 hours.
- Elevated, UAC and secure-desktop applications are unsupported by design. There is no coordinate or visual fallback yet.
- Production-signed installed validation remains blocked by the missing Authenticode certificate.

## Known gaps

- The broker constrains Chromium, not its host process. A compromised browser
  worker can open sockets directly and bypass every check in this section.
- Runtime-side traffic is not brokered: the configured `public_search` endpoint,
  provider calls and the database connection all go direct.
- Outside frozen mode the broker still resolves names, so a hostile page can
  still signal data through a DNS label. Only the freeze closes that.
- `wss:` and `https:` are the same `CONNECT` at the broker, so WebSocket
  blocking remains a Playwright-level control in the network guard.
- **A persistent profile is an impersonation artefact, and same-user isolation
  does not exist.** Any process running as the user can decrypt it. This is the
  largest new risk M8a introduces and nothing in the design removes it.
- **The existing packaged build is unsigned**, and unsigned distribution
  combined with a real session cookie is materially worse than unsigned plus a
  disposable context. A fail-closed Authenticode release command now exists,
  but no production certificate or successfully verified production artifact
  exists yet. **Authenticode plus release-manager confirmation of the public CA
  and publisher identity remains a release gate before M8a reaches a real user
  account**, not a documentation note. M7b's packaged acceptance
  substituted the stock Electron executable to get past Smart App Control, which
  disables asar integrity validation; that workaround is **development-only** and
  is not acceptable for a build holding real session data. Machines with Smart
  App Control or WDAC in enforcement mode can block unsigned binaries (observed;
  see PACKAGING.md).
- The bundled Public Suffix List snapshot ages. A suffix delegated after the
  pinned version is classified by the old rules until the next Lumi release.
- Profile deletion is ordinary unlinking. It does not defeat a journalling
  filesystem, an SSD's wear levelling, a shadow copy or a backup.
- Voice narration is steered, not enforced: a live model could still misspeak.
  The card and timeline remain the authority.
- **The credential-surface detector has known blind spots**: a login surface
  without a `type=password`/matching-`autocomplete` field, or a native
  passkey/WebAuthn prompt, is not detected. It is over-inclusive elsewhere to
  offset false negatives it cannot see, not to eliminate this class.
- **A `file:` navigation during a takeover is briefly, genuinely rendered**
  before the guard retreats it — a reactive, not a preventive, control (see
  above).
- **Capture is permitted on a machine with no agent runtime without querying
  anything.** When main has *established* that no runtime exists here
  (packaged with a missing or invalid `agent-runtime.json`, a runtime that
  could not be prepared, or the runtime's own files not being present at
  all), the capture guard resolves to *clear* without a query: the headed
  sign-in browser only exists as a descendant of a runtime process, and only
  main's own supervisor starts one. This is the single non-runtime answer the
  guard accepts; it is never taken while runtime startup is still deciding,
  and never for a runtime that is installed but failing, which stays an
  outage and keeps capture refused.
- **The Windows Playwright-teardown keep-alive hang found in S2 is mitigated,
  not root-caused**: a bounded wait for the broker's connections to quiesce
  before stopping Playwright, plus a hard `pytest-timeout` net. Treat it as a
  working fix for a reproducing symptom, not a closed investigation into
  Playwright's or Chromium's internals.
