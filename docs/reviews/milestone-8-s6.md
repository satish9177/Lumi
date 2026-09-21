# Milestone 8b, S6: network-frozen local form draft, discard and human handover

> **Lumi fills the form in its own browser with the network frozen, verifies the values are in the fields, and hands you the browser. Nothing was sent while Lumi was filling. If the form needs the network to accept a value, Lumi stops and tells you.**

> This does **not** mean Lumi works on every form, submits applications, saves a draft on the website, or knows whether a site accepted anything. Lumi never submits.

Status: **S6 implementation closed. M8b engineering implementation is complete.**

```text
Real-account release gate:        BLOCKED  (Authenticode: NotSigned)
Real-form shipping validation:    PENDING  (not attempted, and must not be, in an unsigned build)
M8b approved for real-account release: NO
```

M9 and M10 have **not** been started. Nothing here uses a real account, saved detail or form.

Roadmap: M8b - S4 done (element/form observation); S5 done (disclosure manifest / exact approval); **S6 done (network-frozen local draft implementation)**.

## 1. Starting point

Pushed S5 head `a4826d113e8f59ef40d3b33fb7cfb88c353ecd0a` (S5 implementation `8302eb73b0128e507f27538111fa9c4e366b3822`).

## 2. Final SHAs

| Commit | SHA |
| --- | --- |
| Implementation: `feat(agent): add network-frozen local form draft (M8b S6)` | `fb0bee2226afd786bf00a27ab1c544d0d7bebd5c` |
| Fix: `fix(agent): close every other window before thaw and fix S6 test baselines (M8b S6)` | `5c9ca3d85d84d765986772db0ab03ed975f84878` |
| Docs closure: `docs: close milestone 8b S6 implementation` | the commit that adds this file (a document cannot contain its own hash; see the final report) |

