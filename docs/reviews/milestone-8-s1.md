# Milestone 8a S1 — persistent browser-profile foundation

Implementation report. Branch `lumi-agent-v2`.

S1 adds **no user-visible capability and no new authority**. It builds one
durable primitive — a Lumi-managed, persistent, isolated Chromium profile bound
to exactly one site — and the lifecycle machinery that makes owning such a thing
defensible: a two-layer lease, a pinned Public Suffix List, a version guard, a
deletion path, and a structural prohibition on ever reading the credential
material inside.

> **S1 provides persistent profile infrastructure. There is still no login flow
> and no authenticated agent reading.**

| | |
|---|---|
| Starting SHA | `01e66ee2d49b9faa9271e7044a01878082b70fdc` (M8-0 / S0 complete) |
| Architecture review | `docs/plans/milestone-8.md` §§7, 17, 23, 26, 27 |
| Final SHA (implementation) | `24fd1dd7976c7ae8c2ae785a5f008c32fee3fc37` |
| Final SHA (documentation) | `865f53c262049b65fe41789e93d3441e52721dc3`, then this correction commit recording that SHA |
| Migration | `0005` → `0006` |

**Commit structure.** Implementation and packaging are one commit rather than
two. The suggested split was conditional on their being separable, and they are
not: bundling full Chromium without `channel: "chromium"` produces a build whose
worker cannot launch a browser, and adding the channel without the packaging
change leaves the bundle carrying a binary nothing uses. Splitting them would
have created a commit that does not build correctly, which is worse than a
slightly larger one.

---

## 1. What was built

```text
one Lumi-managed browser profile
        ↓
one registrable domain (eTLD+1, from a pinned PSL snapshot)
        ↓
a persistent Chromium user-data directory under %LOCALAPPDATA%
        ↓
cookies and storage stay inside Chromium's profile
        ↓
the profile survives a Chromium restart and a worker-process restart
        ↓
Lumi never reads credential material out of it
```

Four operations exist and no others:

```text
createBrowserProfile(site, label)      POST   /browser-profiles
listBrowserProfiles()                  GET    /browser-profiles
openBrowserProfile(profileId)          POST   /browser-profiles/{id}/open
deleteBrowserProfile(profileId, rev)   POST   /browser-profiles/{id}/delete
```

(plus `GET /browser-profiles/{id}` and `POST /browser-profiles/{id}/close`).

There is no `manageProfile(action, json)`, no operation that re-points a profile
at another site, and no operation that reads anything out of one.

---

## 2. Migration `0006`

One forward-only Alembic revision, head `0005` → `0006`. **Exactly one new
table and no change to anything that existed.**

| Change | Kind |
|---|---|
| `browser_profiles` | new table: `id`, `label`, `site`, `allowed_origins`, `status`, `revision`, `chromium_build`, `playwright_version`, `app_version`, `lease_runtime_generation`, `lease_expires_at`, `revoke_epoch`, `account_fingerprint`, `account_label_hash`, `last_login_completed_at`, `last_observed_at`, `created_at`, `updated_at`, `deleted_at` |
| `uq_browser_profiles_site_live` | partial unique index on `site` where `status <> 'DELETED'` |
| `browser_profiles_immutable_binding` | trigger: refuses any change to `id`, `site`, `allowed_origins` or `created_at`; refuses to revive a `DELETED` row; refuses a decreasing `revoke_epoch` |

**Untouched, deliberately:** `task_grants.kind` still admits only
`public_research`; `step_authorizations` is unchanged; no column was added to
`action_attempts`, `browser_dispatches` or `research_sessions`; `action_status`
and `task_status` are unchanged. **No M7b grant or authorization semantics
changed merely because profiles now exist.**

No column exists anywhere for a cookie, a token, a storage blob, a
`storageState` document or the profile directory path.

Status vocabulary is `NEW`, `NEEDS_LOGIN`, `AUTHENTICATED`, `DELETED`.
`AUTHENTICATED` is in the schema for S2 and **no S1 code path assigns it** —
asserted by `test_opening_never_makes_a_profile_authenticated`, which opens and
closes a profile three times and checks the status stays `NEEDS_LOGIN` with
`last_login_completed_at` still null.

The metadata-parity test that pins the SQLAlchemy tables to the migration
(`test_migrations_match_table_definitions`) passes with no diff.

---

## 3. Profile directory design

```text
%LOCALAPPDATA%\Lumi\browser-profiles\<profile-uuid>\
```

* **`%LOCALAPPDATA%`, never `%APPDATA%`** — a roaming profile is synchronised to
  a domain share, and a directory holding live session cookies must not travel.
* **Outside the install directory, the repository and `dist/`** — an all-users
  install under `Program Files` is not writable, and an upgrade or uninstall
  would capture or destroy the profile.
* **The directory name is the profile UUID and nothing else.** No site and no
  label appears anywhere in the path, so a directory listing — by a backup tool,
  a sync client, a screen share or another person at the machine — does not
  disclose which accounts the user holds. The site association is a database row.
* **Derived, never transported.** `app/browser/profile_paths.py` is the only
  thing that computes a profile path, and the two trusted processes that need it
  — the runtime (to delete) and the worker (to open) — each derive it from the
  id. It is not a field in any API response, worker request, log record,
  diagnostic or model prompt.

`LUMI_BROWSER_PROFILE_ROOT` may move the **base** directory, and is read from
the process environment only — never from a request, a model or a page. It takes
a base, never a profile path: the UUID is always appended by Lumi. The test
suite points it at a temporary directory so a test run never writes into the
developer's real `%LOCALAPPDATA%`.

