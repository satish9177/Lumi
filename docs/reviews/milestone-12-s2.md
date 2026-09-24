# Milestone 12 S2: documents + controlled download composition (partial: documents)

> **A trusted action supplies the input; the planner only cites it.** `document_read` and `document_compare`
> are the first two capabilities to actually consume a Milestone 12 S1 resource. Neither the model nor the
> renderer can mint a `document_ref` -- it exists only once a trusted action (main, on the user's behalf)
> adds an already-approved file to this orchestration's own document task. The extracted text and every
> comparison detail (shared terms, headings, quotes) stay local; only a character count or an overlap
> percentage ever reaches the orchestration's own result summary.

```text
Migration head          0024
```

## Scope: documents composed; download/inspection deliberately deferred

S2's full plan named five capabilities: `inspect_public_page`, `document_read`, `document_compare`,
`download_document`, `place_downloaded_file`. This pass composes **`document_read` and `document_compare`
only**. The other three all need the same missing primitive: a trusted way for a `public_url_ref` to enter an
orchestration as a *planner-selectable* input, plus (for `download_document`) a destination-selection
convention Lumi has never needed before (today's direct download flow always has the user pick a destination
root and file name explicitly in that flow's own UI; composing it through the general orchestrator would mean
inventing a new "default destination" heuristic with its own security review). Building and reviewing that
mechanism to the same bar as this slice is real, separate work. Rather than build it hastily alongside
documents, it is deliberately left for a follow-up pass, and `inspect_public_page`/`download_document`/
`place_downloaded_file` remain `capability_unavailable` through the orchestrator for now -- composing them
does not weaken any M1-M10 boundary either way, it simply has not happened yet. This is a scope decision, not
a security finding: nothing about the resource-ref mechanism blocks it; the missing piece is UX/policy for
"which URL, which destination," not authority.

**Renderer:** none. `attachApprovedDocument` is fully implemented and tested down to the IPC/preload layer,
but no renderer control calls it yet -- matching S1's own no-renderer-changes precedent, and because a real
file-root/file picker control is itself a small UI design decision better made together with S4 (the
milestone's own designated cockpit slice) than added in isolation here without the ability to verify it
interactively in this session. Until a UI exists, `document_read`/`document_compare` are reachable through
the orchestrator only via direct IPC/API calls (already exercised end to end by this slice's own tests), not
yet from the running app's own screen.

## What this slice adds

* **Migration `0024`**: `orchestrations.document_task_id` (nullable, set once) -- the one document task this
  orchestration's document resources refer into, because `DocumentService.compare_local()` (Milestone 10 S1)
  requires both documents to live in the same task. `orchestration_resources.backing_id` (a file or document
  id inside that task) and `backing_text` (reserved for a future `public_url_ref`), mutually exclusive by a
  CHECK constraint. Both are model-invisible: they are projected only into the trusted, main-process-only
  wire response, never into what the planner's own context is built from.
* **`OrchestrationService.register_resource()`**: mints a `document_ref` (or, mechanism built but unused this
  slice, a `public_url_ref`) from a trusted action outside the planner loop -- never a capability's own
  result. Sets `document_task_id` once; a second file from a different task is refused
  (`document_task_mismatch`). `trusted_input_label()` is the one place this milestone deliberately allows
  echoing a user's own already-seen file name back to the planner -- categorically different from
  `safe_label()`'s template-only rule for a capability's *output*, and the two are never used for each
  other's purpose (verified in review).
* **`document_read`/`document_compare` join `SYNCHRONOUS_CAPABILITY_IDS`** (main already performed the real
  extraction/comparison through `DocumentService`'s own existing, no-new-approval methods before calling
  `advance()`) and **`REPEATABLE_CAPABILITY_IDS`** (reading multiple files, or comparing more than one pair,
  is legitimate -- unlike `public_research`/`project_status`/`project_start`, which still answer at most
  once per orchestration, unchanged).
* **`advance()` gains `result_backing_id`**: required exactly when a capability's own output spec says so
  (`document_read`'s new `document_id`), forbidden otherwise -- a single guard applied uniformly before any
  capability-specific branching, so it cannot be forgotten per capability.
* **`OrchestrationCoordinator.attachApprovedDocument()`** (main): lazily creates the shared document task on
  first use, adds the file through `DocumentController`'s existing entry point, registers the resource.
  **`dispatchDocumentRead`/`dispatchDocumentCompare`**: resolve a cited ref's backing id and *kind* from the
  same fresh `view.resources` the planner was just shown (never stale, never a separate fetch), call
  `DocumentService`'s existing methods, and build a summary from numeric facts only (`textChars`,
  `Math.round(overlap * 100)`) -- never any extracted text, shared term, heading or quote.

## Adversarial review

One fresh, independent Claude review (general-purpose agent, no prior session context) checked nine
invariants against the actual code. **No High or Medium findings.** Three Informational notes, all addressed
in this same pass rather than carried forward:

1. `resolveDocumentResource` matched a cited ref by `ref` alone, not `kind` -- not exploitable (both ids are
   always scoped to the one `document_task_id` this orchestration owns, and `advance()`'s own
   `resource_kind_mismatch` check would catch it before any summary reached the planner regardless), but
   fixed anyway: it now takes an `expectedKind` and refuses before calling `DocumentService` at all, with a
   new regression test.
2. `dispatchDocumentCompare`'s `first.taskId !== second.taskId` check is currently unreachable (both refs
   always resolve through the same `documentTaskId` parameter today) -- kept as documented defense in depth
   for a future change, not the primary guarantee (which is the set-once `document_task_id` column itself).
3. `result_backing_id_not_allowed` was tested for only one of the three M11 capabilities that must refuse
   it -- extended to a parametrized test covering `project_status`, `public_research` and `project_start`.

Confirmed sound: cross-orchestration document-task isolation (the same `SELECT ... FOR UPDATE` row lock
`_live_running()` already uses, plus a set-once compare-and-swap on `document_task_id`, plus every resource
lookup being scoped to `(orchestration_id, ref)` with no unscoped variant); `document_compare` cannot smuggle
documents across tasks; `needs_backing_id` is enforced by one guard before any capability-specific branch;
no extracted text, shared term, heading or quote reaches the planner in either capability's summary or its
resource's `safe_label`; the loop-guard exemption is scoped to exactly `document_read`/`document_compare`;
`register_resource`'s own kind/backing-shape validation and terminal/expired-orchestration refusal all hold;
the migration's FK/CHECK/downgrade shape is sound and consistent with the rest of the schema.

## Validation

* `uv run mypy app tests`: clean (311 source files).
* `uv run pytest tests/test_orchestration_service.py`: 57 passed (documents composition + the full S1 suite,
  unchanged).
* `uv run pytest -m "not browser and not desktop_uia"`: 2,570 passed, 386 deselected; the only failure is the
  pre-existing, documented environment baseline (`test_booking_routes_without_a_worker_answer_503`),
  unrelated to this diff.
* `npm.cmd run typecheck`: clean.
* `npx vitest run`: passing at the same baseline as S1 (the only failing files are the three pre-existing,
  documented, machine-specific baselines -- `real-inference`, `tokenizer-pack`, `accessibility` scam CSS --
  unrelated to this diff); one genuine regression this slice caused
  (`document-firewall.test.ts`'s closed-channel-list assertion, which correctly needed `attachApprovedDocument`
  added to it) was found and fixed before this validation run.
* `npm.cmd run build`: main/preload/renderer all build clean.
* `pytest -m desktop_uia` and `npm.cmd run package:dir` remain deferred to the M12 final validation pass, per
  S1's own established practice (this slice touches neither surface).

## Documented residuals, carried forward

* `inspect_public_page`, `download_document`, `place_downloaded_file` remain `capability_unavailable` through
  the orchestrator (see "Scope" above) -- composing them is a follow-up, not blocked by anything in this
  slice's own design.
* No renderer UI yet calls `attachApprovedDocument`; a file/root picker control is deferred to be designed
  together with the milestone's own cockpit slice.
* `document_compare`'s catalog `inputClasses` (`['document_ref','document_ref']`, from Milestone 11 S1) is
  coarser than what this slice actually requires (`document_result_ref` x2, i.e. two *already-read*
  documents) -- consistent with the capability's own description ("two already-read documents") and with
  `inputClasses` being documented as "coarse, descriptive only," not a deviation from authority.
