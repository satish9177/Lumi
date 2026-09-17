# Page inspection (Milestone 7a)

One approved URL, one grounded answer. Design source:
[`plans/general-computer-use-architecture.md`](plans/general-computer-use-architecture.md) §L M7a and §M.

The user gives a public page address and a question. Lumi shows an exact
approval card. After the trusted click, an isolated browser reads that one page
once. The runtime stores a bounded observation. Electron main then answers
from that observation, quoting the page. This is not browsing: there is no
clicking, typing, search, sign-in, download, upload, form submission, follow-up
navigation, or non-GET request.

"Read-only" is used narrowly. The operation is classified `READ_ONLY`, which in
M7a means Lumi performs no intentional mutation operation. It does not mean
opening a page has zero remote side effects: a server can log, count or react to
a GET.

## Flow

```text
renderer form / main composer or task-panel request containing one URL
  -> main composer: routeTypedRequest claims it (never reaches realtime/open_url)
  -> preload (fixed channels, positional strings)
  -> main: canonicalize + destination policy; question bounds
  -> runtime POST /tasks {type: page_inspection, url, question}        (policy again)
  -> runtime POST /tasks/{id}/inspection/prepare {disclosure}
       immutable proposal: url, host, question, policy_version, limits,
       disclosure recipients  -> digest -> exact, expiring approval request
  -> trusted card (host, URL, "no clicks, form submissions, uploads, downloads,
     or non-GET requests", question, who receives page text)
  -> trusted click: /approve, then /browser-execution with the revision on screen
       start_attempt (claims the single-use approval) -> dispatch row committed
       -> worker inspect_public_page {url}                           (policy again)
       -> PageObservation validated -> stored in the transaction that
          finishes the attempt
  -> main GET /actions/{id}/inspection -> model router (page_answer), only
     the approved recipients -> strict parse + grounding check
  -> runtime POST /actions/{id}/inspection/answer                   (grounding again)
```

Speech and typed text cannot approve. "Yes", "approve" and "go ahead" only
focus the card. The voice narration carries the host and a closed state, never
page text.

## Destination policy (`public-url-v1`)

The policy is enforced four times: by main when the user types the address, by
the runtime at task creation and again before execution, by the worker on the
approved URL, and by the worker's network guard on every request the page
makes.

* **Shape:** `https:` only, with the default port. No user information. No IP
  literal in any spelling a browser accepts (`127.1`, `0x7f000001`,
  `2130706433`). No percent-encoded or backslashed authority. No
  single-label, local or reserved suffix (`localhost`, `.local`, `.internal`,
  `.home.arpa`, `.test`, `.onion`, …). Printable ASCII only. `file:`, `data:`,
  `javascript:`, `about:`, `chrome:` and custom protocols are all refused.
* **Scope:** the host must be on a trusted allowlist (exact host or `*.suffix`).
* **Resolution:** every address the host resolves to must be globally routable.
  Loopback, private, link-local (including 169.254.169.254), carrier-grade NAT,
  multicast, reserved, unique-local, IPv4-mapped and 6to4 addresses are refused.
* **Redirects:** Playwright's route handler is *not* called for redirect hops
  (verified in this repository). The guard therefore never lets Chromium follow
  a 3xx. A main-document redirect is handed back to the operation, which
  checks the target and navigates to it as a new request, for at most 5 hops.
  Subresource redirects are dropped.
* **Everything else the page does:** only document, script, stylesheet and
  fetch/XHR requests are allowed, only to allowed hosts, and only with GET or
  HEAD. A page's own `fetch`/XHR POST, PUT, PATCH or DELETE -- same-origin
  included -- and any form submission are refused before they leave the
  context. Images, media,
  fonts, beacons, WebSockets and event streams are refused. Popups are closed,
  service workers are blocked, and downloads, attachments and non-HTML
  documents are refused.

**Honest limit:** this is not an egress proxy. The host is resolved by the
guard and again by Playwright's driver, so DNS rebinding can race the check.
That is why M7a does not offer arbitrary hosts: public destinations must be on
the allowlist. The M7b egress broker is the gate for broader coverage.

Configuration (trusted main only; empty means no inspection):

| Where | Setting |
| --- | --- |
| Development `.env` / shell | `LUMI_PUBLIC_INSPECTION_HOSTS=github.com,example.com` |
| Development only | `LUMI_INSPECTION_TEST_ORIGINS=http://127.0.0.1:<port>` (exact loopback fixture origins) |
| Packaged `agent-runtime.json` | `"publicInspectionHosts": ["github.com"]` (never test origins) |

Main passes these to the runtime as `LUMI_PUBLIC_INSPECTION_HOSTS` and
`LUMI_INSPECTION_TEST_ORIGINS`. The runtime passes them to its worker as
`LUMI_BROWSER_PUBLIC_HOSTS` and `LUMI_BROWSER_INSPECTION_TEST_ORIGINS`.

## The operation