The fix commit exists because the first full browser run (see section 56) found four failing tests. Three were test expectations (the worker-route list; two zero-resolution baselines captured *before* the freeze, when Chromium's own background traffic can resolve a name; and the S5 browser acceptance, whose approve now correctly refuses without a preparation window). One was a **single, non-reproduced** failure of the discard test's zero-counter assertion: six single runs and three more full-file runs of that code passed. I could not prove it was a fixture artifact, and it pointed at a real race (a popup opened by the dirty page has a navigation request of its own; if the guard handled it after the thaw it would not be refused), so discard now closes **every other window of the context** and waits for the guard's own popup closes to finish before the network returns, and the test asserts only the blank tab remains. The final full browser run below is from the tree that includes this hardening.

## 3. Migration `0011`

`services/agent/alembic/versions/0011_local_form_drafts.py` (revises `0010`) adds exactly: `browser_dispatches.frozen_at` (nullable `timestamptz`) and the `form_drafts` table. Nothing historical is rewritten (`test_migration_0011_round_trips_keeps_history_and_enforces_its_constraints`: an old dispatch keeps `frozen_at IS NULL`, a `prepared_nothing` attempt result is untouched, the constraints refuse an arbitrary status, a malformed digest, non-array or more than twelve fields and a second live draft for one profile, and downgrade to `0010` removes exactly the S6 shape). Present in the packaged bundle (section 58).

## 4. Headed preparation-mode transition (why it exists)

Before the S5 manifest is built the user opens the **preparation window**: (1) the worker captures the current in-site page into its own memory (never returned, never a renderer/model/IPC field: the request carries only the profile's own bound site); (2) the runtime closes the headless read context; (3) the SAME persistent profile is reopened **headed**; (4) the worker navigates back to that page internally under the read guard, revalidating scope on the captured URL and on every redirect hop; (5) a completely fresh S4 observation is taken through the normal credential-surface / identity / in-site gates; (6) every proposal and approval waiting from the headless document is rejected. If a safe return is impossible the refusal is `preparation_destination_missing` and the user opens the form in the window themselves. No URL is accepted from any other source.

## 5. Why filling headless and reopening headed is forbidden

Closing a context destroys its DOM. A draft made headless and "kept" by reopening headed would be a draft that no longer exists, and any claim about it would be a claim about nothing. So a local draft can only be made in, and kept by, a headed preparation session; the worker refuses a LOCAL_DRAFT dispatch on a headless profile (`preparation_mode_required`), and refuses an observation it never issued for the current document and form (`stale_observation`), so an approval made from a headless page cannot be replayed against the headed one (`test_the_headless_approval_cannot_be_used_after_preparation_mode_begins`).

## 6. Historical S5 `prepared_nothing`

`form-prepare-v1` manifests still parse and their digests still verify (history stays readable), but are never executable: `approve` refuses `legacy_manifest_not_executable`, and a pending v1 card is shown as superseded with only Cancel. New manifests are `form-prepare-v2`. A spent `prepared_nothing` approval is terminal; nothing scans or refills old rows (`test_a_historical_s5_manifest_is_never_executable`, `test_keeps_the_historical_S5_result_readable`).

## 7. `LOCAL_DRAFT` effect

`BrowserEffect.LOCAL_DRAFT`: writes only to browser-local authenticated form controls while the browser is under the verified S6 freeze. `PREPARE` (the booking adapter's) is not overloaded.

## 8. Registry invariants

`LOCAL_DRAFT`: target `AUTHENTICATED_SESSION`, retry `NEW_APPROVAL_REQUIRED`, reconciliation `NOT_REQUIRED`. `ACCOUNT_READ`: `AUTHENTICATED_SESSION`, `OBSERVE_THEN_REPLAN`, `NOT_REQUIRED`. No authenticated operation may declare reconciliation. Enforced at registry construction and tested; `CONSEQUENTIAL` booking behaviour is unchanged.

## 9. `form_drafts`

`id, task_id, profile_id, action_id, attempt_id, dispatch_id, manifest_digest, draft_digest, observation_id, tab, document_epoch, form_epoch, form_ref, status, revision, created_at, updated_at, fields JSONB`. Status is a closed CHECK: `PREPARED | STALE | DISCARDED | HANDED_OVER`. `fields` holds refs, identity hashes, approved digests and verified-local-value hashes only, at most twelve. No value column, selector, locator description or label. `revision` is the compare-and-swap token for the trusted clicks. Partial unique indexes allow at most one live (`PREPARED`/`STALE`) draft per profile and per task. `attempt_id` and `dispatch_id` are unique.

## 10. `browser_dispatches.frozen_at`

Written once by `BrowserRepository.mark_frozen`, a compare-and-set that requires the same dispatch, still `DISPATCHED`, on the worker generation that proved the freeze, with `frozen_at` still NULL. It is called only after the worker's freeze proof and before the first write, and never after a write: it is never back-filled.

## 11. Durable ordering

`begin_exact_execution` grants, claims and starts the approval in one transaction (attempt durable, action `EXECUTING`) -> the dispatch row is inserted (`frozen_at NULL`) -> the worker is asked to enter the freeze -> the worker proves it -> `frozen_at` is recorded -> the account is re-checked from the database -> ONE dispatch writes every field. No transaction is open while a browser is driven. The service test's worker stand-in queries the database **from its own connection at the instant the write request arrives** and asserts the attempt is open, the dispatch row exists and `frozen_at` is already set (`test_the_worker_is_asked_to_write_only_after_frozen_at_is_recorded`).

## 12. Playwright guard freeze

`AccountReadNetworkGuard`: a `frozen` flag checked at the very top of `_handle`, before the request is inspected, fetched, resolved, proxied or followed. A frozen guard refuses with a per-reason counter and touches nothing else.

## 13. Guard in-flight accounting

`in_flight` is incremented in the same synchronous step as the freeze check (no `await` between) and decremented in `finally`, so a request is either refused at the door or counted, never neither. `freeze()` sets the flag first, then waits, bounded, on an event for the counted requests; it stays frozen even if it times out (the caller decides to thaw). Unit tests use a route stand-in whose upstream fetch stays open: counted from entry, refused before fetch when frozen, waited for, and a 20-request race where every route is either fetched or aborted.

## 14. Broker freeze/drain

`EgressBroker.freeze_and_drain(timeout)`: mode `FROZEN` is set **first**, then every relay task is cancelled (closing both sockets in `_splice`'s `finally`) and the set is awaited to empty, cancelling again if a task slipped in. A connection that arrives while frozen is refused **before it is registered**, so `active_connections` is a fact about relays that exist. Entry requires `active_connections == 0`.

## 15. Existing tunnel closure

`test_an_already_open_tunnel_cannot_carry_another_byte_after_freeze`: a CONNECT tunnel is opened and carries a byte; after `freeze_and_drain` the sink server has received **zero new bytes**, the tunnel is closed, the resolver was called zero times and the dial count did not move. The negative control `test_freeze_alone_would_have_left_the_tunnel_open` shows `freeze()` without the drain leaves the tunnel carrying bytes, which is why the drain exists.

## 16. Freeze owner

`FormFreezeController.owner = (profile_id, dispatch_id, worker_generation)`. While it exists every other dispatch, profile open, takeover, research session and public read is refused (`freeze_owned`; `form_is_dirty` for the owner's own profile). Release is reachable only through the discard and handover lifecycle functions, each bound to the owner's dispatch id. There is no generic network-mode call (scanned).

## 17. Freeze verification

Worker `POST /v1/profiles/form-freeze` returns safe metadata only: status, `guard_in_flight`, `broker_active_connections`, `resolution_count`, `dial_count`, `freeze_duration_ms`. Order: page quiet -> guard frozen -> guard in-flight zero -> page-level pending zero (current document only, so a request a departed document abandoned cannot hold the freeze) -> broker frozen and drained -> proof. Any failure restores the open state (broker, then guard) and the page was never touched. A streaming request open at entry gives `page_never_settles` and changes zero fields (`test_an_open_streaming_request_is_never_frozen_over`, including that a fresh attempt succeeds once the guard's own fetch has ended).

## 18. Raw protected-value execution path

`protected_values` -> trusted runtime memory (read under a share lock **in the approving transaction**, canonical value re-hashed and required to equal the manifest `value_digest`, else `protected_value_changed` with nothing consumed) -> loopback worker request -> the frozen DOM. The exact approved bytes are used; a later change to the saved value does not affect this attempt. The dict is cleared in `finally`.

## 19. Raw-value firewall

Planted markers are asserted absent from: every table except `protected_values` (actions, approvals, task events, dispatches, drafts, attempts, grants, observations, tasks), captured logs at DEBUG, the worker's result, the `repr` of the execution model (`repr=False`), and all provider requests (the controller test records every provider call). Only the worker request carries them (asserted present there: the worker gets the exact bytes).

## 20. Exact manifest recheck

At approval: the S5 guard (grant, account and epoch, origin, protected-value digests, observation currency), the digest re-check above, `preparation_mode_required` if the manifest's observation was not recorded in the current preparation window or the worker generation changed (checked before anything is consumed), and `legacy_manifest_not_executable` for v1.

## 21. Worker-live element revalidation

Immediately before every write: same tab, document epoch and form epoch; the element ref resolves; a **fresh** value-free inventory is built, its fingerprint equals the observed one, the element is found at the same ref, its identity hash (recomputed with the same function the manifest used) equals the approved one, and for a choice the option identity hash matches. Control type, `enabled`, `read_only`, `visible` and `submit_like` are re-checked. Mismatch is `element_changed`; no nearest or fuzzy match, no adaptation, no model call mid-fill.

## 22. Three field primitives

`set_value` (`fill`) for text, email, tel, number and textarea; `select_option` by reviewed option ordinal for `select_single`; the radio member by the same ordinal via a read-only DOM helper mode plus a check; `set_checked` for a checkbox. All inside one approved dispatch, in `app/browser/local_form_draft.py` only. No Enter, no key press, no per-keystroke typing.

## 23. Local value verification

After each write the control is re-derived (never the handle that wrote it), read back inside the worker and required to equal the approved value; its SHA-256 (or a canonical state/selection hash) is the only thing returned or stored. A native-ness check refuses ARIA-only widgets. Text verified hashes equal the approved digests, so the runtime re-checks the worker's word.

## 24. Structure and re-render handling

After each write a value-free inventory is rebuilt and its fingerprint compared. A change stops the run: remaining refs are never re-mapped, the tab's element refs die, and the structure the page was left in is recorded so a partial draft can still be verified later. If the page also tried (and was refused) to use the network during the write, the code is `unsupported_under_freeze`, otherwise `element_changed`.

## 25. `unsupported_under_freeze`

Returned for: a controlled input the page resets without the network; a dependent select whose options need the network (the country write succeeded, the states did not); asynchronous validation (`aria-invalid` on a control just written); a missing network-loaded option. Server counters for the validation and states endpoints did not move after the freeze: the network was never turned on for one request.

## 26. Partial draft behaviour

A stop after at least one verified write leaves the page dirty and frozen: draft `STALE`, action `FAILED` (known: nothing was sent), task paused `form_draft`, card "Lumi could not finish this form ... needs manual review", controls Discard / Hand over only. A stop before any verified write destroys any page a primitive touched while frozen and then thaws (no draft row); a stop before any primitive is a plain thaw.

## 27. Successful `PREPARED`

All approved fields verified: draft `PREPARED`, attempt `SUCCEEDED`, task paused `form_draft`, the network still frozen, planning does not resume.

## 28. Draft digest

`SHA-256(canonical JSON)` over task, profile, action, manifest digest, dispatch, observation, tab, both epochs, form ref, draft status and every field's identity hash, approved digest / option identity / checked state and verified-local hash, ordered by element number; `written_at` is excluded. Tests mutate each input and assert every digest differs. No raw value is an input.

## 29. Dirty restrictions

From the first primitive the worker session is dirty: `observe`, `navigate`, `history`, `tab` open/close and a normal profile close are refused (`form_is_dirty`), tested directly against the operations and the store. The runtime independently refuses a read step, a profile close, `prepare_scope`, `planning_context` and a takeover for that profile (worker call count asserted unchanged).

## 30. No provider after write

The runtime has no provider client and none is reachable from freeze entry, fill, verification, discard or handover. The Electron controller test records every provider call: zero between the fill and the completed hand-over, and no provider request contains a raw marker.

## 31. Safe discard ordering

Verify frozen (re-freeze if anything was not) -> open a blank tab -> close every dirty tab with `run_before_unload=False` -> assert `is_closed()` -> clear draft ownership -> broker open -> guard open -> mark `DISCARDED`. The fixture page runs a 250 ms GET autosave timer: during the frozen fill and before discard the server's autosave counter is 0; after discard, for 1.2 s, still 0 and the old page `is_closed()`. The negative control thaws the same live page and the server DOES receive the autosave, so the test has teeth. The same holds end to end in `test_headless_to_headed_preparation_fill_verify_and_discard`.

## 32. Handover exact approval

Action tool `handover_form`, R2, in the existing action + approval framework (no parallel table). Its proposal is safe references only: task, profile, draft id, draft digest, site display, field count, partial flag. The approval guard requires a live draft, the same digest, profile and task, the registry to show that draft dirty on the same worker generation, the profile authenticated, and the revoke epoch unchanged. The generic action routes refuse both tools.

## 33. Live draft verification before thaw

`handover_draft` re-reads the live page: the worker's record must equal the approved per-field hashes exactly, the structure must be the one the page was left in, and every re-derivable field must still hold its value. A human who changed a field while frozen gets `draft_changed` with the network still frozen and the approval spent (`test_a_human_who_changed_the_frozen_page_cannot_be_handed_over`, real headed Chromium).

## 34. `user_takeover` semantics

After approval: the wide human-mode guard is installed first (while the broker is still frozen), then broker open, then the read guard opened and removed, then the existing page is brought to front. The browser is never closed or reopened; draft `HANDED_OVER`; task paused `user_takeover`; the network is verified to work for the human afterwards and the server's submission counter stays 0. The copy is "You took over in the browser window. Lumi did not submit anything and cannot tell you whether the site accepted or saved it." No later inspection is used to infer success.

## 35. No submission guarantee

No operation exists for a button, link, submit-like control, Enter, `form.submit`, `requestSubmit`, click, upload or arbitrary invoke. A tampered persisted state naming a submit-like element is refused (`unsupported_control`) with zero click/submit events. Fixture server: `submissions == 0` in every test.

## 36. Crash with `frozen_at`

Recovery reads it: `OUTCOME_UNKNOWN`, event `remote_effect: impossible_under_verified_freeze`, `local_state: lost`, task paused `browser_lost`. `test_a_crash_is_read_through_frozen_at[True]`.

## 37. Crash without `frozen_at`

`OUTCOME_UNKNOWN`, `remote_effect: unknown` (no claim), same pause. `test_a_crash_is_read_through_frozen_at[False]`. A lost freeze answer at runtime makes no remote-effect claim either.

## 38. No reconciliation / no retry

Registry: `NEW_APPROVAL_REQUIRED`, `NOT_REQUIRED`. A lost fill answer, a lost freeze answer and a lost handover answer are each `OUTCOME_UNKNOWN`; the worker is called exactly once in each test; a spent approval cannot be used again.

## 39. Draft loss on restart

A `form_drafts` row is not a restorable draft. At startup every live row is closed `DISCARDED` and its task paused `browser_lost` with a `task.form_draft_lost` event; nothing is re-filled and no approval is reused (`test_startup_closes_a_live_draft_as_lost_and_never_refills_it`). The card warns before the fill: "These values are only in the browser window. If Lumi or your computer restarts, they are gone and you will need to prepare the form again."

## 40. Credential / account checks

Before freeze (worker preparation gates and runtime), again from the database immediately before the write dispatch (status, revoke epoch, fingerprint), and in the worker before **every** write (in-site, credential surface, identity). A failure is `account_changed`, `login_required`, `account_identity_unknown` or `left_site_scope` with no write. After dirty, navigation and account switching are structurally refused.

## 41. Adversarial fixture

`GET /app/apply/draft` (`evals/sites/account_fixture/draft.py`): text, email, tel, textarea, country select, radiogroup, checkbox, React-like controlled input, network-dependent state select (`?dependent=1`), async validation (`?validate=1`), a value-resetting controlled input (`?reset=1`), form re-render after input (`?rerender=1`), a streaming request (`?stream=1`), an autosave timer (`?timer=1`); POST autosave on `input`, PUT on `change`, POST on blur, GET image / fetch / `sendBeacon` exfiltration, third-party exfiltration, DNS-name exfiltration (`https://<value>.exfil.invalid/`), popup on focus, JS redirect on input, hidden submit, visible submit, Enter handler, Continue and Save Draft buttons, a file input, prompt-injection page text. Password/OTP surfaces are the existing `/login`, `/login/otp` and `/app/apply/secure` paths.

## 42. All zero counters

During a successful frozen fill and while the draft waits frozen, on the fixture server: `submissions`, `autosave`, `blur_save`, `exfiltration`, `popup_hits`, `mutations` all 0, the second origin's `third_party` 0; broker `resolutions` and `dial_count` delta 0; `active_connections` 0; popups not adopted (one tab); no file chooser (the file input is never given a ref and never touched). The guard's refusal counter is **greater than zero**: the page really tried, and only the freeze said no. This is asserted at the worker level and end to end.

## 43. Open-tunnel test

Section 15.

## 44. Streaming-request test

Section 17.

## 45. Controlled-input test

The React-like controlled input passes (`fill` -> framework handler -> value stable -> verified hash correct) with the network frozen. `?reset=1` resets the value without the network: `unsupported_under_freeze`, page destroyed while frozen (nothing verified), no request reached the validation endpoint.

## 46. Dependent-select test

`?dependent=1`: with the network frozen the target state option cannot exist locally: `unsupported_under_freeze`, one field verified, page left frozen, `states_hits` unchanged, no temporary network access, no text typed into a select.

## 47. Re-render test

`?rerender=1`: writing the phone field replaces the whole form. Stops with a partial verified count, remaining refs not used (the later field's control does not exist), every element ref of the tab dead, still frozen, zero effects.

## 48. Discard autosave test

Section 31.

## 49. Handover test

Real headed Chromium: before the handover every server counter is zero; requesting the handover changes nothing; approval keeps the same page object, the values remain in the fields, the network works for the human afterwards, the task is paused `user_takeover`, the second approval cannot be reused. Autosave is deliberately not asserted zero afterwards: the user authorised the network's return, and the card says the page can now send.

## 50. Source scan

`tests/test_local_form_draft_source.py` runs a receiver-aware scanner over the whole worker tree: every file except `local_form_draft.py` (and the booking adapter and public-research scroll, other milestones' own) contains none of `fill type press click check select_option set_checked set_input_files dispatch_event request_submit focus hover clear drag ...`; `local_form_draft.py` may contain exactly `fill`, `select_option`, `set_checked`, `check` (the scan proves they are all present, so the allowance does real work) and no evaluation of its own; the runtime-side modules contain no page action; the DOM helper stays static and read-only (its two S6 modes are asserted present); 21 planted violations are caught and ordinary methods (`policy.check`, `dict.clear`, `writer.write`) are not flagged. The worker's routes are asserted to be exactly the reviewed set.

## 51. Targeted test results

| File | Tests |
| --- | --- |
| `test_local_form_draft_browser.py` (worker + real Chromium) | 21 passed |
| `test_form_freeze_network.py` (broker + guard) | 9 passed |
| `test_form_draft_service.py` (real PostgreSQL, scripted HTTP worker) | 22 passed |
| `test_form_draft_browser.py` (worker + headed Chromium + PostgreSQL, end to end) | 4 passed |
| `test_local_form_draft_domain.py` | 22 passed |
| `test_local_form_draft_source.py` | 66 passed |
| `test_schema.py` (incl. migration `0011`) | 11 passed |
| S5 `test_form_prepare.py`, updated to attach a guard-only executor (its subject is the approval guard) | 63 passed |
| TypeScript `agent-form-draft.test.ts` | 23 passed |

## 52. `uv run mypy`

Clean, 203 source files.

## 53. TypeScript

`npm.cmd run typecheck` clean; `npm.cmd run build` clean. `npx vitest run`: **2197 passed, 22 skipped, 1 failed, plus 2 files that could not load.** The failure is the known `accessibility.test.tsx` stale-CSS baseline and the two unloadable files are the known `real-inference` / `tokenizer-pack` missing-model-pack baseline. They are not S6 regressions.

## 54. Eval

`npm.cmd run eval`: **118/118**, re-run on the final tree (a first run of 110/118 was caused by my own concurrent database tests, not by a change: the eval starts a real runtime and shares the database).

## 55. Non-browser suite

`uv run pytest -m "not browser"`: **1438 passed, 1 failed, 323 deselected.** The failure is the known `test_booking_routes_without_a_worker_answer_503` baseline (`browser_worker_unavailable` vs `browser_worker_not_configured`); nothing was changed to hide it.

## 56. Browser suite

One complete copy, nothing else running, from the committed tree that includes the fix commit: `uv run pytest -m browser` - **305 passed, 18 skipped, 0 failed**, 1439 deselected, 4 warnings, 42m17s. The 18 skips are the `LUMI_ELECTRON_E2E` / `LUMI_PACKAGED_E2E`-gated acceptance tests, run separately in sections 57-59. The warnings are the known FastAPI/Starlette 422 deprecation, Windows asyncio/proactor closed-transport warnings and the `httpx.ReadError [WinError 10054]` in the deliberate runtime-crash test.

The **first** full copy (before the fix commit) was `301 passed, 18 skipped, 4 failed` (section 2). It is recorded rather than hidden: it is what found the discard-race hardening.

## 57. Electron acceptance

Run sequentially, after `npm.cmd run build`, with `LUMI_ELECTRON_E2E=1`, one file at a time: `test_authenticated_acceptance.py` **1 passed**; `test_login_takeover_acceptance.py` **1 passed**; `test_electron_acceptance.py` **2 passed**; `test_inspection_acceptance.py` **4 passed, 1 failed** on the first pass (`test_main_restart_after_the_read_answers...`) and **5 passed** on an immediate re-run of the same file; `test_m6_acceptance.py` **5 passed**; `test_voice_acceptance.py` **3 passed**. The one first-pass failure is the launch-under-load flake `docs/reviews/milestone-8-s3.md` section 11 already recorded for these files (Electron launched while other suites' processes were still closing); it is not root-caused and was not reproduced.

**Limit, stated precisely.** The desktop app cannot bind a profile to a loopback fixture site (the pinned Public Suffix List refuses a bare loopback IP, as S3 recorded), so a *driven Electron session through the S6 cards* is not possible without weakening the production public-suffix/profile rule, which was not done. The S6 cards are exercised by the pure view-model tests, the fake-runtime controller tests (which also assert the network only returns through the second approval, that voice reaches none of it, and that no provider is called after the fill) and the real-browser Python acceptance (`test_form_draft_browser.py`), where the real runtime services, a real worker and a real headed Chromium run the whole chain. What Electron acceptance did prove: the new form-plan fields (`preparing`, `draft`, `handover`, `executable`) parse in a real desktop snapshot, and the six new routes are on main's exact allowlist at the same time they were implemented (S5 found a missing route only in Electron acceptance; `agent-form-draft.test.ts` now pins the six accepted routes and a list of nearby, parameterised and generalised rejections).

## 58. Package

`npm.cmd run package:dir` passed. Bundled Chromium 153.0.8010.12 (`chromium-1243`) verified to launch **headless and headed from the bundle alone** (`headedVerified: true`); runtime 655.3 MB (python 215.9, agent 3.9, browser 435.5); "no browser profile directory in the bundle" check passed; "the bundle imports and has no secrets" check passed. Migration `0011_local_form_drafts.py` is present under `resources/agent-runtime/agent/alembic/versions/`. No `Cookies` or `Login Data` file exists anywhere under `agent-runtime`, and no planted S6 marker (`SECRET_S6`, `S6_71A`) occurs in the packaged agent code. (The S6 tests, fixtures and `evals/` are not packaged.)

## 59. Packaged acceptance

`LUMI_PACKAGED_E2E=1 uv run pytest tests/test_packaged_app.py`: **1 passed in 35.10 s** on a re-run (a fresh database migrates through `0011` from the package). The first attempt timed out during the cold first launch of the freshly built 655 MB bundle (first-run scanning of new binaries); an immediate re-run passed. Synthetic fixtures only.

## 60. Authenticode

`Get-AuthenticodeSignature release\0.1.0\win-unpacked\Lumi.exe` -> **`NotSigned`**, no signer certificate. electron-builder's `signing with signtool.exe` lines in the package log are not a signature. The release gate and the **no-real-account / no-real-saved-detail / no-real-form gate remain open**; every test used synthetic fixtures and the gate was not weakened to make any S6 test easier.

## 61. Real-form shipping-gate status

The original design ships S6 only if (1) every adversarial network/exfiltration counter is zero **and** (2) at least three unadapted simple real-world forms complete. **(1) is met on the synthetic fixture** (sections 42-49). **(2) has not been attempted and must not be:** the packaged build is `NotSigned`, so no real account, real saved detail or real authenticated form may be used, and no three-form result is fabricated. Therefore:

```text
S6 implementation closure:                 DONE (synthetic adversarial proof)
S6 shipping / release gate:                NOT SATISFIED
Real-form release validation:              BLOCKED by the Authenticode gate
M8b approved for real-account release:     NO
```

A zero-counter result on the fixture demonstrates the freeze, not that a real site has no effect; on a real site Lumi cannot see the server's counters, so a real-form run would demonstrate usability only.

## 62. Scope confirmation

M9 and M10 were **not** started. No `site_interactive_form` capability, no submit, no upload, no key vocabulary and no planner-visible write exists. The planner vocabulary is unchanged (`navigate observe reveal tab history`) and asserted against sixteen write-shaped verbs.

## Other things worth stating

- **Decisions I made where the brief left room.** The click that fills is the existing `approveFieldDisclosure` channel (its card now says "Fill these fields") rather than a renamed `fillApprovedForm`: fewer channels, same trust. Six new trusted channels exist (`startFormPreparationMode`, `stopFormPreparation`, `discardFormDraft`, `prepareFormHandover`, `approveFormHandover`, `rejectFormHandover`), each an id and a revision, none on the voice backend (asserted).
- **Screen capture** is refused while a preparation window or a draft exists (`capture_refused_form_draft_active`), set from the controller's own view of the task, so a screenshot that would go to a model cannot include a dirty form. The S2 capture-restart closure is untouched. The worker has no screenshot API at all.
- **Residual: classification of a structural stop.** `unsupported_under_freeze` versus `element_changed` is decided by whether the page tried and failed to use the network during the write. On the adversarial fixture every write triggers refused requests, so a re-render there is also reported as `unsupported_under_freeze`. Both stop the run the same way and lead to the same card.
- **Residual: a refused request is counted, never inspected.** The guard reports how many requests the page attempted while frozen, not what they were.
- **Residual: handover leaves the read-mode WebSocket refusal in place** (Playwright has no removal call for it). It only closes sockets, so a live-chat socket on a handed-over page stays refused: a compatibility cost, not a risk.
- **Known test-rig fact.** The S3 rig signs in before the read session exists; S6 tests load the form *through* the guarded session, as production always does, so the page's own load-time requests are visible to the guard.