Resolution fails closed rather than inventing a fallback
(`localappdata_not_set`, `localappdata_not_absolute`,
`profile_root_not_absolute`). It is resolved **lazily** in the runtime, so a
machine without `%LOCALAPPDATA%` fails profile operations rather than refusing to
start the runtime at all.

---

## 4. Windows permission behaviour

On creation, `restrict_to_current_user` runs `icacls <dir> /inheritance:r
/grant:r <USER>:(OI)(CI)F` with a fixed argument vector and no shell: inherited
entries are dropped and the current user is granted explicitly. It is
best-effort — a failure is logged (by code, never by path) and profile creation
continues, because `%LOCALAPPDATA%` is already user-scoped and refusing to
create a profile over an ACL edit would be the worse trade.

`test_a_new_profile_directory_stops_inheriting_access` asserts on Windows that
the created directory shows no `(I)` inherited entries and names the current
user.

**What this is not, stated plainly and in `SECURITY.md`:**

* the directory is **user-scoped**, and that is the entire claim;
* Chromium encrypts `Cookies` and `Login Data` with a DPAPI key bound to **the
  same user**, so **any process running as that user can decrypt the profile**;
* same-user malware and same-user processes are **outside Lumi's security
  boundary**, and no ACL changes that;
* no encryption was invented beyond what Chromium and Windows already provide.

Treat the profile directory as secret material, because that is what it is.

---

## 5. Public Suffix List

| | |
|---|---|
| Source | `https://publicsuffix.org/list/public_suffix_list.dat` |
| Bundled at | `services/agent/app/data/public_suffix_list.dat` |
| Version | `2026-09-17_19-09-53_UTC` |
| Upstream commit | `329834e40a8543f3ef6265db543b7af6c4185571` |
| SHA-256 (LF-normalised) | `82c08c93231a51649b061ea3541884b01716344c3f2edec07272a0a8b9849b1c` |
| Size | 334,111 bytes, 16,476 lines |
| Rules parsed | 10,028 ordinary + 288 wildcard + 8 exception = **10,324** (6,950 ICANN) |

**Pinned, never fetched.** The digest is asserted in code on load; a substituted
or corrupted snapshot raises `public_suffix_list_digest_mismatch` rather than
silently moving a site boundary. There is no code path that downloads, updates
or replaces it — `test_the_list_is_bundled_and_never_fetched` asserts the loader
contains no `httpx`, `requests`, `urlopen`, `urlretrieve` or `socket`.

The digest is taken over the file with line endings normalised to LF, so it
depends on the file's content and not on how a checkout wrote it to disk.
`.gitattributes` additionally pins the file to `eol=lf`.

**Both sections are used, ICANN and private**, because browsers apply the whole
list to cookie scope. `foo.github.io` and `bar.github.io` cannot set cookies for
one another, so they are different sites and must be different Lumi profiles.
ICANN-only rules would let one profile span every GitHub Pages project.

**459 of the snapshot's rules are written in Unicode** (`公司.cn`). They are
converted to A-labels once at load; an unconvertible rule raises rather than
being dropped, because dropping one would weaken the boundary quietly. Lumi
stores and compares sites as A-labels.

The published algorithm is implemented in full, including exception rules
winning over wildcards and the implicit `*` fallback. The upstream test vector
file is committed at `services/agent/tests/public_suffix_vectors.txt` and
**all 77 vectors pass** (68 ASCII directly, 9 Unicode compared as A-labels).

Tricky cases asserted explicitly:

| Input | Registrable domain |
|---|---|
| `github.com`, `sub.github.com`, `a.b.c.github.com` | `github.com` |
| `example.co.uk`, `www.example.co.uk` | `example.co.uk` |
| `foo.github.io`, `bar.foo.github.io` | `foo.github.io` |
| `www.ck` (`*.ck` + `!www.ck`) | `www.ck` |
| `city.kobe.jp`, `www.city.kobe.jp` | `city.kobe.jp` |
| `b.c.kobe.jp`, `a.b.c.kobe.jp` | `b.c.kobe.jp` |
| `kobe.jp` (a registration under `jp`, despite `*.kobe.jp`) | `kobe.jp` |
| `my-bucket.s3.amazonaws.com` | itself (private suffix) |
| `example.example` (unlisted TLD, implicit `*`) | `example.example` |

Refused, with no registrable domain: `com`, `co.uk`, `github.io`, `c.kobe.jp`,
`s3.amazonaws.com`, `instance.compute.amazonaws.com`, `localhost`, `127.0.0.1`,
`0x7f000001`, `2130706433`, `192.168.1.1`, and anything carrying a port, path,
scheme, credential, space, underscore or non-ASCII label.

**Residual:** the snapshot ages. A suffix delegated after the pinned version is
classified by the old rules until the next Lumi release. Recorded in
`SECURITY.md` and in plan §24.13.

---

## 6. One profile, one site

`canonical_site()` takes what trusted configuration supplied — a host or a URL —
strips scheme, path, query and fragment, and resolves it through the PSL to a
registrable domain **before a row exists**. `https://Sub.GitHub.com/x`,
`github.com.` and `GITHUB.COM` all become `github.com`.

The binding is then immutable, at three levels:

1. **No API.** There is no `changeProfileSite`, no `PATCH`, and
   `CreateBrowserProfileBody` is the only place a site is ever accepted.
2. **No migration path.** Nothing reassigns an existing profile.
3. **A database trigger.** `browser_profiles_immutable_binding` refuses any
   `UPDATE` that changes `site` or `allowed_origins`.

`test_a_profile_cannot_be_rebound_to_another_site` reaches *past* the service
with direct `UPDATE` statements, because the guarantee has to hold against a
future code path nobody has written yet. Both are refused.

A partial unique index enforces **one live profile per site**; deleting one
frees the site again, which is what makes "delete this profile" a complete
action rather than a dead end.

