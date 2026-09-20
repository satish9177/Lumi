# Milestone 8b, S4: authenticated element and form observation

> **S4 observes authenticated form structure only. Lumi cannot type, choose, check, click, upload or submit anything.**

> **Element inventory remains local in S4 and is not added to the authenticated provider projection. S5 will deliberately decide what structural metadata a planner may receive.**

Status: **S4 closed.** S5, S6, M9 and M10 have **not** been started. M8b itself is not complete.

Roadmap: M8b - S4 ✅ authenticated element/form observation; S5 next; S6 later.

## 1. Starting point

Closed S3 head: `9b4a3d6bc72037b0e7cd034c77694f0f4d2d6fe9` (`docs: finalize milestone 8a S3 validation`).

## 2. Implementation SHAs

| Commit | SHA |
| --- | --- |
| Implementation: `feat(agent): add authenticated form element observation (M8b S4)` | `e46249e6f3983b85eadf45a3eb35ea5f9782a5ff` |
| Docs closure: `docs: close milestone 8b S4 element observation` | the commit that adds this file (see the final report for its SHA; a document cannot contain its own hash) |

## 3. Migration `0009`

`services/agent/alembic/versions/0009_authenticated_form_inventory.py` (revises `0008`) adds exactly two columns to `authenticated_observations`: `form_epoch` (integer, default 0) and `element_inventory` (JSONB, default `'{}'`). It adds nothing that belongs to S5/S6: no protected values, drafts, disclosure manifest, approval bindings, `frozen_at` or written-value hash. Downgrade removes the S4 shape and deletes version-2 rows (local account-private evidence; evidence rows cannot be rewritten). The migration is present in the packaged bundle at `agent-runtime/agent/alembic/versions/`.

## 4. Observation and schema-version strategy

- `schema_version = 1`: an S3 text/link observation with no inventory. Existing rows keep it (`form_epoch = 0`, `element_inventory = '{}'`). `ADD COLUMN` with a constant default rewrites no row and never touches the immutability trigger. A CHECK makes "version 1 with an inventory" unrepresentable.
- `schema_version = 2`: every new observation.
- The runtime API response keeps the S3 text/link shape; the inventory is not part of it.

## 5. Bounded form grouping

Forms `f1`-`f5`. Grouping is by owning `<form>`, else the nearest `[role=form]`, else a single "unowned" group. A form label is present only if the page supplies `aria-labelledby` or `aria-label`; it is redacted, bounded to 120 characters and never fabricated. A bound that cuts anything off sets `truncated`.

## 6. Same-origin frame model

Frames `fr0`-`fr4`; `fr0` is always the main frame. A frame is inventoried only if it and every ancestor are http(s) frames of the top-level document's origin. A cross-origin frame is never evaluated, so not even its labels are read. A frame carries a slot number, never a URL. A frame that contains any credential-shaped control contributes nothing.

## 7. Exact element inventory

Elements `e1`-`e40` (at most 100 records scanned per frame), options `op1`-`op25` numbered per control. Each element carries: `elementRef`, `formRef`, `frameRef`, `role`, `controlType`, `accessibleName`, `valueState`, `required`, `enabled`, `visible`, `readOnly`, `maxLength`, `optionRefs`, `submitLike`. `labelRef` is reserved and never set. The models are `extra="forbid"` and frozen.

## 8. Deliberately absent fields

Current or default value, value preview, option value, id, name, class, tag name, selector, XPath, DOM path, HTML, event handler, script, coordinates, bounding box, dataset, form action, form method, raw `autocomplete`, frame URL, and any locator description.

## 9. `valueState`

`empty | filled | unknown`, reported only for `text`, `email`, `tel`, `number` and `textarea`; everything else is `unknown` because the meaning of a checkbox, select or widget "value" is ambiguous and S4 does not guess. The page-side helper reads a field's value in exactly one place (`hasValue`), returns a boolean, and discards the string. The value therefore never reaches Python, the runtime, the database, a log or a provider. The model validator rejects a non-`unknown` state on any other control type.

## 10. Credential, file and OTP exclusions

