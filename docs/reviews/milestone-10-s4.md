# Milestone 10, S4: cross-app preparation workflow

> **Lumi can now carry one preparation through several apps: download a document into an approved folder, read it, optionally compare it with ONE approved AI provider, let you adopt individual details from it, and prepare an account form with exactly those details — then stop before submitting.**

> **It still cannot submit, press Enter, click, upload a file or navigate on your behalf, and no step's approval implies another's.** A workflow is a deterministic controller that owns lineage only; it holds no tool of its own and asks no model what to do next. Cross-executor recovery and the consequential test action are S5.

Status: **S4 COMPLETE (engineering).** S5 is not started.

```text
M9                                 COMPLETE (merged to main)
Windows signing infrastructure     READY
Production certificate             NOT CONFIGURED
Real-account release               BLOCKED

M10 S1 approved documents + broker COMPLETE
    S2 downloads + placement       COMPLETE (engineering)
    S3 project recipes             COMPLETE (engineering)
    S4 cross-app preparation       COMPLETE (engineering)
    S5 cross-executor recovery     NOT STARTED
```

Only synthetic data was used: a generated PDF of "application details", S2's protocol-exact fake download worker, a scripted, grounded S1 provider result, and the account fixture in a real headed Chromium.

## 1. The flow and who approves what

```text
role download   S2 card + main's native dialog -> download -> placement            (unchanged)
role documents  exactly the placed file (same file identity, same SHA-256) -> extract (S1)
                optional ONE provider disclosure: its own S1 card (provider, model, excerpt)
                candidates: document_extracted | provider_derived
                each adoption: its own exact approval + main's native dialog (value + source)
role form       account-reading card -> preparation mode -> planning grant -> exact manifest
                -> frozen local fill with ONLY this workflow's values                (M8, unchanged)
                STOP BEFORE SUBMIT
```

| Authority | Held by | Never implies |
| --- | --- | --- |
| Download + placement | the `download` step task (S2 grant) | reading the file, any provider |
| Provider disclosure | the `documents` step task (S1 `document_disclose`, one provider and model) | any form, any origin, any adoption |
| Adoption of one detail | an exact R2 approval (`adopt_workflow_value`) on the documents task | any form or origin |
| Account reading, planning, manifest | the `form` step task (M8 grants and exact manifest) | the disclosure, another workflow's values, the global saved details |

## 2. Lineage (decided by the database)

Migration `0020` adds `workflows`, `workflow_steps`, `workflow_candidates` and `workflow_values`:

* `workflow_steps`: `UNIQUE(task_id)` (a task is in at most one workflow) and `UNIQUE(workflow_id, role)`. A step is inserted **in the same transaction that creates its child task**, under the workflow row lock (a `link` hook on `TransferService.create` and `DocumentService.create_task`).
* A candidate's `(workflow, source task, 'documents')` is a composite foreign key onto `workflow_steps`; its `(document, source task)` and `(disclosure, source task)` are composite foreign keys onto `documents` / `document_disclosures` (which gain `UNIQUE(id, task_id)`). A document or disclosure from any other task or workflow cannot become a candidate, whatever code tries.
* A value's `(candidate, workflow, provenance, kind, digest)` is a composite foreign key onto its candidate, and the digest is a CHECK against `sha256(value)`. Provenance is `document_extracted` or `provider_derived` — there is no user-typed member. `UNIQUE(workflow_id, kind)`.
* The documents step holds exactly the file its own transfer placed: `add_placed_file` re-reads it from its READ root and requires the placement's (volume, file index) **and** the download manifest's SHA-256. A file replaced after placement is `placed_file_changed`. The generic document routes refuse to add any other file to a workflow step (`workflow_step_files_fixed`).
* The form step's value source is decided by `workflow_steps` alone: a `form` step reads only its own **live** workflow's adopted values; any other task reads only M8's global saved details; a `download`/`documents` step cannot plan a form (`not_a_form_step`). The grant scope and the manifest carry the `workflow_id`, and every use re-checks that it is still the task's workflow and still live.

## 3. Provenance

* **`document_extracted`:** a deterministic, closed-vocabulary extractor (`Full name:`, `Email:`, `Phone:`, `City:`, `Country:`, `LinkedIn:`, `Portfolio:` …) over the document's own text. Every value must canonicalise as that M8 protected kind; anything else, including injected instructions, is ignored. The candidate records the document's text digest and the span.
* **`provider_derived`:** the S1 provider's closed comparison schema is **unchanged** — it can point at text but can never name a field kind or a value. A labelled line of the *document* becomes a provider-derived candidate only when a quote the runtime grounded in the disclosed projection covers that **whole line**; the value is read from the document span, never from the quote (review finding 1). Identifiers were redacted before the provider saw them, so an email or phone can never be provider-derived.
* **Adoption** is an exact, single-use R2 approval whose proposal is digest-only (no raw value in the ledger). The guard, inside the approving transaction, re-derives the value from the document (and, for a provider-derived value, re-proves the same SUCCEEDED disclosure, the same projection digest and a still-grounded quote), then inserts the value. Main shows a native dialog built from the runtime's card: the kind, the exact value and its source.
* The M8 manifest gains `workflow_id` and a per-field `provenance`. Both are omitted from the dump when unset, so every pre-S4 scope and manifest keeps its exact digest (pinned by digests computed with the pre-S4 module). The card labels a workflow value "(from your document)" or "(AI suggestion from your document)", never "Saved …".