`allowed_origins` is derived at creation — `https://<site>` and
`https://www.<site>`, `https:` only — and frozen with the site. S1 stores it and
navigates nowhere.

---

## 7. The lease: two layers, and why both

### Layer 1 — the authoritative database lease

`browser_profiles.lease_runtime_generation` and `lease_expires_at`, taken by a
conditional `UPDATE` in the existing compare-and-swap idiom
(`BrowserProfileRepository.acquire_lease`). The conditions under which a lease
may be taken are *written into the statement*, not checked by an earlier
`SELECT`, so two runtime generations racing for one profile cannot both win:
exactly one `UPDATE` matches a row and the other gets zero rows back.

A lease may be taken when it is free, ours (a renewal), or expired. Release is
generation-bound, so a late release from a reclaimed-from process cannot unlock
a profile somebody else now owns.

### Layer 2 — the OS-level backstop

An exclusive file handle on `.lumi-profile-lock`, held **inside** the profile
directory by whichever process has the profile open
(`app/browser/profile_lock.py`; `msvcrt.locking(LK_NBLCK)` on Windows,
`fcntl.flock(LOCK_EX|LOCK_NB)` elsewhere). It never blocks, never retries and
never breaks another holder's lock.

It exists because the two layers fail differently. The database lease is only as
good as the database: **two Lumi installations pointed at different databases
would both believe they held it.** The OS handle knows nothing about generations
or expiry, but it is held by a live process on the machine.

`test_two_worker_processes_cannot_hold_one_profile` proves exactly that case
with two real worker subprocesses and no shared database at all: the second gets
`409 profile_locked_by_another_process`.

**Chromium's own `SingletonLock` is neither relied on nor deleted.** Its failure
modes are confusing and it may do something other than refuse; deleting one
behind the browser's back is the hand-editing of browser internals this design
avoids. `test_the_profile_lock_is_lumis_own_file_and_chromiums_is_untouched`
asserts the lock module contains no `Singleton`, no `unlink`, no `rmtree`, no
`os.remove` and no `shutil`.

### Ordering

Opening is: **DB lease → OS handle → launch Chromium.** If the worker refuses,
the runtime releases the lease, so a half-held profile is never left behind
(`test_a_worker_refusal_does_not_leave_a_lease_behind`).

---

## 8. Stale-lease recovery

Deterministic, and it fails closed:

```text
foreign lease + OS handle free   ->  reclaim
foreign lease + OS handle held   ->  refuse  (profile_locked_by_another_process)
```

A lease naming another runtime generation describes a process that cannot be
alive — one runtime holds a PostgreSQL advisory lock for its whole life — but
"cannot be alive in *this* database" is not "nothing is using the directory".
So a reclaim is attempted **only** when the OS handle is also free. Opening one
Chromium profile directory twice corrupts it, and that is not a risk worth a
heuristic.

At startup, `release_stale_leases()` clears every lease naming a different
generation, so a crashed generation does not lock its profiles out for the
length of a TTL. The OS handle still refuses the open if something genuinely
holds the directory.

Default lease TTL: 300 s (`LUMI_BROWSER_PROFILE_LEASE_TTL_SECONDS`, 30–3600).

---

## 9. Chromium version guard

`chromium_build`, `playwright_version` and `app_version` are recorded on the row
after a successful open.

| Case | Behaviour |
|---|---|
| Same build | opens |
| Newer build than recorded | opens, and the metadata progresses to what actually opened it |
| **Older build than recorded** | **refused** with `profile_browser_downgrade_refused`, **before the directory is touched**. Nothing is deleted, nothing is repaired. |

A Chromium profile is forward-compatible but not backward-compatible; opening a
directory written by a newer build risks corrupting something the user signed
into. The user can create a new profile and sign in again.

Tested both in-process
(`test_an_older_chromium_refuses_and_does_not_delete_the_profile`, which then
proves the session is *still intact* by reopening with a permitted build and
finding the fixture still says "signed in") and over the real worker HTTP
contract (`test_a_worker_refuses_a_chromium_downgrade_and_keeps_the_profile`).
`test_a_refused_downgrade_never_creates_a_directory` shows the guard runs first,
so a refusal leaves no trace on disk at all.

---

## 10. Crash and session-restore behaviour

Persistent contexts launch with `--hide-crash-restore-bubble` and
`--disable-session-crashed-bubble` (plus `--no-first-run` and
`--no-default-browser-check`). Session restore is never enabled — Lumi never
writes `restore_on_startup` — so Chromium has nothing to restore; those flags
remove the visible artefact a headed S2 window would otherwise show.

After an unclean shutdown:

* the profile directory remains;
* login and session cookies may remain, because they are the browser's;
* **tabs are not restored**;
* the OS handle is released by the operating system with the process, so the
  next worker can take it — and until the process dies, nobody else can
  (`test_killing_the_owner_frees_the_profile_rather_than_stranding_it`).

**S1 does not implement authenticated task continuation and claims none.** There
is no authenticated task to continue, because there is no authenticated reading.

---

## 11. Deletion semantics

`delete_profile(profileId, expectedRevision?)`:

1. close this runtime's own context if it holds one;
2. `mark_deleted` — a conditional `UPDATE` that **refuses while any live lease
   exists**, so deletion cannot win a race against an owner;
3. take the exclusive OS handle;
4. remove every child of the directory except the lock file, while still holding
   it (Windows will not unlink an open file, and releasing first would open a
   window in which a second installation could take the profile);
5. release the handle, unlink the lock file, remove the now-empty directory;
6. the row stays, `DELETED`, terminal, with `deleted_at` set.

The row is kept because later tables will reference a profile with
`ondelete=RESTRICT` and a deleted profile's history has to remain readable. The
site becomes available for a new profile.