Excluded while listing, not filtered afterwards: `password`, `file`, `hidden`, `autocomplete` tokens `current-password`, `new-password`, `one-time-code` and `webauthn`, and inputs whose name matches `otp`. A frame containing any credential-shaped control yields nothing at all, in list mode, and cannot be resolved in resolve mode.

## 11. Accessible-name derivation and redaction

A worker-authored, simplified subset of ARIA, in order: `aria-labelledby`, `aria-label`, associated `<label>`, button `value` or image `alt`, button text, `title`, `placeholder`. Text of a subtree never descends into a form control, so a label wrapping an input cannot expose what was typed. Names are bounded, redacted with the S3 `Redactor` inside the worker, and re-checked by the model (`is_redacted`); the model refuses a string that a fresh redactor would still change. It is not a browser accessibility tree.

## 12. Option refs

`op1`..`op25` are sequential per control and carry a redacted label only. The `value` attribute of an `<option>` is never read. Only selects and radio groups carry options.

## 13. Submit-like heuristic

`<input type=submit|image>` and `<button type=submit>` are `submitLike`; only a `button` role can be. The flag can only *remove* a future capability; `false` means "not recognised as a submit control", never "safe".

## 14. `document_epoch`

Unchanged from S3: one per tab, rising on navigation; an element ref belongs to exactly one document epoch.

## 15. `form_epoch`

New, monotonic per tab. It rises whenever the inventory fingerprint changes (or a renewal is forced), so a page that re-renders its form without navigating still invalidates every element ref.

## 16. Inventory fingerprint

SHA-256 over structure only: frame slot, form key, semantic identity (role, control type, name, required, read-only, enabled, visible), option labels and `maxLength`. `valueState` is deliberately excluded so typing never bumps the epoch. The fingerprint is worker-internal and is never persisted or sent.

## 17. Worker-only locator descriptions

`ElementLocator` holds a frame slot, form key, ordinal, frame record count, semantic identity and the two epochs. It lives in `AuthenticatedTab` memory, holds no value and no DOM handle, and is never persisted, returned or logged.

## 18. Element revalidation

`resolve_element` checks the document epoch (`stale_document_epoch`), then the form epoch, then that the ref belongs to the tab. It then re-lists the frame in the live DOM and requires the same control count, a control at the same ordinal, and the same semantic identity and form. No nearest match, no fuzzy fallback, no forcing. The `ElementHandle` is used once and disposed; none survives a step. `reveal(elementRef)` exists only as this worker-level primitive (it scrolls the control into view); the planner vocabulary is unchanged.

## 19. Stale-ref behaviour

Fails closed with a session error (`stale_document_epoch`, or the form-epoch equivalent) and kills the tab's element refs when a gate fires or a ref goes stale. A fresh `observe` is the only way forward.

## 20. Residual: identical-fingerprint replacement

A DOM replacement whose observed semantic fingerprint is identical is indistinguishable by construction. S4 detects structural changes visible to the reviewed fingerprint, not every re-render. Shadow DOM is not inventoried. This residual is documented in `docs/SECURITY.md`; S6, which writes, must weigh it before any write is permitted.

## 21. No-write structural and source proof

`tests/test_form_observation_source.py` (via `tests/form_source_scan.py`) scans the module with the AST for Python and with a reviewed allow-list for the one static DOM helper. It fails on any call-shaped mutation (`fill`, `type`, `press`, `click`, `check`, `select_option`, `set_input_files`, `dispatch_event`, `request_submit` and similar), on assignment to anything other than two local records, and on a second value read. The helper source is static: never built from page text, model text or an argument.

## 22. Fixture proof of zero interaction

The account fixture's form pages count `input`, `change`, `focus`, `click`, `keydown`, `submit` and autosave events. Observing a form, and re-observing and revalidating, leaves every counter at zero.

## 23. Provider prompts

The inventory is **not** added to the planner prompt, the answer prompt or `authenticatedObservationLines()`. A planted marker is asserted present in the local row and absent from every provider payload, public research table, memory path and diagnostic. Diagnostics carry only `form_count`, `element_count`, `option_count`, `form_epoch` and `inventory_truncated`.

## 24. Account-private classification firewall

The inventory is stored as `account_private` / `untrusted_environment` evidence and follows the S3 firewall: it does not enter public research, memory or any shared surface.