## 4. Stop and retention

A workflow expires 24 h after creation. **Stop** marks it STOPPED, purges every candidate's value and quote and every adopted value (digests, previews and provenance stay as the audit record), rejects pending adoptions, revokes the transfer and disclosure grants still open, and revokes the form step's account-reading grant. The form step's planning grant and any manifest refuse a stopped workflow. Stop never undoes a download, a placement, a disclosure already sent or a frozen local draft, and never marks anything in flight failed; a frozen draft stays for the person to check or discard.

## 5. Electron

* 12 fixed IPC channels; none takes a value, a provenance, an origin or a provider, and none submits. The runtime allowlist pins exactly the workflow routes.
* `workflow-wire.ts` refuses an adopted raw value or an unknown provenance anywhere in a reply.
* The form step becomes the active task through `AgentTaskController.activateWorkflowFormTask` (the runtime-issued task id, the same offered recipients, the account card opened PENDING), so the existing account and form cards drive it unchanged.
* `WorkflowPanel` (inside Documents) renders every candidate, quote and card as inert text.

## 6. Tests

* **Python:** `test_workflows_service` (19, real PostgreSQL, real NTFS folder, real extraction helper): lineage (placed file only, file replaced after placement, one role per task, cross-workflow and loose documents refused, the composite foreign keys), adoption (exact, single use, provenance kept, no raw value in the ledger or events, no global saved detail written, source moved → refused), generic routes refusing to mint or approve an adoption, provider-derived candidates only from grounded quotes, disclosure and form origin as separate approvals, the form step placing only workflow values with provenance on the manifest, a non-workflow task never seeing them, Stop, and the six review regressions. `test_workflows_domain` (7, including the pinned pre-S4 digests), `test_workflows_source` (5: no executor import, no submit/click/key/upload/navigate identifier, no write to the global saved details, one caller of `values_for_execution`, no document text read outside `DocumentService`), `test_migration_0020`.
* **Combined acceptance (browser, real headed Chromium):** `test_workflow_acceptance_browser` — download → place → extract → one provider disclosure → three adoptions (two document-extracted, one provider-derived) → preparation mode → exact manifest → frozen local fill. The page holds the workflow values (not the global saved email), the fixture's **submission counter is 0** along with every other adversarial counter, no raw value is in any ledger row, and after Stop the values are purged and the draft is discarded with still zero submissions.
* **TypeScript:** `workflow-controller.test` (8): native adoption confirmation from runtime state, cancel and stale revision, id validation before any request, form-step activation, wire refusals, inert cards, the provenance label on the manifest card, and the allowlist.

## 7. Independent adversarial review (fresh Claude reviewer; no external model)

The reviewer was read-only and ran nothing against the database. Every finding was verified by the lead against the code.

| # | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| 1 | Medium | A provider-derived value was extracted from the provider's own quote, which only had to be a *normalised* substring of the excerpt: the provider could start mid-line (`…contact name: Bob` → a legal name), truncate a value, or change its case. | The value now comes from the **document**: a labelled document line becomes a candidate only if a grounded quote covers the whole line; adoption re-derives from the document span and re-checks the line is inside the quote. The misleading S1 docstring ("the projection's own text") was corrected. Regression test. |
| 2 | Medium | The provider quote (document text) survived Stop and expiry and was still shown. | Purge clears the quote with the value (the CHECK allows a null quote once purged). Regression test. |
| 3 | Low | Lock-order inversion: adoption locked task→workflow, Stop/extract/derive workflow→task (a deadlock, no unsafe state). | One order everywhere: child task rows (sorted), then the workflow row. |
| 4 | Low | The generic document routes could add other files to the workflow's documents task. | Refused on workflow steps (`workflow_step_files_fixed`); the placed-file import is idempotent and re-checks liveness under the task lock. Regression test. |
| 5 | Low | The view listed every adoption action; after 64 the wire's list cap made the workflow unviewable. | One card per candidate (its latest). Regression test. |
| 6 | Low | Stop left the form step's account-reading grant usable. | Stop revokes it (the browser and any frozen draft are left alone). Regression test. |
| — | Note | The M8 planning prompt still calls the offered values "saved details". | Documented (§8). The planner sees only masked previews and never the provenance-bearing card. |

## 8. Validation

* `uv run mypy`: clean (335 files).
* `uv run pytest -m "not browser and not desktop_uia"` (known `test_booking_routes_without_a_worker_answer_503` baseline deselected): **2436 passed**.
* Browser: `test_workflow_acceptance_browser`, `test_form_draft_browser`, `test_form_prepare_browser`, `test_local_form_draft_browser`, `test_download_worker_browser`: 38 passed.
* `npm.cmd run typecheck` and `npm.cmd run build`: clean. `npx vitest run`: all pass except the three known machine baselines (`real-inference`, `tokenizer-pack`, `accessibility`).

## 9. Residual risks

* The M8 form planner's prompt describes workflow values as "saved details" (masked previews only; cosmetic).
* A same-user process can change the downloaded file after the import; the import is identity- and hash-bound, and later extraction re-verifies, but nothing locks the file.
* The candidate extractor is deliberately narrow (labelled lines only); an unlabelled value is never a candidate.
* Two simultaneous `start_documents` calls are serialised by the task lock and the idempotent import; the UI also allows one request per workflow at a time.
* Carried forward unchanged: S1–S3 residuals, production certificate NOT CONFIGURED, real-account release BLOCKED.
