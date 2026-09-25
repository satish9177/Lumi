# Milestone 12 S3: account read + real manual handoff

> **The existing account-read authority is reused, never re-invented.** `account_read` composes over
> Milestone 8a S3's own scope card, grant, profile/account-fingerprint/revoke-epoch checks and credential-
> surface detection -- all unchanged. What S3 adds is the orchestration-level plumbing: a trusted
> `account_context_ref` naming which already-authenticated profile a step may read under, a real translation
> of that task's own deterministic pauses into `manual_handoff_required`, and a genuine Continue that always
> re-observes through a fresh, real step rather than ever assuming the human's part of a handoff happened.

```text
Migration head          0025
```

## What this slice adds

* **`account_read` composed as a task-backed capability**, exactly like `public_research`: the orchestrator
  creates a real `authenticated_read` task through `AuthenticatedReadService`'s own existing boundary
  (`OrchestrationCoordinator.dispatchAccountRead` -> `AgentTaskController.createAccountReadTask` ->
  `createAuthenticatedTask`), and `_read_task_backed_resolution`'s new `account_read` branch only ever reads
  that task's own current state (`describe()`). Selecting `account_read` is never itself approval: the linked
  task's own scope card still gates every read, and the first pause after linking is the ordinary
  `approval_required` (grant `PENDING`), not `manual_handoff_required`.
