# Milestone 8a S3 — authenticated account reading

Implementation report. Branch `lumi-agent-v2`.

> **S3 lets an AI model read pages of a signed-in account, once, after a
> trusted permission click, through one named provider, for a bounded number of
> read-only requests. It is `account_scoped_read` with effect `ACCOUNT_READ`. It
> is not "read-only", "no-effect" or "invisible": a website may record the visit.
> S3 has no form observation, no field writes and no submission.**

| | |
|---|---|
| Starting SHA | `8600e8c0ce57c1d3d5096387c3c09cca8c8b6702` (M8a S2 complete, including the capture restart closure) |
| Implementation SHA | `44eeabc` (`feat(agent): add account-scoped authenticated reading (M8a S3)`) |
| Documentation SHA | DOCSHA (recorded here by the following correction commit) |
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
- Packaging: the bundle carries bundled Chromium only; no profile files or
  credentials are part of it. This was reasoned from the S1/S2 packaging checks
  and S3 adding no binary; no new packaged-build run was performed for S3.