## 25. Checks before projection

The S3 gates run first: account identity, the S2 credential detector and the credential checks. A page the detector flags, or any identity failure, produces no inventory at all.

## 26. S3 preservation

One provider, no failover: unchanged. The network authority is unchanged (`ACCOUNT_READ`, GET/HEAD, S0 broker). The step vocabulary is unchanged (`navigate observe reveal tab history stop`).

## 27. Targeted validation

- S4 worker browser suite: **33 passed**
- Authenticated service-browser S4 coverage: passes
- TypeScript S4 tests: pass
- `uv run mypy`: clean

## 28. Non-browser Python suite

Authoritative post-scanner-fix run: **1082 passed, 1 failed, 297 deselected.**

Failure: `tests/test_booking_preparation.py::test_booking_routes_without_a_worker_answer_503` expected `browser_worker_not_configured` and received `browser_worker_unavailable`.

**This is not an S4 regression.** It reproduces against the closed S3 head `9b4a3d6bc72037b0e7cd034c77694f0f4d2d6fe9`, and the code involved is byte-identical between S3 and S4 (Git blob SHAs):

| File | Blob SHA (S3 and S4) |
| --- | --- |
| `tests/conftest.py` | `e45b9f1ae69f87d83081dd13f451053a2ed0d7e7` |
| `tests/test_booking_preparation.py` | `548bbd3f157e318683a372efdf02d519aeef064c` |
| `app/main.py` | `e18b1bf8994dbae2d251f01bb4dda9a2cee8d08d` |

It is recorded as the existing environment/config-sensitive booking-test baseline. No production code and no shared fixture was changed to make it pass.

## 29. Browser isolation investigation

The first monolithic browser run was **invalid**: the machine reached critical memory pressure, the monitor died and there was no final pytest result. It is not counted as a test failure. Browser files were then isolated one at a time and every ordinary browser-marked test passed.

## 30. Authoritative browser result

One complete run with memory restored: `uv run pytest -m browser` - **279 passed, 18 skipped, 0 failed, 4 warnings, 1083 deselected, 33m32s.** The warnings are the FastAPI/Starlette 422 deprecation, Windows asyncio/proactor closed-transport warnings, and the known `httpx.ReadError [WinError 10054]` in the deliberate runtime-crash test.

## 31. npm, typecheck, build, eval

- `npm.cmd run typecheck`: clean
- `npm.cmd run build`: clean
- `npm.cmd run eval`: **118/118**
- `npm test`: **2108 tests passed.** The known `accessibility.test.tsx` stale-CSS baseline and the `real-inference` / `tokenizer-pack` missing-model-pack baseline failures remain. They are not S4 regressions.

## 32. Packaging

`npm.cmd run package:dir`: **PASS.** Python 3.12.14, Playwright 1.63.0, Chromium 153.0.8010.12 (chromium-1243); headed and headless verified. Runtime total 654.6 MB (python 215.9, agent 3.2, browser 435.5). Bundle imports, no secrets, no browser-profile directory, Chromium launches from the bundle alone, and migration `0009` is present.

## 33. Packaged acceptance

`LUMI_PACKAGED_E2E=1 uv run pytest tests/test_packaged_app.py -vv`: **1 passed in 105.94s.** The packaged runtime starts and a fresh packaged database migrates through current head, including `0009`. No real account was used.

## 34. Authenticode status: UNRESOLVED

No result of `Get-AuthenticodeSignature` was supplied with the closure request, and the electron-builder line `signing with signtool.exe` is not evidence of a valid signature. The check was therefore run directly on this machine:

```text
Get-AuthenticodeSignature release\0.1.0\win-unpacked\Lumi.exe
Status            : NotSigned
SignerCertificate : (none)
```

Windows reports the packaged `Lumi.exe` as **not digitally signed**. The Authenticode release gate is **not satisfied** and remains explicitly open, and the **no-real-account gate is retained**: no real personal account may be used with this build.

## 35. No real-account testing

No real account, real session, real form or real credential was used. All acceptance used the synthetic fixture.

## 36. Scope confirmation

S5, S6, M9 and M10 were **not** started. There is no protected-value store, disclosure manifest, approval binding, network freeze, write primitive or handover in this change.
