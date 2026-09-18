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
- **The packaged build is unsigned**, and unsigned distribution combined with a
  real session cookie is materially worse than unsigned plus a disposable
  context. **Authenticode signing is a release gate before M8a reaches a real
  user account**, not a documentation note. M7b's packaged acceptance
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
