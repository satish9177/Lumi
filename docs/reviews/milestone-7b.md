# Milestone 7b — public web research: implementation report

Branch `lumi-agent-v2`. Base `c6ed989603cc40ae11caac0195b68e4572d021ec`
("fix(ui): let typed requests send without a voice session").
Final commit **`a49ebdf61accf3179951352257e12901d0a95f75`**, pushed to
`lumi-agent-v2`.

Design: [plans/general-computer-use-architecture.md](../plans/general-computer-use-architecture.md)
§L M7b. Milestone documentation:
[PUBLIC-RESEARCH.md](../PUBLIC-RESEARCH.md), with the boundary written up in
[SECURITY.md](../SECURITY.md) and the cases in [EVALS.md](../EVALS.md).

## 1. Summary

Milestone 7a answers "inspect *this* address". M7b answers "find this out on
the public web", where the user does not supply the final URL and Lumi does not
know it until it looks. The user confirms one bounded scope in the trusted UI;
after that Lumi searches, opens public pages, follows links it has itself
observed, reads them, and stops — at the goal, at a budget, or at the first
thing the scope does not cover. Then it answers from what it read, with sources.

It is not a general browser agent. There is no sign-in, no typing into a site,
no form, no upload, no download, no file access, no purchase, no message, and
no request other than `GET` or `HEAD`. The model proposes one step at a time;
deterministic code decides whether that step happens.

Nothing was added outside the existing architecture: the same Electron-main
credential boundary, the same Python durable authority, the same isolated
Playwright worker, the same action ledger, the same worker-generation fencing.

## 2. Files changed

83 files, +20,929 / −1,605. New modules:

| Python | TypeScript |
| --- | --- |
| `app/domain/research.py` (763) — the closed vocabulary | `src/main/agent/research-planner.ts` (451) |
| `app/services/research_tasks.py` (1,362) — the controller | `src/main/agent/research-answer.ts` (295) |
| `app/repositories/research.py` (694) | `src/main/testing/fake-research-runtime.ts` (373) |
| `app/services/research_search.py` (253) | |
| `app/browser/research_session.py` (283) | |
| `app/browser/operations/research.py` (666) | |
| `alembic/versions/0005_create_research_grants.py` (544) | |
| `scripts/research_smoke.py` — opt-in real-web smoke | |

Extended rather than duplicated: `action_status`, `task_status`,
`services/actions.py` (scoped attempts), `services/tasks.py` (cancel revokes a
grant), `network_guard.py` (multi-tab), `registry.py`, `protocol.py`,
`worker.py`, `client.py`, `browser/config.py`, `managed.py`, `config.py`,
`repositories/actions.py`, `services/browser_execution.py`, the four
`app/api/*` modules; and on the desktop side `agent-contracts.ts`,
`agent-wire.ts`, `agent-tasks.ts`, `task-request-interpreter.ts`,
`agent-runtime-supervisor.ts`, the four `models/*` modules, `agent-ipc.ts`,
`preload/index.ts`, `main/index.ts`, `packaged-runtime.ts`,
`agent-task-view.ts`, `AgentTaskPanel.tsx`, `composer-routing.ts`,
`LifeLensApp.tsx`, the voice contracts, and `scripts/run-evals.mjs`.

## 3. Migration

Forward-only Alembic revision `0005_create_research_grants`, head `0004` →
`0005`. It adds `AUTHORIZED` to `ck_actions_status`; creates `task_grants`
(partial unique index `uq_task_grants_task_id_open`, immutability trigger on
`scope`), `step_authorizations` (single-use trigger), `research_sessions`,
`research_observations` (immutability trigger) and `research_answers`; makes
`action_attempts.approval_id` nullable and adds `step_authorization_id` with a
`CHECK ((approval_id IS NULL) <> (step_authorization_id IS NULL))`, so every
attempt still has exactly one authority; and adds `browser_dispatches.session_id`.
Booking and M7a tables are untouched. A metadata-comparison test keeps the
SQLAlchemy tables and the migration in step.

## 4. Task grants

One reusable bounded scope per research task. It is task-bound, scope-bound
(`scope_digest` over canonical JSON), policy-version-bound
(`public-research-v1`), expiring (default 600s, `LUMI_RESEARCH_GRANT_TTL_SECONDS`),
revocable, persisted, and not reusable by another task. Statuses:
`PENDING → ACTIVE → {REVOKED, EXPIRED, COMPLETED}`; a closed grant can never be
reactivated, and at most one open grant exists per task. Confirming is a
compare-and-swap on the task revision *and* the scope digest, so the card the
user saw is the scope that becomes active. Cancelling the task revokes the
grant, closes the session and appends `task.research_scope_revoked` in the same
transaction. The model cannot create, widen or extend a grant — there is no
route, no tool and no field for it.

