# Milestone 8b, S5: protected values, form-planning disclosure and exact manifest approval

> **S5 is approval-only. It performs zero browser writes.** Lumi can know exactly what it *would* place into each approved field, but it still has no operation capable of placing anything into that field.

Status: **S5 closed.** S6, M9 and M10 had **not** been started when this was written. M8b itself was not complete.

> **Superseded in part by S6 (`docs/reviews/milestone-8-s6.md`).** Every statement below describes S5 *as it was closed*. Two of them are no longer true of the running product and are corrected where they appear: the saved value no longer stays in its row (S6 lets an approved value flow through trusted runtime and worker memory into the frozen browser), and approving a manifest no longer ends in `prepared_nothing` (it funds one network-frozen local draft). The historical `prepared_nothing` rows, and their `form-prepare-v1` manifests, are untouched and are **never executable**.

Roadmap: M8b - S4 ✅; **S5 ✅ disclosure manifest / exact approval**; **S6 next** (frozen local form draft).

## 1. Starting point

Closed S4 head: `f438b33d4da595594585d930cd57148af5583447` (S4 implementation `e46249e6f3983b85eadf45a3eb35ea5f9782a5ff`).

## 2. Final SHAs

| Commit | SHA |
| --- | --- |
| Implementation: `feat(agent): add exact form disclosure approval (M8b S5)` | see the final report (a document cannot contain its own hash) |
| Docs closure: `docs: close milestone 8b S5 disclosure manifest` | the commit that adds this file |

## 3. Migration `0010`

`services/agent/alembic/versions/0010_protected_values_form_prepare.py` (revises `0009`): creates `protected_values`; widens `task_grants.kind` to `form_prepare`; widens `profile_binding` to both profile-bound kinds; replaces the one-open-grant-per-task index with one per `(task_id, kind)`. Adds no draft table, freeze state, `frozen_at`, written-value hash or dispatch column. Downgrade deletes `form_prepare` grants and drops the table; nothing historical is rewritten (`test_migration_0010_round_trips_and_removes_only_its_own_shape`). Present in the packaged bundle.

## 4. `protected_values`

`id, kind (UNIQUE, CHECK in the eight), value, value_digest, preview, created_at, updated_at`. CHECKs: digest is 64 hex **and equals `encode(sha256(convert_to(value,'UTF8')),'hex')`** (the database refuses a mismatched digest), value length 1-300, preview non-empty.

## 5. The eight kinds

`legal_name`, `preferred_name`, `email`, `phone`, `city`, `country`, `linkedin_url`, `portfolio_url`. No free-form kind, password, one-time code, payment detail, file, blob or JSON.

## 6. Validation rules

Text only; NFC; ends trimmed; NUL, newline, tab, bidi/zero-width and every control or format character **refused, not dropped**; per-kind length caps (name/city/country 100, phone 32, email 254, URLs 300); email and phone shape; URLs must be http(s) with a host and no userinfo; LinkedIn must be on `linkedin.com`. Names are never "corrected": only runs of spaces collapse. An email's domain is lower-cased.

## 7. Masking policy (exact)

`legal_name` -> `saved legal name`; `preferred_name` -> `saved preferred name`; `email` -> `s***@g***.com` (first local character, fixed `***`, first domain character, fixed `***`, last label); `phone` -> `ending 1234`; `city` -> `saved city`; `linkedin_url` -> `linkedin.com/in/***`; `portfolio_url` -> `saved portfolio link`; **`country` -> the country itself** (a documented exception: a country cannot be masked and still choose the right option; the planning card says so whenever `country` is offered). Previews never depend on the value's length; `preview != value` is asserted for every other kind.

## 8. Plaintext-at-rest residual

Raw values are plaintext task data in Lumi's local runtime database. This is **not** encryption at rest and does not defend against a live compromise of the same Windows user; protection is the OS account and database access controls. It is not a credential store; browser sessions stay under the profile boundary.