Deleting a deleted profile is **idempotent** by contract: it returns the same
terminal row and writes nothing (`test_deleting_is_idempotent` asserts the
revision does not move).

A partial removal is never reported as success: if the directory still exists
afterwards, a warning is logged with error *codes* and no path.

**No secure forensic erasure is claimed.** Files are unlinked. Nothing here
defeats a journalling filesystem, an SSD's wear levelling, a shadow copy or a
backup.

### No programmatic logout

Deletion is **local**. There is no logout automation, no cookie clearing, no
server-side logout request and no "sign out everywhere".
`test_deleting_makes_no_network_request_at_all` monkeypatches
`socket.socket.connect`, `socket.create_connection`, `httpx.Client.request` and
`httpx.AsyncClient.request` to fail the test, then deletes a profile
successfully. `test_the_deletion_path_holds_no_http_client_and_no_logout` scans
the module's code (docstrings stripped) for `httpx`, `requests`, `urlopen`,
`logout`, `sign_out` and `signout`.

The trusted wording S2 must carry:

> **This removes the sign-in data stored by Lumi on this computer. It does not
> sign you out on the website.**

That sentence is true precisely because the deletion path has no network client
in it. **S1 exposes the backend contract only; the UI is deferred to S2.**

---

## 12. Credential-extraction prohibition

> Lumi may operate a persistent Chromium profile, but Lumi must never extract or
> serialise the authentication material inside that profile.

| Lumi owns | The browser owns |
|---|---|
| profile id, label, site, allowed origins | every cookie, of any kind |
| status, revision, browser/app versions | access, refresh, bearer, CSRF tokens |
| the lease and its expiry | `localStorage`, `sessionStorage`, IndexedDB |
| `revoke_epoch`, fingerprint *hashes* | Chromium's `Login Data`, `Cookies`, `Web Data` |
| the directory path, derived and never transported | anything a model could replay |

`tests/test_no_credential_extraction.py` enforces it over `app/`, `evals/` and
`tests/` — Lumi's own code, never `site-packages`, because Playwright of course
defines these methods; the boundary is that Lumi never calls them. Three scans:

1. **Call-shaped patterns**, matched against source with docstrings, comments
   *and string literals* removed, so a module may explain the rule it enforces:
   `storage_state(`, `storageState`, `.cookies(`, `add_cookies(`,
   `clear_cookies(`.
2. **Page script that reaches storage**, matched with literals kept because in
   Python such a script is always a literal: `document.cookie`, `localStorage`,
   `sessionStorage`, `indexedDB`. The fixture *sites* under `evals/sites/` are
   exempt from this one and only this one — a fixture page setting its own
   `localStorage` is a website doing what websites do, and is what the
   persistence proof needs.
3. **Chromium's own profile filenames**, literals kept: `Login Data`,
   `Web Data`, `Local State`, `Network/Cookies`, `SingletonLock`,
   `SingletonCookie`, `Local Storage/leveldb`.

The scanner is itself tested: `test_the_scanner_catches_a_planted_violation`
plants each shape a well-meaning future change would take and requires the
patterns to catch them, and `test_prose_explaining_the_rule_is_not_a_violation`
requires a docstring naming all of them to pass.

**No code path in S1 reads, copies, exports or imports any profile content.**

---

## 13. API and boundary changes

### Runtime HTTP (internal)

| Route | Body |
|---|---|
| `GET /browser-profiles` | — |
| `POST /browser-profiles` | `{site, label}` |
| `GET /browser-profiles/{id}` | — |
| `POST /browser-profiles/{id}/open` | — |
| `POST /browser-profiles/{id}/close` | — |
| `POST /browser-profiles/{id}/delete` | `{expected_revision?}` |

Fixed routes with fixed contracts, in the repository's existing style.
**No route accepts `profilePath`, `userDataDir`, `cookieFile`, `storageState`
or `browserExecutablePath`** — every body is `extra="forbid"`, so such a field
is a `422`, asserted for all five names.

`BrowserProfileResponse` is the boundary: id, label, site, allowed origins,
status, revision, browser/Playwright/app versions, `leased` (a boolean — the
generation id itself stays internal), `lease_expires_at`, `revoke_epoch`,
fingerprint hashes (or null), timestamps. No path, no cookie, no token, no raw
account identity.

### Runtime → worker

Two new endpoints, `POST /v1/profiles/open` and `POST /v1/profiles/close`,
carrying `ProfileSessionRequest`: a profile id, the two generations, and the
Chromium build the database recorded. **No path, no executable path, no
`storageState`, no cookie file, no site scope** — the worker derives the
directory from the id with its own configured base, so the runtime cannot name a
path and neither can anything upstream of it.

`ProfileSessionResponse` returns the id, the worker generation, `OPEN`/`CLOSED`/
`NOT_FOUND`, the Chromium build and Playwright version that opened it, and
whether the worker holds the OS handle.

### Electron main and renderer

**Unchanged.** S1 adds no IPC channel, no preload method, no renderer surface
and no entry to `src/shared/agent-runtime-contract.json`. Electron main does not
know that profiles exist, which is the strongest available form of "no profile
path crosses the renderer/main boundary". S2 owns the trusted UI.

### Refusal codes

`ProfileRefusal` is handled as `409 browser_profile_refused` with a stable
machine `reason` and never a path: `site_*` (from the PSL),
`profile_site_already_bound`, `profile_not_found`, `profile_deleted`,
`profile_kind_mismatch`, `profile_lease_unavailable`,
`profile_locked_by_another_process`, `profile_browser_downgrade_refused`,
`profile_session_limit`, `profile_directory_unavailable`, `profile_open_failed`,
`profile_delete_refused`, `label_*`.

