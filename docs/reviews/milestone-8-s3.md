# Milestone 8a S3 — authenticated account reading

Implementation report. Branch `lumi-agent-v2`.

> **S3 lets an AI model read pages of a signed-in account, once, after a
> trusted permission click, through one named provider, for a bounded number of
> GET/HEAD requests. It is `account_scoped_read` with effect `ACCOUNT_READ`. It
> is not "read-only", "no-effect" or "invisible": a website may record the visit.
> S3 has no form observation, no field writes and no submission.**

| | |
|---|---|
| Starting SHA | `8600e8c0ce57c1d3d5096387c3c09cca8c8b6702` (M8a S2 complete, including the capture restart closure) |
| Implementation SHA | `44eeabc` (`feat(agent): add account-scoped authenticated reading (M8a S3)`) |
| Documentation SHA | `6a2ae641b1e068fe0505b735fc08427b1de9a960` (recorded here by the following correction commit) |
| Migration | `0007` → `0008` (only S3 needs: `task_grants` gains `profile_id`, `profile_revoke_epoch`; new `authenticated_observations`, `authenticated_answers`) |

## 1. Real-account acceptance was intentionally not performed

The architecture plan originally called for a real GitHub authenticated-read
manual pass. It was intentionally not performed because Authenticode remains an
unresolved release gate. All S3 authentication/read acceptance used synthetic
accounts only. This is a release-gated validation deferred by design, not a
failed test. No real password, one-time code, session token or personal account
was typed into this build.

## 2. What was built

```text
signed-in profile (S1/S2) -> typed request -> trusted permission card
   (side-effect warning, ONE provider, no failover, reduced identifiers)
   -> Allow (trusted click; voice cannot grant)
   -> planner (navigate/observe/reveal/tab/history/stop only)
   -> worker: in-site + credential-surface + identity checks, GET/HEAD only,
      redaction inside the worker, bounded text
   -> grounded answer with citations, recorded for the approved provider only
```

## 3. Account-identity spike result

No site-agnostic signal exists that Lumi may use without reading cookies or
storage, inspecting Chromium credential files, or using the profile label or a
hash of the profile id. The only reviewed signal is the synthetic fixture's
`data-lumi-account-id` element (the S2 fingerprint). Rule, enforced in the worker
before any text is read: fingerprint known and matching continues; unknown gives
`account_identity_unknown` (pause, no provider call); mismatch gives
`account_changed` (revoke epoch bumped, grants invalidated, pause). No GitHub
adapter was added. Consequence: a real site without a reviewed identity signal
pauses as `account_identity_unknown` and cannot be read.

## 4. Requirements and where they are proven

1. **Capability name and effect.** `account_scoped_read`, `ACCOUNT_READ`; registry
   invariants refuse `ACCOUNT_READ` on any non-authenticated target.
2. **Disclosure before Allow.** The card states the website-side-effect warning,
   the provider, the character bound and "does not make it anonymous". Electron
   acceptance and `agent-authenticated-view.test.ts`.
3. **No anonymity claims.** Redaction hides email, card (Luhn), phone and long
   digit runs; copy says it is not anonymisation.
4. **One provider, zero failover, `max_vision_calls = 0`.** Chosen by trusted
   main config and a stable recipient id; the router breaks after the first
   attempted provider, including `invalid_output`.
5. **Voice cannot grant.** `grantAuthenticatedScope` is absent from
   `VoiceTaskBackend` (structural test). Typed requests need no voice.
6. **Planner vocabulary.** Six operations; no URL, selector, JS, provider or
   click fields (strict parser, 49 tests).
7. **Guard.** GET/HEAD only, same-site top-level navigation (`left_site_scope`),
   per-hop redirect validation, downloads/WebSockets/popups blocked, S0 broker
   retained.
8. **Credential surface.** Signals-only result, then `login_required` and the
   profile goes to `NEEDS_LOGIN`.
9. **Classification firewall.** Tasks and evidence are `account_private`; no
   leakage to public research context, episodic memory or diagnostics.
10. **Storage.** Separate authenticated evidence and answer tables; no raw URLs
    persisted; `browser_dispatches.result` keeps only stable keys.