## 9. Value digest

`SHA-256(UTF-8(canonical))`, computed by the service, re-derived by the database CHECK, bound by the manifest.

## 10. Raw-value exclusion surfaces

After saving, the value never left its row **in S5**: the repository returned only kind, preview, digest and length. **In S6 this is temporally superseded:** an approved value may flow only through trusted runtime memory and the loopback worker request into the locally frozen browser page (`ProtectedValueRepository.values_for_execution`, verified against the approved digest under a share lock in the approving transaction). It still never reaches the provider, the renderer, Electron main, an action proposal, an approval row, a dispatch row, a draft row, a task event, a diagnostic, a log line or an error payload. Tests plant `LEGAL_NAME_SECRET_S5_71A`, `EMAIL_SECRET_S5_82B@example.test`, `9300001234` and `PORTFOLIO_SECRET_S5_C4D` and assert zero occurrences in the planning context, the card, every table except `protected_values` (full-table text dumps: actions, approvals, events, observations, attempts, grants), captured logs, provider requests, the renderer snapshot, and the packaged artifact. The `PUT` response never echoes the value, and no route reads one.

## 11. The form-planning grant

A `task_grants` row of kind `form_prepare` with a strict frozen `FormPrepareScope`: profile, site, account fingerprint, revoke epoch, source account-reading grant, `planning_recipient`, `failover: none`, `recipient_origin`, `freeze_required: true` (a promise about S6; enables nothing), `allowed_data_refs` (subset of the eight), `max_fields <= 12`, `classification: account_private`. Immutable by the existing trigger.

## 12. Why a separate grant is necessary after S4

The user approved S3 account *text* disclosure, not up to 40 field labels, option labels and saved-detail previews; the later manifest approval is too late to authorise that model disclosure. Ordinary authenticated reading is unchanged (asserted with provider capture: no form marker, no preview).

## 13. Planning provider projection

Separate `form_planning` task class (private: one recipient required, no failover, no image). Carries form/element refs, role, control type, accessible name, required/enabled/visible/readOnly, `maxLength`, `submitLike`, option refs and labels, and masked previews of the selected refs. Not `valueState`, not current values, locators, selectors, ids, option values, frame URLs, digests, origin or fingerprint.

## 14. One recipient, no failover

The provider is copied from the account-reading grant. A failing provider stops the run with `model_unavailable`; the second provider records zero calls (test). A malformed or hostile reply is refused before anything is proposed.

## 15. `allowed_data_refs`

Chosen by the user through the trusted card; validated as a closed subset in main and again in the runtime; a proposal using any other ref fails `data_ref_not_allowed` before a card exists.

## 16. Trusted confirmation / CAS

`confirm_grant` is one statement checking status, revision, scope digest, profile `AUTHENTICATED`, same revoke epoch and fingerprint, **and** a live source grant. Tested: stale revision, changed account, revoked source, wrong task.

## 17. `prepare_form` schema

`{operation, observation, form_ref, entries[1..12]}`, each entry exactly one of `{element_ref, data_ref}`, `{element_ref, option_ref}`, `{element_ref, checked}`. `extra="forbid"`; parsed with stable codes (`no_entries`, `too_many_entries`, `unsupported_proposal`). It is a planner proposal, not a worker operation: the worker protocol, registry and operations contain no `prepare_form`.

## 18. Parse-time target refusal

Before any approval, card or action: duplicate/unknown element, another form's element, stale observation/document epoch/form epoch, unknown option, data ref outside the grant or unsaved, `select_multi`, button, link, submit-like, disabled, read-only, hidden, value longer than `maxLength`, wrong entry variant, a smuggled value/origin/selector/provider/URL. 18 parametrised runtime cases plus 40+ domain cases.

## 19. Form/element epoch binding

An observation is current only if no later page observation of the tab has a different worker, a higher document epoch or a higher form epoch; checked at planning, at proposal and again at approval.

## 20. Element identity hash