`browser_profile_refused` is declared in `app/api/contract.py` and therefore in
`src/shared/agent-runtime-contract.json`, because the runtime can emit it. That
is the **only** change to the shared contract: no new schema, no new enum and no
new example, because no Electron code path reaches a profile route yet.

### One fix to existing code

`BrowserRepository.register_worker_generation` became an `ON CONFLICT DO
NOTHING` insert. It is not new behaviour, it is the removal of a pre-existing
race that S1 makes marginally more reachable; the reasoning and the evidence are
in §21.

---

## 14. Research / authenticated-profile separation

`BrowserContextKind` makes the split explicit:

```text
RESEARCH_SESSION          M7b: task-owned, unauthenticated, disposable, no user_data_dir
AUTHENTICATED_PROFILE     M8a: persistent, Lumi-managed, bound to one site
```

`open_profile` refuses a `RESEARCH_SESSION` intent with
`profile_kind_mismatch` and does not take a lease on the way out. The research
path has no route to a profile at all: it opens `/v1/sessions/open`, which
creates a context with no `user_data_dir`.
`test_the_research_service_has_no_route_to_a_persistent_profile` asserts that
`app/services/research_tasks.py` and `app/browser/research_session.py` contain
no `launch_persistent_context`, no `user_data_dir`, no `browser_profile` and no
`profile_paths`.

In the worker the two live in **separate stores** (`SessionStore` and
`ProfileSessionStore`) reached by separate endpoints, so one cannot be
substituted for the other by passing the wrong id.

**S1 adds no authenticated planner operation.** The registry is unchanged.

---

## 15. Egress-broker integration

Every persistent context is launched with `persistent_launch_options`, which is
built **from** S0's `managed_launch_options` rather than beside it, so a future
change to the broker's launch contract cannot silently miss this path:

* `proxy.server` — the broker;
* `proxy.username` / `proxy.password` — the per-launch credential;
* `proxy.bypass = "<-loopback>"` — the single most load-bearing string in S0;
* `--disable-quic`, `--disable-background-networking`;
* plus `--hide-crash-restore-bubble`, `--disable-session-crashed-bubble`,
  `--no-first-run`, `--no-default-browser-check`;
* plus `service_workers="block"`, `accept_downloads=False`, `permissions=[]`.

There is **no unbrokered persistent context** and no loopback bypass: the S1
fixture reaches the browser through the broker via the existing reviewed
configured-origin path.

Asserted by `test_the_launch_options_are_exactly_the_reviewed_ones` (literally,
field by field, including that no `storage_state`, `storageState`,
`executable_path` or `user_data_dir` appears in the options),
`test_a_persistent_profile_is_launched_through_the_s0_broker` (the broker's dial
counter moves),
`test_loopback_cannot_bypass_the_broker` (an unconfigured loopback origin is
refused and **records no request at its own end**), and
`test_the_profile_fails_closed_when_the_broker_dies` (no direct-connection
fallback).

**Service workers stay blocked.** They were not relaxed because persistent
profiles support them; the S0/plan position on request interception and
Background Sync is unchanged.

**Downloads and permissions.** `accept_downloads=False` and `permissions=[]`.
`test_the_context_grants_no_permissions_and_no_downloads` queries geolocation,
notifications, camera, microphone and clipboard and requires each to be
`denied`, `prompt` or unsupported — never granted.

**Zero model and provider involvement.** No planner call, no provider call and
no model of any kind is reachable from profile creation, open, lease, delete or
version checking. No OpenAI, Gemini or DeepSeek code is imported by any of it.

---

## 16. Full Chromium packaging

**The bundle now carries full Chromium and not the headless shell.**

`scripts/build-agent-runtime.mjs` installs with `playwright install --no-shell
chromium`, which yields `chromium-<rev>`, `ffmpeg` and `winldd` and **not**
`chromium_headless_shell-<rev>`. One browser, one revision, one version matrix,
run headless for M7a/M7b and available headed for S2.

Two build assertions and one launch-side change make that hold:

* the build **fails** if Playwright's own listing names a headless shell, and
  **fails** again if one reaches the bundle directory;
* the build requires a `chromium-<digits>` directory containing
  `chrome-win64\chrome.exe`;
* `managed_launch_options` now passes **`channel: "chromium"`**.

That last one is not cosmetic and was found by measurement, not assumption.
With only full Chromium present, `launch(headless=True)` **fails**:

```text
BrowserType.launch: Executable doesn't exist at
  ...\chromium_headless_shell-1243\chrome-headless-shell-win64\chrome-headless-shell.exe
```

and with `channel="chromium"` it launches `chrome.exe` in Chromium's own new
headless mode. Setting it centrally means development and the packaged build
launch the *same binary*, so a cached headless shell on a developer machine
cannot hide a packaging break.

The build then **launches the bundled `chrome.exe` once**, with nothing but the
bundle on `PLAYWRIGHT_BROWSERS_PATH` and a scrubbed environment, and records the
version it printed. Browser discovery is therefore deterministic and proven from
the bundle alone, with no developer cache to fall back on.

### Two measured behaviour differences from the headless shell

Switching binaries changed two observable behaviours. Both were measured, and
both existing S0 browser tests were updated to state the guarantee rather than
one browser's reporting style:

1. **A proxy refusal on a top-level navigation** arrives as
   `net::ERR_HTTP_RESPONSE_CODE_FAILURE` under full Chromium, where the headless
   shell handed the page a readable `403` with the `x-lumi-refusal` header. The
   boundary is unchanged — the refused destination records **no connection** —
   so the tests now accept either shape and assert on the other end of the wire,
   which was always the real evidence.