11. **Lost reads.** An `OUTCOME_UNKNOWN` read is never auto-retried; only a fresh
    `observe` may follow.
12. **Grant consumption.** Single conditional `UPDATE`, also checking profile
    status, revoke epoch and fingerprint.
13. **Pauses decided by code.** `login_required`, `account_changed`,
    `account_identity_unknown`, `left_site_scope`.
14. **Fixture question.** "Which of my repositories are private?" is answered with
    citations (service-browser test).
15. **Side-effect honesty.** The fixture's `read_count` stays 0 before Allow and
    increases only afterwards.

## 5. Test results (this working tree)

| Suite | Result |
|---|---|
| `uv run mypy` | success, 171 source files |
| `uv run pytest -m "not browser"` | 977 passed, 1 baseline failure (see §6), 263 deselected |
| Browser: `test_authenticated_browser.py` + `test_authenticated_service_browser.py` | 37 passed (27 worker-level, 10 real database + real worker + real Chromium), run sequentially |
| Electron acceptance `test_authenticated_acceptance.py` (`LUMI_ELECTRON_E2E=1`) | 1 passed |
| `npm.cmd run typecheck` | clean |
| `npm.cmd run build` | clean |
| `npm.cmd test` | 2105 passed, 22 skipped; failures listed in §6 |
| `npm.cmd run eval` | 118/118 |
| `test_desktop_contract.py` | passed (after declaring the S3 error codes) |

Electron acceptance limit: the desktop worker judges site scope with the pinned
Public Suffix List, which refuses loopback IPs, so no desktop profile can be
bound to a fixture site. Acceptance therefore proves the card, decline, and the
trusted Allow path (grant, one worker read, an honest "could not verify" answer
for the approved provider, no research rows, released profile). The positive read
is proven in the service-browser module with the site comparison patched.

## 6. Known baseline failures and flakes (not caused by S3)

- `accessibility.test.tsx` (stale CSS whitespace).
- `vision/real-inference.test.ts`, `vision/tokenizer-pack.test.ts` (local model
  pack absent).
- `test_booking_preparation.py::test_booking_routes_without_a_worker_answer_503`
  (`.env`-dependent).
- `realtime.test.ts` timed out under full-suite load and passes alone (8/8).

## 7. Bugs found during S3 and fixed

- Cancelling an authenticated task crashed research grant parsing; research
  queries now filter `kind = 'public_research'`.
- Redacted observation text was copied into `browser_dispatches.result`; only
  stable keys are kept now.
- Tab ref `t4` was accepted by the parser; `AUTH_TAB_REF` restricts to `t1–t3`.
- An orphan task remained when the profile could not be read; the profile is now
  checked at task creation.
- Runtime error codes were undeclared in the desktop contract; declared and the
  contract JSON regenerated.
- Lost-worker test: the fake "kill" did not drop connections and its teardown hung
  on a swallowed Playwright cancellation; the helper now aborts connections and
  abandons hung teardown steps without awaiting them.
- A docstring in the authenticated operations module contained the word
  "keyboard" and tripped the no-key-press source scan; the docstring was reworded
  and the scan left unchanged.

## 8. Not done, on purpose

Form observation, form preparation, field writes, network-freeze workflows,
protected values and submission/handover (S4–S6), M9, M10 and M8b were not
started. Authenticode remains an open release gate.

## 9. Residual risks

- Identity gating only works where a reviewed identity signal exists; real sites
  currently pause as `account_identity_unknown`.
- Redaction is pattern-based and can miss identifiers (names, addresses).
- A website may record any read, which is why the card says so.
- Packaging: verified by an actual `npm.cmd run package:dir` run (§12); the earlier "reasoned, not run" note is superseded.

## 10. Detail for the previously terse requirements