## 5. Step authorizations

One single-use authorization per step, minted by the runtime only after it has
checked the step against the grant. Consuming it re-checks every binding inside
one `UPDATE`: unconsumed, unexpired, bound action revision, bound proposal
digest, bound runtime generation, and an `EXISTS` on an `ACTIVE` grant at the
bound revision and scope digest. Repeated user approvals are not used as a
stand-in for reusable authorization, and a reusable grant is not used as a
stand-in for per-step checking.

## 6. Browser session

One task-owned persistent context per research task, reused across steps,
opened with `service_workers="block"`, `accept_downloads=False`,
`permissions=[]`. Nothing is imported from the user's Chrome or Edge profile;
no cookie, storage entry or credential is exposed to a model or persisted into
an observation. The session is bound to the task and the runtime generation. A
stale reference fails closed after a restart, and the task may continue only by
re-observing authoritative browser state. One active task, one step in flight,
tabs bounded by the grant.

## 7. Operation registry

Five browser operations plus one runtime operation, all in the existing closed
registry with declared effect, retry and timeout semantics
(`Effect.READ_ONLY`, `RetryPolicy.OBSERVE_THEN_REPLAN`,
`OperationTarget.RESEARCH_SESSION`):

| Operation | Takes |
| --- | --- |
| `public_search` | `query` (runtime-side; the browser is not involved) |
| `navigate` | `tab`, `target` = `result`/`link`/`seed` + refs |
| `observe` | `tab` (issues no request) |
| `scroll` | `tab`, `direction` (one `PageDown`/`PageUp`) |
| `history` | `tab`, `direction` |
| `tab` | `action` (open/activate/close), `tab` |

Input models are `extra="forbid"`. `follow_link`, `back`, `forward`,
`open_tab`, `activate_tab` and `close_tab` from the architecture sketch are
folded into `navigate`, `history` and `tab` — fewer operations, the same
capability, less surface to validate.

## 8. Observations and semantic refs

An observation carries `observationId`, `taskId`, `sessionId`,
`runtimeGeneration`, `tabId`, `documentEpoch`, `sequence`, `requestedUrl`,
`finalUrl`, `title`, `timestamp`, `contentHash`, bounded text blocks (≤120
blocks, ≤10,000 chars), bounded links (≤25 of N), `truncated`, `settled` and
provenance `untrusted_environment`. Refs are `o<n>` observation, `b<n>` block,
`l<n>` link, `r<n>` result, `t<n>` tab, `s<n>` an address the *user* typed.

A model never receives an address for a link or a search result — only a ref, a
label and a host — so it cannot mint a destination by printing a plausible
string. No DOM, cookie, storage entry, password field or authorization header
is persisted.

Staleness is checked twice. The controller refuses a ref when a newer
observation of that tab has a higher document epoch, when it belongs to another
tab, or when its session is not the live one. The worker refuses it when its
tab's live epoch is not the epoch the ref was issued for. The address the
worker resolves must equal the one the controller resolved; disagreement is
refused, never reconciled.

## 9. Planner

One planner (`research_planning`), not several. It receives the trusted
objective, durable task state, the observations with website text explicitly
fenced as `UNTRUSTED ENVIRONMENT DATA`, bounded prior evidence, the budgets and
the allowed operation schema. Its output schema is flat, `additionalProperties:
false`, and its fields are exactly `action, operation, query, tab, target,
observation, ref, direction, tab_action, stop_reason, reason`. There is no
field for a URL, selector, XPath, script, method, header, cookie or coordinate,
and `parseResearchDecision` *constructs* the step from checked primitives
rather than copying anything through. An unsupported operation is refused and
replanned within two attempts, then the task stops safely.

The answerer (`research_answer`) is separate and has three fields:
`status`, `answer`, `evidence`.

## 10. Search

A bounded JSON `GET` made by the **runtime**, not the browser: the destination
is policy-checked and DNS-resolution-checked, redirects are not followed, and
status, content type and size are bounded. Results are structured
(`resultRef`, title, host, snippet) and the planner may choose one `resultRef`.
No search credential and no search-result markup ever reaches the browser
worker, and results are untrusted data that can never become executable
instructions. Four common response shapes are parsed (`results`,
`web.results`, `webPages.value`, `organic_results`) so no single paid provider
is baked in; the endpoint, key, header and timeout are configuration
(`LUMI_RESEARCH_SEARCH_*`), and deterministic scripted support covers the
offline case. Queries are refused when they carry a URL, an address, a long
digit run or a secret word, so a research query cannot become an exfiltration
channel.

