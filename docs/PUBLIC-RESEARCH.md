# Public web research (Milestone 7b)

One bounded permission, many public pages, one grounded answer. Design source:
[`plans/general-computer-use-architecture.md`](plans/general-computer-use-architecture.md) §L M7b.

Milestone 7a answers "inspect *this* address". Milestone 7b answers "find this
out on the public web", where the user does not know the final address and
neither does Lumi until it looks. The user confirms one bounded scope in the
trusted UI; after that, Lumi searches, opens public pages, follows links it has
observed, reads them, and stops — at the goal, at a budget, or at the first
thing the scope does not cover.

It is still not a general browser agent. There is no sign-in, no typing, no
form, no upload, no download, no file access, no purchase, no message, and no
request other than GET or HEAD. See [Limits](#limits-stated-plainly).

## What the user sees

```text
PUBLIC RESEARCH

Goal:
Find the Lumi repository on GitHub and tell me what it does.

Allowed for this task:          Not allowed:
✓ search the public web         ✗ sign in anywhere
✓ open public https pages       ✗ type into or submit any form
✓ follow links on those pages   ✗ upload or download anything
✓ read the visible text         ✗ buy anything or make a payment
✓ open and close its own tabs   ✗ send messages
                                ✗ read or change your files
                                ✗ reach private or local addresses
                                ✗ send anything but read requests

Limits: at most 20 steps, 30 pages or searches and 5 tabs, for up to 5 minutes.
Sent to answer: up to 10,000 characters of those pages' text, and your goal,
go to: Google Gemini.

                                        [Cancel]  [Allow research]
```

Then progress in Lumi's own words — `1 search · 2 pages read · 3 of 20 steps` —
and finally an answer with the pages it came from. Nothing on this card is
written by a website.

## Flow

```text
main composer: "Find the Lumi repository on GitHub and tell me what it does"
  -> routeTypedRequest: the durable agent claims it (never reaches realtime)
  -> runtime POST /tasks {type: public_research, objective}
  -> runtime POST /tasks/{id}/research/prepare {disclosure, budgets}
       the runtime builds the scope from its own configuration and opens a
       PENDING task grant. It authorises nothing.
  -> trusted card
  -> trusted click: POST /tasks/{id}/research/grant {grant_id, expected_revision}
       PENDING -> ACTIVE, expiring (10 minutes by default)

  then, for each step, until the goal or a budget:
    main planner (research_planning) chooses ONE step
      -> POST /tasks/{id}/research/steps {request_id, step, planner_calls}
           check the grant: ACTIVE, unexpired, this task, this scope
           check the budgets; check no step is already unresolved
           resolve the semantic ref to an address, from Lumi's own records
           bind the worker; ensure the task-owned session
           create the action -> AUTHORIZED -> mint a single-use step
             authorization -> consume it -> attempt -> dispatch (committed)
           execute exactly one operation
           store the bounded observation with the finished attempt
      -> the new observation goes back to the planner

  -> main answerer (research_answer) composes one answer from the observations
  -> POST /tasks/{id}/research/answer   (grounding checked again, then stored)
       the grant becomes COMPLETED and the browser session is dropped
```

Speech and typed text cannot grant the scope. A spoken "yes" only focuses the
card; the only route to an ACTIVE grant is the `grantResearchScope` IPC channel
behind the Allow button.

### Voice

Voice support is deliberately limited in this milestone and is documented as
such. The voice narration carries a closed research state (`awaiting_permission`,
`researching`, `answered`, `not_verified`, `stopped`), so a spoken status
question is answered by pointing at the card, and speech is told in as many
words that it cannot allow, run or read anything. There is **no** voice command
that creates a research task in M7b: the typed path in the main composer is the
supported entry point. Adding one later means adding a command that creates the
task and focuses the card, and nothing else — granting stays a trusted click.

## The step vocabulary

A planner chooses one member of a closed union (`app/domain/research.py`):

| Operation | What it takes | What it does |
|---|---|---|
| `public_search` | `query` | One bounded JSON GET to the configured search endpoint, made by the **runtime**. |
| `navigate` | `tab`, `target` (`result`/`link`/`seed` + refs) | Opens the address that ref resolves to. |
| `observe` | `tab` | Re-reads a tab. Issues no request. |
| `scroll` | `tab`, `direction` | One Page Down or Page Up key press, then re-reads. |
| `history` | `tab`, `direction` | Back or forward in that tab's own history. |
| `tab` | `action`, `tab` | Opens, activates or closes a task-owned tab. |

There is **no field** anywhere in that union for a CSS selector, an XPath
expression, a JavaScript snippet, a raw URL, an HTTP method, a header, a
cookie, a browser flag, a coordinate or a Playwright call. The planner's model
output is flatter still — primitives from closed enumerations plus refs
matching strict patterns — and `src/main/agent/research-planner.ts` *constructs*
the step from them rather than copying anything through.

## Semantic refs and staleness

| Ref | Means |
|---|---|
| `o<n>` | The n-th observation of this task. |
| `b<n>` | A text block inside one observation. |
| `l<n>` | A link that observation's page offered. |
| `r<n>` | A result that search returned. |
| `t<n>` | A task-owned tab. |
| `s<n>` | An address the **user** typed in the objective. |

A model never receives an address for a link or a search result — only a ref, a
label and a host. Resolution is a table lookup in Lumi's own records, so a
model cannot mint a destination by printing a plausible string.

A link ref is valid only for the document that issued it. Two independent
checks stand between `l5 of o3` and a request:

1. the **controller** refuses it when a newer observation of that tab has a
   higher document epoch, when it belongs to another tab, or when the session
   that issued it is no longer the live one;
2. the **worker** refuses it when its tab's live document epoch is not the
   epoch the ref was issued for, or when its own table for that epoch does not
   contain the ref.

The address the worker's table yields must equal the address the controller
resolved. Disagreement is refused, never reconciled.

## Authorization: grants and step authorizations

Milestone 7a binds one exact approval to one proposal. Research cannot work
that way — a user is not going to approve twenty navigations — so M7b adds the
reviewed two-level model (migration `0005`):

* **`task_grants`** — one reusable, bounded scope per research task. It is
  task-bound, scope-bound (`scope_digest`), policy-version-bound, expiring,
  revocable, persisted, and not reusable by another task. Its `scope` is
  immutable after insert (trigger). At most one PENDING-or-ACTIVE grant exists
  per task, and a closed grant can never be reactivated.
* **`step_authorizations`** — one single-use authorization per step, minted by
  the runtime *only after* checking the step against the grant. Consuming it
  re-checks every binding in the same SQL statement: the grant is ACTIVE,
  unexpired and at the bound revision with the bound scope digest; the action
  is at its bound revision with its bound proposal digest; the authorization is
  unconsumed, unexpired and belongs to this runtime process.

`action_attempts` now carries `approval_id` **or** `step_authorization_id`,
never both and never neither (a CHECK constraint). Both columns are UNIQUE, so
one authority funds at most one attempt.

The action ledger gains one state, `AUTHORIZED`, and one event,
`action.authorized`. It is deliberately not spelled `APPROVED`: the user
confirmed a scope, not that step, and the timeline must not claim otherwise.

## Browser session

One task-owned Chromium context, reused across the task's steps, with:

* no storage state, no imported profile, no cookie jar copied from anywhere;
* service workers blocked, downloads refused, no permissions granted;
* tabs with logical refs (`t1`..`t5`) allocated by the worker;
* a document epoch per tab and a ref table per (tab, epoch).

The session lives in the worker's memory and is bound to its generation. A
worker or runtime restart takes it away, the `research_sessions` row becomes
`STALE`, and every ref it issued stops resolving. The task must re-observe
authoritative browser state before it may continue; `observe` on a fresh tab
honestly reports that the tab holds no page yet.

**Research is not affected by Milestone 8a's persistent profiles.** S1 added a
second, separate kind of browser context — a persistent, Lumi-managed profile
bound to one site — and the two refuse each other rather than falling back. A
research session is still created with no `user_data_dir`, and the research code
has no route to a profile at all: separate worker endpoints, separate stores in
the worker, and a `BrowserContextKind` that a dispatch has to match. A public
research task can never resolve an authenticated profile, and an authenticated
profile can never be substituted for a research session. No M7b grant or
authorization semantics changed because profiles exist.

## Network policy (`public-research-v1`)

The same three layers as Milestone 7a, with layer 2 replaced:

* **Shape** — unchanged from `public-url-v1`: `https:` only with the default
  port, no user information, no IP literal in any spelling, no percent-encoded
  or backslashed authority, no single-label/local/reserved suffix, printable
  ASCII only. `file:`, `data:`, `javascript:`, `about:`, `chrome:`,
  `devtools:`, `ws:`/`wss:` and custom protocols are refused.
* **Scope** — research may reach *any host that passes layer 1*, because a
  research task cannot know its destinations in advance. Configuration can
  narrow this back to a host list (`researchHosts`). The Milestone 7a
  inspection policy keeps its own allowlist whatever research allows.
* **Resolution** — every address the host resolves to must be globally
  routable. Loopback, private, link-local (including `169.254.169.254`),
  carrier-grade NAT, multicast, reserved and unique-local addresses are
  refused, as are IPv4-mapped and 6to4 spellings of them.

Inside the browser context, the network guard additionally allows only
documents, stylesheets, scripts and fetch/XHR; only `GET` and `HEAD`; no
WebSockets; no popups (a popup is closed, never adopted as a tab); no
downloads or attachments; no non-HTML document types; and it validates every
redirect hop itself rather than letting Chromium follow one.

### The egress broker (Milestone 8 S0)

Underneath all of that, **the browser does not open its own connections.**
Chromium is launched pointing at a local egress broker
(`app/browser/egress_broker.py`) that runs inside the browser worker, binds an
ephemeral loopback port, and requires a per-launch credential on every request
including `CONNECT`. For a connection it permits, the broker resolves the
requested host itself, requires every returned address to be globally routable,
and then opens the socket to an address from *that* resolution. Chromium does
not independently choose the destination address for a brokered connection.

Two details make it a boundary rather than a decoration:

* Chromium bypasses proxies for loopback by default, so the browser is launched
  with `--proxy-bypass-list=<-loopback>`. Without it a page could reach
  `http://127.0.0.1:…` directly.
* Playwright's driver — which is what `route.fetch` uses, and therefore how the
  network guard fetches every request — sends `CONNECT` to the broker for
  everything it fetches, including plaintext test origins. That is verified by
  a test, not assumed.

The broker never terminates TLS. A permitted `CONNECT` is answered and then
spliced byte for byte, so certificate validation, hostname verification and HSTS
remain Chromium's and a certificate error remains an error. There is no
interception certificate authority.

Plaintext HTTP is reachable only for exactly-configured local fixture origins;
public destinations are `https:` on port 443 or nothing.

Search is the exception, and it is deliberate: it is a runtime-side request, not
browser traffic, and it is not brokered. See below.

Search is **not** a browser operation. It is a bounded JSON GET the runtime
makes, so no search credential and no search-engine markup ever reaches the
browser.

## Budgets

Counted by the runtime from the database, not by the planner:

| Budget | Default |
|---|---|
| executed steps | 20 |
| observations | 30 |
| planner calls | 20 |
| task-owned tabs | 5 |
| active execution | 5 minutes |
| model input / output tokens | 60,000 / 8,000 |
| image (vision) calls | 2 |
| grant lifetime | 10 minutes |

These are tuning defaults, configurable per task. Reaching one stops the task
with an honest partial answer; it never becomes another step.

## Grounding

The final answer must quote the observations Lumi actually made. Every cited
`o<n>`/`b<n>` must exist in *this task's* evidence, every quote must occur in
the block it cites, and every number in the answer must occur in a quote. Both
Electron main and the runtime run the check, and the runtime is the one that
stores it. "Lumi could not verify that from the public pages it was able to
read" is a real, recorded outcome.

## Prompt injection

Everything a page or a search provider contributes — text, headings, link
labels, snippets, titles, code samples, README content — is
`untrusted_environment` data, delivered inside its own markers, and it cannot:

* add a capability (there is no operation for what it asks);
* create or widen an authorization (no route, parameter or code path exists);
* change the objective (the only trusted instruction is the user's);
* put an address in front of the planner (links arrive as refs and hosts);
* become a label, a control or a link on the trusted card.

The deterministic fixture at `evals/sites/public_pages/` includes a hostile
page that asks an agent to sign in, upload files, message contacts, download an
installer and report a false figure, plus beacon, `fetch`, `sendBeacon`,
WebSocket, popup and same-origin POST exfiltration attempts against a canary
origin. The tests assert that the canary's request log stays empty.

## Configuration

Development (`.env`, read by Electron main only):

```ini
# Research is absent until one of these is set.
LUMI_RESEARCH_ANY_PUBLIC_HOST=1          # any host that is not local/private
LUMI_RESEARCH_HOSTS=github.com           # or narrow it to a list
LUMI_RESEARCH_TEST_ORIGINS=http://127.0.0.1:8822   # unpackaged fixtures only
LUMI_RESEARCH_SEARCH_ENDPOINT=https://api.search.example/?q={query}
```

Installed builds (`%APPDATA%\Lumi\agent-runtime.json`):

```json
{
  "databaseUrl": "postgresql+asyncpg://lumi:<password>@127.0.0.1:5432/lumi_agent",
  "research": true,
  "researchSearchEndpoint": "https://api.search.example/?q={query}"
}
```

The search endpoint is a URL template containing exactly one `{query}`.
`LUMI_RESEARCH_SEARCH_API_KEY` and `LUMI_RESEARCH_SEARCH_HEADER` (default
`X-Subscription-Token`) add one request header for providers that need one;
they are read by the runtime, never by the browser worker. Any provider whose
JSON matches one of the documented shapes works without new code — `results`,
`web.results`, `webPages.value` or `organic_results`, with `title`/`name`,
`url`/`link` and `snippet`/`description`. Anything else needs a reviewed reader
in `app/services/research_search.py`, not a configuration flag.

Lumi ships with **no** default search provider. Without one, research still
navigates to an address the user typed and follows links; it simply has no
`public_search` operation in its scope.

## Limits stated plainly

* **This is not a sandbox, and the broker does not make it one.** The broker is
  a *network-policy boundary for managed browser traffic*. It constrains
  Chromium; it does not constrain the worker process that hosts it. Code running
  natively in that process — which a compromised worker would be — can open any
  socket it likes. Process separation is not privilege isolation, and nothing in
  Milestone 8 S0 changes that.
* **What the broker does close.** DNS rebinding, for connections it carries:
  there is one resolution and the socket is opened to an address from it, so
  there is no second lookup to poison. Before S0 the destination was checked in
  one place and connected from another, and the guard additionally *cached* a
  host's successful resolution for the lifetime of the context — so later
  requests to that host were matched against a remembered answer while the
  connection was made afresh. That cache is gone; refusals are still remembered,
  successes are re-checked, and the connection-time decision is the broker's.
* **Not everything Lumi does is brokered.** `public_search` is a runtime-side
  JSON `GET` to a *configured* endpoint. It never goes through the broker and is
  not browser traffic; its destination comes from trusted configuration rather
  than from a page or a model, and it is shape- and resolution-checked in the
  runtime. Provider traffic, database traffic and every other runtime request
  are likewise outside the broker. "All of Lumi's network traffic is brokered"
  would be false; "the managed research browser's connections are" is true.
* **`READ_ONLY` is narrow.** It means Lumi issues no intentional mutation
  operation. A `GET` can still be logged, counted or acted on by a server.
* **Popup closing has a narrow race.** Playwright fires the context's `page`
  event for tabs Lumi opens itself, so the guard suspends popup-closing for the
  instant it takes `new_page()` to return. A site-opened popup landing inside
  that instant is adopted instead of closed. It gains nothing — every request
  it makes still passes the guard, and no operation can name a tab that is not
  in the session's table — but it stays open until the session ends.
* **Scrolling sends one key press.** It is a fixed `PageDown`/`PageUp`, not
  JavaScript and not a coordinate, but it is still input to the page.
* **Anti-bot blocks are limitations, not problems to route around.** Some sites
  refuse automated browsers; LeetCode returned HTTP 403 in Milestone 7a and
  still does. Lumi reports that honestly. There is no CAPTCHA solver, no
  stealth mode and no attempt to look like a human browser.
* **A lost step is unknown, not failed.** A public read has no consequential
  effect to reconcile, so the action stays `OUTCOME_UNKNOWN`, blocks the task,
  and is never repeated automatically. The way forward is to re-observe. The
  card says so: while any step of the task is unresolved, `unresolved_step` is
  true in the view and the card reads "Lumi does not know what its last step
  did", with Stop as the only control. It never shows confident progress over
  a step whose outcome nobody has.
* **Recovery is honest, not exactly-once.** Duplicate planner requests cannot
  create duplicate durable state (the `request_id` is the idempotency key), a
  consumed step authorization can never be reused, and a worker-generation
  mismatch fails closed. None of that makes an arbitrary web read
  transactional.
* **The planner is one model call with a closed contract.** It is not a
  guarantee of good judgement. The deterministic tests prove the pipeline and
  the boundaries; they do not prove that a live model researches well.

## Tests

| Where | What it pins |
|---|---|
| `services/agent/tests/test_research_domain.py` | The step vocabulary, refs, scope, budgets, grounding, the destination policy, the search reader. |
| `services/agent/tests/test_research_authorization.py` | Nothing before the trusted grant; expiry; revocation; single-use authorizations; cross-task refusal; budgets; the answer. |
| `services/agent/tests/test_research_browser.py` | A real Chromium: session reuse, semantic refs, stale refs, tabs, the network boundary, hostile content, redirects, mutation requests. |
| `services/agent/tests/test_research_ledger.py` | The whole pipeline through real processes: multi-hop research, the scope card, budgets, a worker restart, a runtime crash. |
| `src/main/agent/research-planner.test.ts` | What a planner is able to say, and what it is not. |
| `src/main/agent/research-answer.test.ts` | Grounding, and a persuaded model being refused. |
| `src/main/services/agent-research.test.ts` | The loop: consent, refusals, budgets, unknown outcomes. |
| `src/main/services/agent-research-boundary.test.ts` | Runtime routes, IPC channels, configuration validation. |
| `src/renderer/src/composer-routing-research.test.ts` | Who owns a typed research request, and what never reaches realtime. |
| `src/renderer/src/agent-research-view.test.ts` | What the trusted card says. |
| `src/main/services/agent-wire-research.test.ts` | The wire: every research field checked, an address in an observation refused. |
| `scripts/run-evals.mjs` (group `research`) | The behaviour cases, including hostile pages and consent. |

Fixture site: `uv run python -m evals.sites.public_pages.server --port 8811`
(`/search`, `/research/hub`, `/research/project`, `/research/decoy/*`,
`/research/hostile`, `/research/loop/*`).

Real public web: `uv run python -m scripts.research_smoke [url ...]`. Opt-in,
read-only, no model. It starts a real worker and Chromium, opens one research
session and prints what each address returned -- including a refusal, which is
the honest result for a site that blocks automated browsers.

Its result on 18 September 2026, one session, three addresses in the same tab:

| Address | Result |
|---|---|
| `github.com/satish9177/Lumi` | OK. 120 blocks (6,807 chars, truncated), 25 of 125 links. |
| `docs.python.org/3/library/asyncio-task.html` | OK. 120 blocks, 25 of 31 links. |
| `leetcode.com/problems/two-sum/` | **HTTP 403**, `page_http_error`, `FAILED_BEFORE_EFFECT`. |

LeetCode served a Cloudflare interstitial ("Performing security verification").
Lumi reported the 403 and stopped. Nothing here tries to look like a human
browser, and nothing solves a challenge. The document epoch moved 5 -> 7 -> 10
across those steps in one reused session, which is the other thing this script
shows: the browser persists, and every ref is bound to the document that issued
it.