**Authenticated grant schema (`AuthenticatedReadScope`, `app/domain/authenticated.py`).**
Frozen, `extra="forbid"`, `schema_version` 1, `policy_version`
`authenticated-read-v1`. Fields: `profile_id`, `site`, `allowed_origins`,
`allowed_operations` (unique, at most five), `methods` (only `GET`/`HEAD` are
representable), `classification = account_private`, the allowed/forbidden
summaries shown on the card, `website_side_effects_possible: Literal[True]`,
`disclosure` (one `recipient`, `max_text_chars`, `max_blocks`,
`identifiers_reduced: Literal[True]`, `failover: Literal["none"]`), `budgets`
(step, observation, planner, answer, tab and active-time caps, all upper-bounded;
`max_vision_calls: Literal[0]`), `account_fingerprint` (a SHA-256 digest, never
the raw identity string) and `profile_revoke_epoch`. `digest` is the SHA-256 of
the canonical JSON, so the scope cannot change between card and click.

**Trusted confirmation and compare-and-set.** `prepare` builds the scope without
opening a browser and inserts a `PENDING` grant, which authorises nothing. The
trusted Allow click calls `confirm_grant`, a single `UPDATE ... WHERE` that
requires the expected revision, `status = PENDING`, the same `scope_digest`
and the profile predicate below, then sets `ACTIVE`, bumps the revision and
sets an expiry. Zero rows updated means refusal; there is no read-then-write
window. Voice has no path to this call (`grantAuthenticatedScope` is absent from
`VoiceTaskBackend`, structurally tested).

**Profile, fingerprint and revoke-epoch binding.** One predicate,
`_profile_still_matches_grant()`, is reused by the confirming statement and by
the statement that consumes each step authorization: the profile row must be
`AUTHENTICATED`, its `revoke_epoch` must equal the grant's
`profile_revoke_epoch`, and its `account_fingerprint` must equal the fingerprint
stored in the grant scope. Writing it once means the two statements cannot
disagree. `account_changed` bumps the epoch, so every older grant fails in the
very statement that would spend a step.

**ACCOUNT_READ registry invariants (`app/browser/registry.py`, enforced at
registration).** (1) an `ACCOUNT_READ` operation may only target
`AUTHENTICATED_SESSION`; (2) an `AUTHENTICATED_SESSION` operation must be
`ACCOUNT_READ`; (3) it must use `RetryPolicy.OBSERVE_THEN_REPLAN`, never repeat
itself; (4) it must declare `Reconciliation.NOT_REQUIRED`, because nothing can
verify whether a site recorded a visit; (5) a `RESEARCH_SESSION` operation must be
`READ_ONLY` with the same retry policy. Violating any of these raises at
registration, not at run time (`test_browser_registry.py`).

**Research and authenticated sessions are different types.**
`ResearchBrowserSession` and `AuthenticatedSession` are separate classes with
separate id spaces. The worker refuses any dispatch whose id kind does not match
the operation's target, so a research session id cannot stand in for a profile
and a profile cannot stand in for a research session. Research queries filter
`kind = 'public_research'`; authenticated rows live in their own tables.

**`reveal`.** It resolves a link or block ref of the *current document epoch*
held in worker memory and calls `locator.scroll_into_view_if_needed(timeout=...)`.
It emits no key event, so a focused control is never fed a key, and a stale ref
fails with a stable code. A source-scan test forbids key-press APIs in the module
(the reason the docstring had to avoid the word "keyboard", §7).

**Grounding against the redacted projection.** `finish` calls
`verify_research_grounding` over this task's own stored observations, which are
the redacted text the provider actually received, and rejects an answer whose
provider differs from the grant's single recipient (`recipient_mismatch`).
Citations can therefore only point at redacted, stored evidence.

**Diagnostics privacy.** Runtime logs and task events carry ids, status codes,
counts (`redactions`, `evidence_count`, `observations_used`) and the provider name.
They carry no page text, link addresses, titles, identity strings, fingerprints
or answer text. Link addresses live only in worker memory for one document epoch;
`browser_dispatches.result` keeps only stable keys.

## 11. Validation closure results (this session, head `a165faa`)

| Check | Result |
|---|---|
| `uv run pytest -q tests/test_no_credential_extraction.py` | **15 passed** in 1.30 s |
| `uv run pytest -m browser`, one copy | **245 passed, 18 skipped, 0 failed, 4 warnings**, 30 m 47 s |
| Skipped (18) | the `LUMI_ELECTRON_E2E` / `LUMI_PACKAGED_E2E`-gated acceptance tests, run separately below |
| Warnings | the visible one is the known Windows `PytestUnhandledThreadExceptionWarning` (`httpx.ReadError: [WinError 10054]`) at a browser test's teardown (same keep-alive hazard as S2 §22); the other three were not itemised from the truncated log. No test failed |