`inspect_public_page` sits in the closed registry with target `PUBLIC_PAGE`,
effect `READ_ONLY`, retry `NEW_APPROVAL_REQUIRED` and reconciliation
`NOT_REQUIRED`. Its entire input is `{url}`: extra fields are rejected, so
there is no selector, XPath, script, header, cookie, browser argument or wait
condition. It runs in a fresh context with no stored state and follows a fixed
sequence: check → navigate (validated hops) → wait for the visible text to
settle (quiet for about 1.5 s with no script or API request in flight, capped
at 8 s) → check the final URL → read visible text and links with locators (no
page scripting) → build a hashed observation. If the page replaces itself
during extraction, it is read again once; otherwise the result is
`document_unstable`.

## Observation (`schema_version: 1`)

`observation_id`, `provenance: "untrusted_environment"`, `requested_url`,
`final_url`, `redirects`, `title`, `document_epoch` (main-frame commits during
the dispatch), `settled`, `observed_at`, `blocks` (`b1…`, ≤200 blocks, ≤500
chars each, ≤12,000 chars total), `links` (`l1…`, ≤20, http(s) only, fragments
dropped), `truncated`, `total_text_chars`, `total_link_count`, `content_hash`
(SHA-256 over the model-visible projection). Stored in `page_observations`
(migration `0004`) with `task_id`, `action_id`, `attempt_id` (unique),
`dispatch_id` (unique) and `worker_generation`. A trigger makes the evidence
immutable and lets the answer be written only once. No cookies, headers,
storage state, DOM, attributes or screenshots are stored.

The renderer receives metadata (including the page title and final URL,
shown as labelled plain text) plus the quoted evidence of a verified answer.
Full page text stays between the runtime and main.

## Answers

Main's `page_answer` route receives three separated parts. The system part holds
the rules and output contract. The user utterance holds the question. A
delimited untrusted observation section holds the page; page text cannot forge
the section markers. The output is exactly `{status, answer, evidence}`, with
`status` one of `answered`, `not_found` or `ambiguous`. An `answered` result
must cite blocks, each quote must occur in its cited block, and every number
in the answer must occur in a quote. Main checks this, and the runtime checks
it again before storing. `not_found` and `ambiguous` are stored with Lumi's own
text: "Could not verify this from the inspected page." If every permitted
model gave unverifiable output, the result is recorded as `not_verified`. If no
permitted model could be reached, nothing is recorded, and the answer can be
requested later from the saved observation without reopening the page.

Only providers named in the approved disclosure receive page text
(`ModelRouter` `permits`). The runtime refuses an answer for an observation
that is not the task's latest, or whose content hash differs
(`stale_observation`).

## Outcomes and recovery

| Ledger state | Meaning for an inspection |
| --- | --- |
| `SUCCEEDED` | A validated observation is stored with the attempt |
| `FAILED` | Lumi knows no usable observation was obtained (refused destination, redirect, HTTP error, download, timeout before a result) |
| `OUTCOME_UNKNOWN` | The read may have happened but its result was not received or stored (lost worker response, worker crash, runtime restart) |

A public read has no consequential effect to reconcile, so there is no
reconciliation route. `OUTCOME_UNKNOWN` is never rewritten as `FAILED` and never
retried automatically. A repeat is a new action with a new exact approval.
Unknown inspections do not block unrelated tasks, because nothing consequential
can be pending. A duplicate request with the same typed request id returns the
existing card, including after a main restart. A consumed approval cannot fund
a second read. Loading the panel after a restart calls no model and opens
nothing.

## Tests

| Layer | File |
| --- | --- |
| Policy (Python / TS) | `services/agent/tests/test_public_url_policy.py`, `src/main/agent/public-url-policy.test.ts` |
| Worker + real Chromium + fixture + canary | `services/agent/tests/test_public_page_worker.py` |
| Ledger rules over the API (no browser) | `services/agent/tests/test_page_inspection_api.py` |
| Real processes, kills and lost responses | `services/agent/tests/test_page_inspection_ledger.py` |
| Answers, grounding, hostile models, disclosure | `src/main/agent/page-answer.test.ts` |
| Main controller, duplicates, restart, speech cannot approve | `src/main/services/agent-inspection.test.ts` |
| Contract parity, routes, config, card model | `src/main/services/agent-inspection-contract.test.ts` |
| Real desktop app (opt-in `LUMI_ELECTRON_E2E=1`) | `services/agent/tests/test_inspection_acceptance.py` |

Fixture: `uv run python -m evals.sites.public_pages.server --port 8811` (rated,
unrated, hostile, escape, dynamic, replacing, restless, redirects, download,
binary, missing, slow).

## Known limitations

* Public destinations must be allowlisted (see above). There is no egress proxy
  yet, and residual DNS-rebinding risk is documented rather than eliminated.
* The browser does not fetch pages itself: every request is replayed by
  Playwright's driver (`route.fetch`). Sites with bot protection may refuse
  it. A LeetCode problem page returned HTTP 403 in the manual smoke test, and
  Lumi reported that honestly.
* Long pages are cut at 200 blocks or 12,000 characters, and the card says so.
  Content outside that window cannot be used as evidence.
* "Settled" is a heuristic, not a guarantee. Content rendered after the quiet
  window is missed, and a page that never stops changing is marked unsettled.
* The grounding check proves that quotes and numbers come from the page. It
  cannot prove the model paired the right label with the right value. The
  prompt requires label-and-value quotes, and the evidence is shown so the
  user can check.
* Headless Chromium is not an OS sandbox (see the architecture document §A).