SHA-256 over the projected semantic identity (observation, tab, both epochs, form, frame, role, control type, accessible name, the four state flags, submit-likeness, `maxLength`) plus, for choices, the option ref and label. No selector, id, value or coordinate exists in the projection. It uses the projected identity, not the worker-internal ordinal.

## 21. Recipient-origin derivation

From the profile's own `allowed_origins` matched against the newest observed page's host; stored in the scope; a page on another host gives `origin_changed`. Never from a model, page or renderer.

## 22. Manifest schema

Frozen `DisclosureManifest`: task, profile, grant, provider, site display, recipient origin, account binding (a one-way hash, not the fingerprint), revoke epoch, observation, tab, document/form epoch, form, and per field the element ref, identity hash, label, control type and exactly one of {dataRef + valueDigest + preview}, {option ref/label/identity hash}, {checked}.

## 23. Canonical digest

Fields ordered by element number; `manifest_digest` is SHA-256 over canonical JSON of everything else and re-verified on parse. Tests mutate 15 manifest-level and 10 field-level facts independently, and the identity hash's own inputs.

## 24. Protected-value freshness

At approval the values are re-read with a share lock and compared to the bound digests; `protected_value_changed` refuses the approval and leaves it pending. `verify_protected_values_current` is the reusable helper for S6's later re-check.

## 25. Profile/account freshness

Grant usable (ACTIVE, unexpired, same account and epoch, source alive) is decided by the database; precise reasons are `account_changed`, `login_required`, expiry. Login expiry also yields no planning context.

## 26. Exact action approval binding

The existing action + approval rows are reused (no new table). Tool `prepare_form`, risk R2, proposal = the manifest. Approval is granted, claimed (revision, digest, expiry re-checked in SQL) and terminalised by `ActionService.settle_exact_approval` in one transaction, with a guard that re-checks everything in section 24-25 under the task lock. The generic action routes refuse this tool, so they cannot bypass the guard.

## 27. Trusted UI / IPC

Six channels: `prepareFormPlanning(refs)`, `grantFormPlanning(id, rev)`, `declineFormPlanning(id, rev)`, `runFormPlanning()`, `approveFieldDisclosure(id, rev)`, `rejectFieldDisclosure(id, rev)`. Sender-checked; no manifest, value, origin, field or provider crosses. Main's runtime route allowlist gained exactly eight routes, none that saves or reads a value. The renderer never masks and holds no raw value; buttons say "Allow planning" and "Approve this plan", never "Fill".

## 28. Voice/text structural exclusion

None of the six names appear in the voice controller, its backend type, the tool vocabulary or the voice IPC handler (tests).

## 29. `prepared_nothing`

No new action status. The approval is spent through `WAITING_APPROVAL -> APPROVED -> EXECUTING -> SUCCEEDED` in one transaction; the finished attempt's result is `{"code":"prepared_nothing","browser_dispatches":0}`. (S5 only. Since S6 a new `form-prepare-v2` approval funds a local draft instead; a `prepared_nothing` result is historical and its approval is terminal.) It creates no dispatch and is never given to a worker. `SUCCEEDED` means the approval was recorded and used, not that anything was done; the timeline and card say so.

## 30. Proof of zero dispatch/write

Service tests: dispatch count and worker-call list unchanged across preparation and approval. Real-browser acceptance (`test_form_prepare_browser.py`, real worker, real Chromium, fixture `/app/apply`): dispatches unchanged, and the page counters for `input`, `change`, `focus`, `click`, `keydown`, `submit` and `autosave` and the server's `submissions`/`mutations` all unchanged.

## 31. Single-use approval proof

Second approve refused; the spent approval id cannot be attached to another attempt (unique constraint); stale revision, changed manifest/saved value/profile/revoke epoch all refused before consumption; a new proposal supersedes the old.

## 32. Provider raw-secret scan

Capture tests on the named provider assert zero raw markers and no `valueState`/selector/digest/URL in the input.