**The first browser run was invalid, not a regression.** It ended `179 passed,
18 skipped, 66 errors` because Docker Desktop was not running, so PostgreSQL
refused connections (`ConnectionRefusedError: [WinError 1225]` from asyncpg).
The errors were in database-backed tests. Docker Desktop and the compose
PostgreSQL container were started and the whole suite was re-run once, giving the
result above. Only the second run is counted.

**Electron acceptance, one file at a time (`LUMI_ELECTRON_E2E=1`, after `npm.cmd run build`).**

| File | First pass | Final |
|---|---|---|
| `test_authenticated_acceptance.py` (S3) | 1 failed | **1 passed** |
| `test_login_takeover_acceptance.py` (S2) | 1 passed | **1 passed** |
| `test_electron_acceptance.py` | 2 passed | **2 passed** |
| `test_inspection_acceptance.py` (M7a) | 4 passed, 1 failed | **5 passed** |
| `test_m6_acceptance.py` | 4 passed, 1 failed | **5 passed** |
| `test_voice_acceptance.py` | 3 passed | **3 passed** |
| Total (final) | | **17 passed, 0 failed** |

Failures on the first pass, each classified:

- **S3 acceptance, `leased == 0`.** Not a leak. The test asserted the profile lease
  was gone the instant the answer row existed, but the answer commits *before* the
  worker finishes closing the profile's Chromium. A probe showed the lease
  clearing about 16 s later. This is a race in the test's assertion; the release
  order in `authenticated_read.py` is unchanged. Fixed in the test only: it now
  polls up to 30 s and still fails if the lease is never released. It then passed,
  including the end-of-run "no leftover `app.server` process" check. I did not
  establish why it passed at the original S3 commit; a slower Chromium close on a
  freshly restarted Docker/Windows session is the likely but unproven reason.
- **Inspection kill test and M6 scenario A.** Both failed at the desktop launch
  deadline ("the Lumi renderer never loaded", `test_electron_acceptance.py:291`),
  the launch-under-load hazard the README warns about, shortly after Docker
  Desktop had been started. Each passed alone and both whole files then passed on
  a re-run. Classified as a launch flake from re-runs; not root-caused.

## 12. Packaged build (`npm.cmd run package:dir`)

- **Build:** exit 0. Bundle verification ran: the bundle imports and contains no
  secrets; bundled Chromium 153.0.8010.12 (`chromium-1243`) launches headless and
  headed from the bundle alone (`headedVerified: true`). Runtime 654.5 MB (Python
  215.9, agent 3.1, browser 435.5).
- **S3 files present in `resources/agent-runtime/agent`:**
  `alembic/versions/0008_authenticated_read.py`, `app/api/authenticated_schemas.py`,
  `app/browser/account_read_guard.py`, `authenticated_session.py`,
  `operations/authenticated.py`, `app/domain/authenticated.py`,
  `app/domain/redaction.py`, `app/repositories/authenticated.py`,
  `app/services/authenticated_read.py`.
- **Packaged acceptance** (`LUMI_PACKAGED_E2E=1 uv run pytest tests/test_packaged_app.py`,
  synthetic demo clinic, scrubbed environment): **1 passed** in 112 s. The bundled
  runtime migrates a fresh database, so migration `0008` runs from the package.
- **No profile or credential material in the artifact.** No `Cookies`,
  `Login Data` or `Local State` file exists under `agent-runtime`; the only
  "profile" name matches are source modules (`profile_lock.py`, `profile_paths.py`,
  `profile_session.py`, `browser_profile.py`, migration `0006`); no `.env` file
  was found. No real account was used.
- **Authenticode** remains an unresolved release gate. Nothing here authorises a
  real-account pass.

## 13. Boundaries

No S3 boundary changed: one provider, zero failover, account-identity fail-closed,
the Authenticode gate, synthetic-only acceptance, the S2 capture guard and the
no-form/no-write boundary are untouched. The only file changed in this closure
besides this report is the S3 acceptance test's lease-release wait.