2. **An idle keep-alive connection to the proxy lingers for about five seconds**
   after the last response, where the shell closed it immediately. Measured:
   `active_connections` stayed at 1 for ~5 s and then fell to 0. This matters
   for M8b: a freeze entry must drive `active_connections` to zero before it can
   claim nothing was in flight, which is exactly what `freeze()`'s return value
   is for. The S0 freeze test now waits for quiescence first — which is what the
   production caller will have to do — rather than assuming the page finishing
   is the same instant as the socket closing.

Full Chromium also issues one extra request per page (a favicon probe) that the
shell did not.

### Profile directories can never enter an artifact

Impossible by construction (profiles live under `%LOCALAPPDATA%`), asserted
anyway: the build walks `dist/agent-runtime` and **fails if any
`browser-profiles` directory is found**. `browser-profiles/` is in `.gitignore`
so a developer who relocates the base into the checkout cannot commit one, and
`tests/test_profile_paths.py` asserts the resolved base is under
`%LOCALAPPDATA%` and inside none of the repository, `services/agent`, `dist`,
`dist/agent-runtime`, `release`, `out`, `Program Files` or `Program Files (x86)`.

---

## 17. Exact size measurements

Measured by summing file sizes (not block allocation), so the before and after
numbers are comparable.

| Component | Before (headless shell) | After (full Chromium) | Change |
|---|---|---|---|
| `chromium_headless_shell-1243` | 270.1 MB | — | −270.1 MB |
| `chromium-1243` | — | 431.9 MB | +431.9 MB |
| `ffmpeg-1011` | 3.4 MB | 3.4 MB | — |
| `winldd-1007` | 0.2 MB | 0.2 MB | — |
| **`ms-playwright/` total** | **273.7 MB** | **435.5 MB** | **+161.8 MB** |
| `python/` | 215.9 MB | 215.9 MB | — |
| `agent/` | 2.4 MB | 2.4 MB | — |
| **`dist/agent-runtime` total** | **492.0 MB** | **653.8 MB** | **+161.8 MB** |

| Installer | Size |
|---|---|
| Before (`Lumi Setup 0.1.0.exe`, headless shell) | 275,390,230 bytes = **262.6 MB** |
| After (`Lumi Setup 0.1.0.exe`, full Chromium) | 338,281,420 bytes = **322.6 MB** |
| Change | **+62,891,190 bytes = +60.0 MB** (the installer is LZMA-compressed, so it grows by far less than the 161.8 MB of uncompressed payload) |

The plan's estimate was "roughly +150–200 MB"; the measured runtime-bundle
change is **+161.8 MB**, inside that range, and the measured **installer**
change is **+60.0 MB**. The installer figure comes from an actual
`npm run package` run on this machine, not from arithmetic on the bundle.
`release/0.1.0/win-unpacked` is 1,120 MB on disk.

### A correction to the plan's signing assumption

Plan §23 says "Chromium's binaries are Google-signed, so unlike `greenlet`'s
unsigned `.pyd` they should not trip Smart App Control — verify, do not assume."
**Verified, and the assumption is wrong.** Playwright ships *Chrome for Testing*
builds from its own CDN, and they are not Authenticode-signed:

| Binary | `Get-AuthenticodeSignature` |
|---|---|
| `chromium-1243\chrome-win64\chrome.exe` (new) | `NotSigned` |
| `chromium_headless_shell-1243\chrome-headless-shell.exe` (old) | `NotSigned` |
| `release\0.1.0\win-unpacked\Lumi.exe` | `NotSigned` |
| `release\0.1.0\Lumi Setup 0.1.0.exe` | `NotSigned` |

This is **not a regression introduced by S1** — the headless shell was unsigned
too — but it is a larger unsigned binary that S2 will run with a visible window,
and it strengthens rather than weakens the signing gate in §23 of this report.
electron-builder logs "signing with signtool.exe" during packaging; with no
certificate configured that step is a no-op, as the table above shows.

---

## 18. Packaged-runtime manifest

`manifest.json` moves from `version: 1` to `version: 2` and gains browser and
PSL identity plus measured sizes. From the S1 build:

```json
{
  "version": 2,
  "python": "3.12.14",
  "playwright": "1.63.0",
  "browsers": "chromium",
  "browser": {
    "kind": "chromium",
    "revision": "chromium-1243",
    "components": ["chromium-1243", "ffmpeg-1011", "winldd-1007"],
    "version": "153.0.8010.12"
  },
  "publicSuffixList": {
    "version": "2026-09-17_19-09-53_UTC",
    "commit": "329834e40a8543f3ef6265db543b7af6c4185571",
    "digest": "82c08c93231a51649b061ea3541884b01716344c3f2edec07272a0a8b9849b1c",
    "source": "https://publicsuffix.org/list/public_suffix_list.dat",
    "rules": 10324
  },
  "sizeMegabytes": { "total": 653.8, "python": 215.9, "agent": 2.4, "browser": 435.5 },
  "lockDigest": "…",
  "agentDigest": "…"
}
```

**No profile id and no path**, asserted by
`src/main/agent/packaged-browser.test.ts`, which also checks the real manifest
when a build exists and requires `browser.components` to contain no
`headless_shell`.

---

## 19. The deterministic persistence proof

Two proofs, at two levels, and the report is explicit about what each one shows.

### A. Across a Chromium restart (in-process)

`test_a_session_survives_a_browser_restart`:

```text
/session/start  →  the fixture server issues an HttpOnly session cookie
                   (page script cannot read it either)
close the persistent context, close Chromium
open a new ProfileSessionStore → a new Chromium process, same directory
/account        →  the fixture server says "signed in as Fixture Account"
```

### B. Across a hard-killed worker **process** (subprocess)

`test_a_session_survives_a_worker_process_restart`:

```text
worker process A opens the profile     (real subprocess, holds the OS handle)
worker process A closes it
the test's browser signs in            (fixture issues the cookie)
worker process A is killed outright    (no signal handler, no cleanup)
worker process B opens the profile     (new process, new Chromium, same dir)
worker process B closes it
the test's browser reads /account      → "signed in"
```

**The proof is the server's behaviour, never an export.** Lumi does not call
`storage_state()`, does not call `context.cookies()`, and does not open any file
Chromium wrote. The cookie is `HttpOnly`, so the only thing that can present it
on the second run is the browser itself, carrying state it kept in the profile
directory. The fixture keeps its session table in its own process and stays up
across every restart, so if the profile had *not* persisted, `/account` would
render "signed out" — which
`test_a_different_profile_does_not_inherit_the_session` and
`test_nothing_persists_for_a_directory_that_was_deleted` both demonstrate it
does.

`test_web_storage_survives_a_browser_restart` does the same for `localStorage`,
again observed by the fixture page rather than by Lumi.

**Documented difference from the brief.** The sign-in step in proof B is driven
by the test process using the worker's own `ProfileSessionStore`, not by the
worker over its HTTP contract, because **S1 deliberately has no navigation
operation for a persistent profile** — authenticated page reading is S2/S3.
Nothing in the worker can be asked to visit a page with a profile. The write and
the read are still separated by a hard-killed worker process and a fresh one
that launched Chromium on the same directory, so the persistence claim is
unaffected; only who drove the page differs. A fully worker-driven proof becomes
possible in S2 and should be added there.

**Runtime restart.** Covered at the level the architecture permits: profile rows
and leases are in PostgreSQL and survive trivially, and startup lease
reclamation is tested (`test_startup_clears_leases_left_by_generations_that_are_gone`).
A runtime restart kills the worker, so its browser-level effect is exactly proof
B, which is the stronger test.

---

## 20. Exact test results

All Python figures are from `services/agent` with a real PostgreSQL.

| Suite | Result |
|---|---|
| `tests/test_public_suffix.py` | **46 passed** |
| `tests/test_profile_paths.py` | **9 passed** |
| `tests/test_no_credential_extraction.py` | **15 passed** |
| `tests/test_browser_profiles.py` | **40 passed** |
| `tests/test_browser_profile_session.py` (browser) | **17 passed** |
| `tests/test_browser_profile_worker.py` (browser, hardkill) | **5 passed** |
| `tests/test_egress_broker.py` | **48 passed** |
| `tests/test_egress_broker_browser.py` (browser) | **12 passed** |
| `tests/test_schema.py` | **9 passed** |
| `uv run mypy` | **Success: no issues found in 143 source files** |
| Full `uv run pytest` | **959 passed, 1 failed, 16 skipped** in 18 m 35 s. The one failure is the `.env`-dependent baseline in section 21. |
| `npm.cmd run typecheck` | **clean** |
| `npm.cmd test` | **1966 passed, 1 failed, 22 skipped** (112 files: 105 passed, 3 failed, 4 skipped). Every failure is a known baseline one — see section 21. |
| `npm.cmd run build` | **clean** (main, preload and renderer bundles) |
| `npm.cmd run eval` | **118/118 eval cases passed** |
| `npm.cmd run package` | **succeeded**, producing `release/0.1.0/win-unpacked` and `Lumi Setup 0.1.0.exe` with full Chromium and no headless shell |
| Electron acceptance (`LUMI_ELECTRON_E2E=1`) | **2 passed** |
| Packaged acceptance (`LUMI_PACKAGED_E2E=1`) | **1 passed**, against this build's own unsubstituted `Lumi.exe` |

Every new external/process test is bounded and cleans up:
`test_browser_profile_worker.py` kills each worker subprocess in a `finally`,
with a 120 s readiness bound and a 60 s kill bound; browser-level opens use a
30 s operation timeout.

---

## 21. Known baseline failures

Four tests fail on this machine before and after S1, for reasons that have
nothing to do with this slice. Each was checked, not assumed.

| Test | Cause | Checked how |
|---|---|---|
| `src/renderer/src/accessibility.test.tsx` — *keeps the level legible without colour* | A stale CSS-string assertion against the scam card's stylesheet. | The same assertion fails at the same line in the repository's own `baseline-vitest.log`. |
| `src/main/vision/real-inference.test.ts` | `ENOENT … vision-models/clip-vit-base-patch32-q8/vocab.json` — the local vision model pack is not installed on this machine. | Run in isolation; the error is a missing model file, and nothing in S1 touches `src/main/vision`. |
| `src/main/vision/tokenizer-pack.test.ts` | The same missing model pack (`model_load_failed`). | As above. |
| `services/agent/tests/test_booking_preparation.py::test_booking_routes_without_a_worker_answer_503` | This developer's `services/agent/.env` sets `LUMI_PUBLIC_INSPECTION_HOSTS=github.com,example.com`, so `_worker_source` always builds a `ManagedBrowserWorker` and `browser_worker_not_configured` is unreachable; the route answers `browser_worker_unavailable` instead. | Commenting that one line out makes the test pass; restoring it makes it fail again. `_worker_source`'s condition is unchanged by S1 (the diff adds only two keyword arguments). |

### One pre-existing flake found and fixed

`tests/test_page_inspection_ledger.py::test_a_stale_revision_and_a_concurrent_second_execution_read_nothing_extra`
failed roughly one run in three with `500` instead of `409`. The captured
traceback showed the cause in **Milestone 7a code**, not S1's:
`BrowserExecutionService._bind_worker` reads `get_worker_generation(...)` and
then inserts, so two concurrent executions both saw "not registered" and both
inserted, and the loser got
`UniqueViolationError … "pk_browser_worker_generations"`.