* **`account_context_ref`** (`app/domain/orchestration_resources.py`'s `REGISTERABLE_RESOURCE_KINDS`): minted
  only from a trusted renderer action (`OrchestrationCoordinator.attachApprovedAccount`, mirroring
  `attachApprovedDocument`), after `AuthenticatedReadService.check_profile()` -- the same deterministic check
  a direct account-reading request already passes through -- confirms the profile is signed in, not deleted,
  not mid-takeover and not leased by another runtime generation. The model never creates or relabels one; its
  `safe_label` is a fixed template naming only the profile's own already-configured `site`.
* **`account_result_ref`**: minted on a succeeded `account_read` step, `privacy_class: "private"`, carrying a
  **template-only** label and (after a fix; see Adversarial review below) a **template-only** step summary --
  the answer's closed-vocabulary status and a quoted-evidence count, never the answer text itself.
* **`manual_handoff_required` made real.** The linked task's own `login_required` / `account_changed` /
  `account_identity_unknown` / `left_site_scope` pauses (all detected by the unchanged M8a subsystem) map
  onto this one orchestration-level reason. There is deliberately no separate CAPTCHA signal: a CAPTCHA-
  guarded sign-in already shows a credential surface (at minimum a password field), which the existing,
  over-inclusive `login_required` detection already catches -- verified in review to fail safe even for a
  bot-check interstitial with **no** credential surface at all, because the account-identity fingerprint
  check is unconditional and a missing signal is itself treated as `account_identity_unknown`, before any
  page text is ever read.
* **`pending_note`** (migration `0025`, `orchestration_steps.pending_note`, DB-CHECK-enforced non-null only
  while `PENDING`/`AWAITING_APPROVAL`, cleared to `NULL` by `resolve_step`): a bounded, controller-authored
  safe instruction ("Manual action required: sign in ... completing any verification ... including a
  CAPTCHA ... choose Continue.") for a still-unresolved manual-handoff step, kept structurally distinct from
  `result_summary` (which only ever describes a *resolved* step) so the cockpit can show a real instruction
  without opening the linked task's own panel, and so it can never be confused with -- or accidentally
  contribute to -- a step's result.
* **A real Continue.** `OrchestrationCoordinator.progressPendingStep` now nudges `AgentTaskController
  .continueAccountRead(taskId)` -- but *only* when paused for `manual_handoff_required`, never for the
  ordinary `approval_required` wait. `continueAccountRead` attempts exactly one fresh, forced `observe`
  against the real, linked task (the one place a step may be attempted while the task is still `PAUSED`),
  then either continues the planner loop (if it actually cleared) or reports whatever it actually finds --
  including "still paused" or "the account changed; refused." It never assumes the click itself is evidence
  of anything.
* **Cockpit UI** (`OrchestrationPanel.tsx`): a prominent "Manual action required" block (using the paused
  step's own `pendingNote`) alongside the existing Continue/Stop controls, and a minimal account picker
  (signed-in profiles only) with an "attach" action -- without this, `account_read` would be unreachable from
  the running app at all, since it needs a real `account_context_ref` before the planner can ever choose it.

## Adversarial review

One fresh, independent Claude review (general-purpose agent, no prior session context), focused on: account/
profile confusion, cross-task ref replay, private-data leakage, provider routing, prompt injection, Continue-
as-fake-success, handoff expiry, restart, wrong-account transition, CAPTCHA automation, and grant/resource
aggregation. **Two High findings, both confirmed against the actual code and fixed in this same pass:**

1. **Account-private answer text leaked into a second, undisclosed AI recipient's context.**
   `_read_task_backed_resolution`'s `account_read` branch originally built the step's `result_summary` from
   `f"{answer.status}: {answer.answer}"` -- the actual synthesized answer text, up to 600 characters,
   embedded verbatim. `result_summary` is exactly what `orchestrationResultLines()` feeds to the
   **orchestration-planning** model on every later planner tick, whose recipient is resolved independently
   (`PRIVATE_TASK_CLASSES`, one pinned provider) from the recipient the user actually confirmed on the
   account-read task's own scope card. The user's "Allow" click authorizes disclosure to that one named
   recipient; it says nothing about a second, differently-configured model seeing a synthesis of their
   private account content. **Fixed:** the summary is now template-only, exactly matching `document_read`'s
   own character-count pattern -- `"Account read finished: {status} ({N} quoted item(s) of evidence)."` --
   never the answer text. New regression assertions in `test_orchestration_account_read.py` check the exact
   answer text, private repository names and quoted block text are all absent from `result_summary` in both
   the "answers directly" and "answers after a real Continue" test paths.
2. **A premature Continue press could revoke an otherwise-healthy grant.** `continueAccountRead`'s error
   handling originally revoked the grant whenever a forced re-observe raised **either**
   `authenticated_not_granted` (`AuthenticatedGrantNotUsableError`: the grant is truly, permanently dead --
   the account changed, or it expired) **or** the much broader `authenticated_unavailable`
   (`AuthenticatedProfileUnavailableError`: covers `profile_not_authenticated` -- the person simply has not
   finished signing in yet -- and `profile_takeover_active` -- a sign-in is literally in progress right now).
   Since the primary hero scenario (login handoff) reaches `profile_not_authenticated` on *every* Continue
   pressed before the person finishes, the bug meant the very first premature Continue would revoke the
   grant and force a brand-new scope card, defeating "Continue re-observes and can be pressed again." **Fixed:**
   the revoke now fires only for `authenticated_not_granted`; `authenticated_unavailable`'s whole family
   leaves the pause in place, retryable, exactly as intended. New tests in
   `agent-continue-account-read.test.ts` (previously zero coverage existed for `continueAccountRead` at all)
   pin this down for both `profile_not_authenticated` and `profile_takeover_active`, confirm the grant stays
   `ACTIVE` and a subsequent real re-observe still succeeds, and separately confirm a *truly* dead grant
   (`authenticated_grant_not_usable`, independent of the earlier `ACTIVE` read -- simulating the exact race
   the fix targets) is still revoked with reason `grant_unusable`, exactly once. Reverting either fix by hand
   reproduces the corresponding test failures, confirmed before restoring both.

Two Informational notes, not fixed (inherited, unmodified behavior, not S3-specific):

* `createAccountReadTask` repoints "the active task" pointer (`store.write`), identical to
  `createResearchTask`'s own existing behavior for `public_research` -- a pre-existing product characteristic
  of every task-backed capability, not new here.
* The orchestration's own 30-minute TTL is one fixed budget shared between all planning work and the human's
  own login/CAPTCHA time; confirmed a real bound exists on the Continue path itself (`resume()`'s
  `is_live()` check, not just at initial dispatch), so a `manual_handoff_required` pause cannot be continued
  forever -- worth knowing that a long-running orchestration reaches the pause with less of that budget left,
  not a defect.

**Confirmed sound** (verified against the actual code, not assumed): CAPTCHA/bot-check interstitials with no
credential surface still fail safe (the account-identity fingerprint check is unconditional and a missing
signal is `account_identity_unknown`, checked before any page text is read); cross-orchestration
`account_context_ref` replay is refused (same orchestration-scoped resource lookup already proven for
`document_ref`); selecting `account_read` is never itself approval, in both Python and TypeScript; no CAPTCHA
automation surface exists at all -- the closed `AuthOperation` vocabulary has no click/type/submit primitive,
structurally, not by convention; worker-detected wrong-account handoffs correctly revoke and never silently
continue, with a fresh `prepare()` afterward requiring a brand-new grant; the renderer cannot reach
`createAccountReadTask`/`continueAccountRead` directly (no IPC channel exists for either -- only
`attachApprovedAccount`, a trusted user-initiated action, and the pre-existing `listBrowserProfiles` read,
are IPC-reachable); all new durable state is real Postgres, so a restart mid-handoff loses nothing;
concurrent/double-click Continue presses are serialized by `exclusive()`'s busy-lock, never raced; the new
`pending_note` CHECK constraint cannot be violated by any code path (`_commit_step` only ever sets it while
unresolved, `resolve_step` unconditionally clears it, and `update_pending_note`'s own `WHERE status = :status`
makes a concurrent resolve-vs-refresh race a safe no-op); migration `0025` is a clean, low-risk, correctly
chained add/drop pair with no data-corruption path; no prompt-injection path exists from page content into
capability selection, authorization or handoff status -- the planner only ever sees server-computed
`available_capabilities` and controller-authored `resultLines`/`facts`, never raw page text, a URL or an
identity signal.

## Validation

* `uv run mypy app tests`: clean (312 source files).
* `uv run pytest tests/test_orchestration_account_read.py tests/test_orchestration_service.py`: 71 passed.
* `uv run pytest -m "not browser and not desktop_uia"`: 2,586 passed, 386 deselected; the only failure is the
  pre-existing, documented environment baseline (`test_booking_routes_without_a_worker_answer_503`),
  unrelated to this diff. (An earlier run of the same command, executed concurrently with an unrelated
  `test_schema.py` invocation against the same Postgres test database, corrupted that database's schema out
  from under itself -- `task_grants.approval_input_tick`, added by migration `0016`, went missing while
  Alembic's own version marker still claimed head. Dropped and recreated the test database, re-ran migrations
  clean, and re-ran the full suite in isolation to get this trustworthy result; not a defect in this diff.)
* `npm.cmd run typecheck`: clean.
* `npx vitest run`: 2,593 passed (up from the pre-slice baseline by the 3 new `agent-continue-account-read
  .test.ts` cases plus this slice's other new/updated tests); the only failing files are the three
  pre-existing, documented, machine-specific baselines (`real-inference`, `tokenizer-pack`, `accessibility`
  scam CSS), unrelated to this diff.
* `npm.cmd run build`: main/preload/renderer all build clean.
* `pytest -m desktop_uia` and `npm.cmd run package:dir` remain deferred to the M12 final validation pass, per
  S1/S2's own established practice (this slice touches neither surface).

## Documented residuals, carried forward

* **S2's deferred capabilities are unchanged**: `inspect_public_page`, `download_document`,
  `place_downloaded_file` remain `capability_unavailable`, per `docs/reviews/milestone-12-s2.md`'s own
  rationale. Nothing in this slice touches that decision.
* **Document picker/cockpit UI remains outstanding** for final M12 completion (`document_read`/
  `document_compare` are still reachable only via direct IPC/API calls, not from the running app's own
  screen) -- `account_read`'s own minimal picker was built this slice only because S3's own acceptance
  criteria are otherwise unreachable from the real app, not because the document picker's own scope changed.
* A task-backed capability's linked child task reaching a `CANCELLED` status (as opposed to a revoked grant
  or a stopped orchestration) is not specifically handled by `_read_task_backed_resolution` for *any*
  capability, `account_read` included -- a pre-existing characteristic shared with `public_research`/
  `project_start` since M11, not introduced or widened here. No entry point reachable today cancels an
  `authenticated_read` task independently of revoking its grant, so this is not currently triggerable for
  `account_read`.