## 11. Network policy

`public-research-v1` keeps all three layers of the M7a policy — shape, scope,
DNS resolution — and replaces only the trusted host allowlist with "any name
that resolves solely to globally routable addresses". The resolution layer as
shipped in M7b remembered a host's verdict for the lifetime of the context; see
§17 for the correction and for what Milestone 8 S0 changed. M7a's own policy
(`public-url-v1`) keeps its allowlist unchanged. Refused: localhost, loopback,
RFC1918, link-local, multicast and reserved ranges, IPv6 loopback/private/
link-local, cloud metadata endpoints, `file:`, `data:`, `javascript:`,
browser-internal and custom protocols, credential-bearing URLs, non-HTTPS
public navigation (except a controlled test fixture origin), WebSocket,
beacon, popup-driven navigation, downloads and external protocol handlers.
Every redirect hop is revalidated by the guard, because Playwright does not
call route handlers for redirect hops. `GET`/`HEAD` only, unchanged from M7a.
A canary fixture proves a refused destination was never contacted, rather than
merely that Lumi said it refused.

## 12. Budgets

Defaults, all configurable: 20 steps, 30 observations, 20 planner calls, 5
tabs, 300s active execution, 60,000 model input tokens, 8,000 output tokens, 2
vision calls. They are recorded in the grant's scope, shown on the card, and
enforced by the runtime before a step is authorized — not by the planner
promising to behave. On reaching a limit the task stops and answers honestly
with what it has; the looping-corridor fixture proves it terminates.

## 13. Recovery

No exactly-once claim for an arbitrary web read. A duplicate planner request
cannot create duplicate durable state (`request_id` is the idempotency key). A
consumed step authorization can never be reused. A worker-generation mismatch
fails closed. After a runtime or browser restart, every semantic ref is stale
and the task must re-observe before continuing. A lost step stays
`OUTCOME_UNKNOWN`, blocks the task and is never repeated automatically; the
card says so in as many words — "Lumi does not know what its last step did" —
with Stop as the only control, rather than showing confident progress over a
step whose outcome nobody has.

## 14. Prompt-injection defenses

Page text, headings, buttons, snippets, ARIA labels, metadata, URLs, code
samples and README content are untrusted throughout. The only trusted inputs
are the user's objective and the controller's policy. A hostile page cannot
widen scope, create authorization, request files, trigger Telegram, expose a
secret, enable a download, cause a submission or edit the planner's rules,
because there is no field in which to ask and no route that would accept it.
Grounding is the second line: a quote must occur in the block it cites and
every number in the answer must appear in a quote, checked in TypeScript and
again in Python. The hostile fixture asks for "999 contributors" and an upload;
the answer is refused with `quote_not_in_block`. Website text never becomes a
UI label or a control.

## 15. Results (2026-09-18, Windows 11)

| Check | Result |
| --- | --- |
| `npm.cmd run typecheck` | Pass |
| `npm.cmd run build` | Pass |
| `npm.cmd test` | **1956 passed, 1 failed, 22 skipped** (3 failed files). The failures are the three known environment baselines, identical to `baseline-vitest.log`: `vision/real-inference.test.ts` and `vision/tokenizer-pack.test.ts` (CLIP pack not installed) and `accessibility.test.tsx:73` (CRLF). Baseline before M7b: 1511 passed with the same three. |
| `uv run pytest` | **765 passed, 16 skipped** (opt-in desktop and packaged suites), exit 0 |
| `uv run mypy` | Pass, 122 files |
| `npm.cmd run eval` | **118/118** deterministic eval cases |
| Electron acceptance (`LUMI_ELECTRON_E2E=1`) | **15/15**, run one file at a time: `test_electron_acceptance` 2, `test_voice_acceptance` 3, `test_m6_acceptance` 5, `test_inspection_acceptance` 5 |

Note on the Electron suite, stated plainly: running all four files in a single
pytest process on this machine produced 7 failures, every one of them
`AssertionError: the Lumi renderer never loaded` — the 60-second launch
deadline, not a behavioural assertion. Each file passes on its own, including
each of those 7 cases. README and EVALS.md now say to run them one file at a
time. `pytest` was never run twice concurrently against `lumi_agent_test`.

`LUMI_PUBLIC_INSPECTION_HOSTS=""` is set for the Python runs. Without it, the
developer `.env` makes `test_booking_preparation.py::test_booking_routes_without_a_worker_answer_503`
fail — a pre-existing environment interaction, reproducible on the M7b base
commit.