S1 makes that path slightly more reachable, because opening a profile binds the
worker too, so it is fixed here rather than left: `register_worker_generation`
is now an `ON CONFLICT DO NOTHING` insert that reads the existing row back on a
conflict. Registering a generation is idempotent by nature — the row is the
worker's identity, not a claim on anything. Six consecutive runs of the
previously flaky test now pass.

## 22. Residual limitations

1. **A persistent profile is an impersonation artefact, and same-user isolation
   does not exist.** Any process running as the user can decrypt it, because
   Chromium's DPAPI key is bound to that user. This is the single largest new
   risk M8a introduces and no part of this design removes it.
2. **Unsigned distribution plus real session data** is materially worse than
   unsigned plus a disposable context. See §24.
3. **The bundled PSL snapshot ages.** A newly delegated suffix is misclassified
   until the next release.
4. **Deletion is ordinary unlinking**, not forensic erasure. Shadow copies,
   backups, SSD wear levelling and journalling filesystems are untouched.
5. **The OS handle binds only processes that ask.** It is an advisory lock; the
   only processes that open that file are Lumi's, which is the point, but a
   determined unrelated program could open the directory anyway.
6. **A compromised worker bypasses everything**, including the broker. Process
   separation is not privilege isolation.
7. **S1 opens one profile at a time** per worker (`profile_session_limit`). A
   deliberate bound while the lease design is new, not a permanent constraint.
8. **`icacls` is best-effort.** If it fails, creation continues and the
   directory keeps `%LOCALAPPDATA%`'s own user scoping.
9. **The `www` alias in `allowed_origins` is an assumption**, not an
   observation. S3 will need real origin scope for sites that use another
   sub-domain for sign-in.
10. **Nothing here has been exercised against a real website.** Every S1 test
    uses the local fixture.

---

## 23. Authenticode release-gate status

**Unchanged by S1, and still a gate.** The build is not signed.

M7b's packaged acceptance substituted the stock Electron 38 executable to get
past Smart App Control, which **disables embedded asar integrity validation**.
That workaround is **development-only**. It is not acceptable for a build that
will hold real session data, and this report does not claim packaged M8a is
ready for real accounts.

**S1's packaged acceptance did not need the substitution.** It ran against this
build's own `release/0.1.0/win-unpacked/Lumi.exe`, unmodified, and passed. That
is a better result than M7b's, and it changes nothing about the gate: Smart App
Control allowing a particular unsigned rebuild on a particular day is not a
property anyone can ship on. The binaries are still unsigned (§17), including
the bundled `chrome.exe`.

**Lumi must be Authenticode-signed before an M8a build touches a real user
account.** This is a release gate independent of the slices, and S1 does not
solve it because the repository has no signing infrastructure to make it
practical. Existing integrity checks were not removed: the `.env` assertion, the
native-extension import check and the packaged acceptance test all remain, and
S1 adds the browser-profile and headless-shell assertions to the same set.

---

## 24. What was not started

Explicitly **not** built, not stubbed and not partially implemented:

* **S2** — no manual login, no takeover state machine, no headed window, no
  credential-surface detector, no trusted login controls, no login card, no
  takeover indicator, no restart-during-login semantics. `AUTHENTICATED` exists
  in the status vocabulary and nothing assigns it.
* **S3** — no `authenticated_read` grant, no `ACCOUNT_READ`, no site-scoped
  navigation, no `reveal`, no disclosure manifest, no redaction, no provider
  disclosure card, no authenticated answer. `account_fingerprint` and
  `account_label_hash` are nullable columns with no writer and no fabricated
  default; absence never implies authentication.
* **M8b (S4–S6)** — no element observation, no refs, no form epochs, no
  `protected_values`, no manifest digest, no field writes, no freeze entry, no
  dirtiness tracking, no handover.
* **M9, M10** — untouched.
* **UI** — no new renderer surface, no IPC channel, no preload method, no entry
  in the shared runtime contract. Electron main does not know profiles exist.
* No new planner operation, grant kind, permission card or model-facing field
  was added anywhere in this slice.

---

## 25. Exit criteria

| # | Criterion | Status |
|---|---|---|
| 1 | A persistent Chromium profile can be created for one registrable domain | **Met** — §1, §6 |
| 2 | Directory is opaque and under `%LOCALAPPDATA%\Lumi\browser-profiles` | **Met** — §3 |
| 3 | Session state survives worker/runtime restart without exporting cookies | **Met** — §19 (with the documented difference) |
| 4 | Two owners cannot concurrently open one profile | **Met** — §7 |
| 5 | Stale-lease recovery is deterministic | **Met** — §8 |
| 6 | Chromium downgrade refused rather than risking corruption | **Met** — §9 |
| 7 | Deleting removes local state without claiming server logout | **Met** — §11 |
| 8 | No profile path crosses renderer/main/model/log boundaries | **Met** — §3, §13, and the log tests |
| 9 | No cookie/token/storage extraction API exists in runtime code | **Met** — §12 |
| 10 | PSL behaviour pinned and tested | **Met** — §5 |
| 11 | Persistent contexts remain brokered by S0 | **Met** — §15 |
| 12 | Full Chromium packaged; M7a/M7b still work headless with it | **Met** — §16, §20 |
| 13 | Profile directories can never enter the packaged artifact | **Met** — §16 |
| 14 | Manifest records browser and PSL identity | **Met** — §18 |
| 15 | Existing M1–M7b/S0 behaviour intact | **Met** — §20 |
| 16 | There is still no manual login flow | **Met** — §24 |
| 17 | There is still no authenticated page reading | **Met** — §24 |
| 18 | There is still no form interaction | **Met** — §24 |

**S1 stops here.**