## 33. Diagnostics/memory firewall

Events carry ids, digests and counts only (`protected_value_count`, `allowed_data_ref_count`, `form_count`, `candidate_element_count`). Authenticated tasks are excluded from other requests' contexts and episodic memory (test with a snapshot carrying a form plan).

## 34. Targeted tests

`test_form_prepare_domain.py` **159**, `test_form_prepare.py` **63**, `test_form_prepare_api.py` **8**, `test_form_prepare_browser.py` **1**, schema **10**, source-scan additions; TypeScript `agent-form-planning.test.ts` **36**, `agent-form-boundary.test.ts` **27**, route allowlist test.

## 35. `uv run mypy`

Clean (189 source files).

## 36. npm tests

`npx vitest run`: **2174 passed, 22 skipped, 1 failed + 2 files that could not load**: the known `accessibility.test.tsx` stale-CSS baseline and the `real-inference` / `tokenizer-pack` missing-model-pack baseline. Not S5 regressions.

## 37. Typecheck / build

`npm.cmd run typecheck` clean; `npm.cmd run build` clean.

## 38. Eval

`npm.cmd run eval`: **118/118**.

## 39. Non-browser suite

`uv run pytest -m "not browser"`: **1319 passed, 1 failed, 298 deselected.** The failure is the known `test_booking_routes_without_a_worker_answer_503` baseline (`browser_worker_unavailable` vs `browser_worker_not_configured`), unrelated to S5; nothing was changed to hide it. One S4 test that asserted `protected_values` was absent was updated to the S4 invariant that still holds.

## 40. Browser suite

One copy, nothing else running: `uv run pytest -m browser` - **280 passed, 18 skipped, 0 failed**, 1321 deselected, 4 warnings, 33m46s.

## 41. Electron acceptance

Run sequentially. It **found a real defect**: main's runtime supervisor refuses unlisted routes, and the new form routes were not on the allowlist, so every authenticated task's snapshot failed (`request_failed`). The unit tests use a fake runtime and could not see it. Fixed with an exact eight-route allowlist and a test. After the fix: authenticated acceptance, base desktop acceptance (booking, lost-response restart) passed; the login-takeover acceptance passed alone and once failed with "renderer never loaded" in a multi-file run, then passed when re-run (a startup flake, not reproduced). **Limit:** the desktop app cannot bind a profile to the loopback fixture (public-suffix rule, as S3 recorded), so the S5 cards themselves are exercised by the pure view-model tests, the fake-runtime controller tests and the real-browser Python acceptance, not by a driven Electron session.

## 42. Package

`npm.cmd run package:dir` passed: Chromium 153.0.8010.12 headless and headed verified, no browser-profile directory, bundle imports and secret scan passed, migration `0010` present, planted markers absent from the artifact.

## 43. Packaged acceptance

`LUMI_PACKAGED_E2E=1 uv run pytest tests/test_packaged_app.py`: **1 passed in 112s** (fresh database migrates through 0010).

## 44. Authenticode: UNRESOLVED

`Get-AuthenticodeSignature release\0.1.0\win-unpacked\Lumi.exe` -> `NotSigned`, no signer certificate. electron-builder's `signing with signtool.exe` lines are not a signature. The release gate and the **no-real-account / no-real-saved-detail / no-real-form gate remain open**; everything used synthetic fixtures.

## 45. Known gaps

- No product UI exists for entering saved details; the runtime route exists for the trusted desktop layer and tests seed it.
- Form planning needs an open task (before an answer closes it); joining reading and planning into one flow is S6's concern.
- Approval cannot see an unobserved DOM change; S6's worker-live check covers that.

## 46. Scope confirmation

S6, M9 and M10 were **not** started. No worker operation, `LOCAL_DRAFT` effect, freeze, `form_drafts`, `frozen_at`, dirty state, handover or write exists; the source scanner and a schema/route/protocol scan assert it.