New test counts: Python `test_research_domain` 31, `test_research_authorization`
24, `test_research_browser` 26 (real Chromium), `test_research_ledger` 10 (real
processes, kills, restarts). TypeScript `research-planner` 44,
`research-answer` 21, `agent-research` 21, `agent-research-boundary` 9,
`agent-wire-research` 14, `agent-research-view` 15, `composer-routing-research`
10. Evals: 35 research cases and 5 composer cases.

## 16. Manual smoke on the real public web

`uv run python -m scripts.research_smoke` — opt-in, read-only, no model, one
session, three addresses in the same tab:

| Address | Result |
| --- | --- |
| `github.com/satish9177/Lumi` | OK. 120 blocks (6,807 chars, truncated), 25 of 125 links. Title and README text read. |
| `docs.python.org/3/library/asyncio-task.html` | OK. 120 blocks, 25 of 31 links. |
| `leetcode.com/problems/two-sum/` | **HTTP 403**, `page_http_error`, `FAILED_BEFORE_EFFECT`. |

LeetCode served a Cloudflare interstitial ("Performing security verification",
Ray ID logged by the page itself). Lumi reported the 403 and stopped. Nothing
attempts to look like a human browser, and nothing solves a challenge — this is
the honest limitation, not a defect to route around, and acceptance never
depended on it. The document epoch moved 5 → 7 → 10 across the three steps in
one reused session, which is the other thing the script demonstrates.

The controlled multi-hop, search, decoy, hostile, stale-ref, redirect, budget
and crash/restart flows are covered by the fixture suites rather than by hand,
so they stay verified.

There is no LeetCode-specific adapter, and no site-specific adapter of any kind.

## 17. Known limitations

* **Not a network sandbox.** *(Corrected, and since closed — see
  [milestone-8-s0.md](milestone-8-s0.md).)* As shipped in M7b, resolution
  happened in the policy and again inside Playwright's driver, so a hostile DNS
  server could race the two (rebinding). The wording elsewhere in this document
  implied a fresh application-level DNS check before every request; that was not
  accurate. `PublicNetworkGuard` cached a host's resolution **verdict** for the
  lifetime of the context (`self._resolutions`), so a *successful* resolution
  was reused for later requests to that host while the connection was made
  again each time. M7a narrowed this with a host allowlist; research could not,
  because it does not know its destinations. Milestone 8 S0 added the
  connection-time egress broker, removed the success cache, and made the
  connecting component the one that resolves and checks.
* **`READ_ONLY` is narrow.** It means Lumi issues no intentional mutation. A
  `GET` can still be logged, counted or acted on by a server.
* **Popup closing has a narrow race.** The guard suspends popup-closing for the
  instant `new_page()` takes to return. A site-opened popup landing in that
  instant is adopted rather than closed; it gains nothing (its requests still
  pass the guard, and no operation can name a tab outside the session's table)
  but it stays open until the session ends.
* **Scrolling sends one key press** — a fixed `PageDown`/`PageUp`, but still
  input to the page.
* **Anti-bot blocks are limitations.** See LeetCode above.
* **A lost step is unknown, not failed**, and the UI says so.
* **Recovery is honest, not exactly-once.**
* **Voice is deliberately limited.** Voice narrates a closed research state and
  can focus the card; there is no voice command that creates a research task in
  M7b, and speech can never grant, run or read. The typed path is the supported
  entry point. Documented in PUBLIC-RESEARCH.md §Voice.
* **The planner contract is not good judgement.** The deterministic tests prove
  the pipeline and the boundaries, not that a live model researches well.
* **Grounding proves provenance, not correct pairing.** A quote and a number
  come from the page; that the model paired the right label with the right
  value is checked by showing the user the evidence.

## 18. Regressions checked

Appointment booking, clinic information, M7a exact-URL inspection,
main-composer single-owner routing, typed requests without a voice session, the
OpenAI/Gemini/DeepSeek model router and its provider disclosure, crash-safe
booking semantics, and the existing network mutation protections: all still
pass, in the unit suites, the eval suites and the real-desktop acceptance
suites listed above. M7a keeps its own `public-url-v1` allowlist; the research
policy is a separate version string and a separate object.

## 19. Scope

M8, M9 and M10 were **not started**. No authenticated browsing, login, session
or cookie import, password/OTP/CAPTCHA handling, form filling, typing user data
into a site, submission, purchase, upload, download, filesystem write, desktop
UIA, mouse/keyboard control, coordinate clicking, screenshot-based computer
use, shell access, arbitrary Playwright, arbitrary selector, arbitrary
JavaScript, browser extension, project execution or additional autonomous
planner was added. Work stops at M7b.
