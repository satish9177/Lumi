# Milestone 12 S5: form and workflow composition boundary

Starting commit: `4d19fd9f9d77a7a354e89a63d9af5ea10b94a636` (`lumi-m12`). Migration head remains `0025`.

## Status

```text
S5 security/composition evaluation: COMPLETE
form_prepare orchestration:          NOT COMPOSED
workflow_prepare orchestration:      NOT COMPOSED
```

S5 evaluated composition and deliberately refused it. Both capabilities remain `capability_unavailable`.

## Decision

`form_prepare` and `workflow_prepare` remain **catalogued but unavailable to the general orchestration planner**. This is the explicit S5 fallback in the handoff: leave a capability unavailable when composition would require granting the planner new URL, destination, or value authority. No form, workflow, browser, desktop, IPC, or database authority was added in this slice. S6 has not started.

## Code trace and blocker

* M12 `document_read` produces a `document_result_ref` backed by an extracted document ID (`orchestration-coordinator.ts`, `orchestration_resources.py`). M8 `FormPrepareService` obtains values through `value_snapshots`: global saved details for a standalone form task, or `WorkflowRepository.snapshots` for that form task's linked M10 workflow. It does not accept an M12 document result as a value source. Adding that result directly to a form grant would bypass M10's candidate, exact adoption approval, value digest, workflow scope, and expiry checks. A safe bridge must first create a candidate from the *same extracted document* and preserve two distinct human decisions: M10 adoption binds candidate source, value and workflow; the later M8 form manifest and approval bind the observed form and target fields. No current bridge carries the M12 document through both stages without exposing raw content to orchestration planning.
* M8 form observation and planning are attached to an authenticated-read task and its approved profile. `prepareFormPlanning` can re-observe that task's page and open its own scope card. There is no controller-issued M12 `form_target_ref` binding one fresh observed form, frame, navigation revision, and account identity to an orchestration. Minting a ref from a planner-supplied selector or a stale UI snapshot would break the stated freshness rule.
* M10 `WorkflowController.startWorkflowDownload` requires a URL, approved root ID, filename, and intent from its trusted workflow UI. `WorkflowService.start_documents` only accepts that workflow's placed download, and `start_form` creates a workflow-linked authenticated task. The existing M10 workflow is safe on its own terms, but M12 has no trusted, per-orchestration URL/destination input bundle to select. A planner-provided URL or path would reopen S2's deliberately deferred policy. Registering a bare `workflow_ref` or reporting a workflow as complete when merely opened would misrepresent progress and could bypass the inner approvals.
* The orchestration runtime's `COMPOSED_CAPABILITY_IDS` excludes both capabilities. Its resource compatibility matrix has no entries for them; `form_target_ref`, `form_result_ref`, and `workflow_ref` are closed vocabulary with no trusted registration path. The coordinator's closed dispatch returns `orchestration_refused` if an unavailable capability somehow reaches it. The runtime itself pauses `capability_unavailable` if it receives a catalogued but uncomposed step. The domain regression pins the current composed-capability/resource vocabulary. Separate coordinator regressions pin fail-closed dispatch for each S5 capability; planner-schema regressions reject raw value, URL, path, filename and overwrite fields even if an unavailable capability is incorrectly offered.

## Existing boundary evidence and limitations

M8/M10 browser suites cover protected form fields, exact disclosure, frozen local drafting, event suppression, zero submissions, and M10 adoption/Stop purge. Those prove the **direct** form/workflow paths; they do not prove the requested orchestrated document-to-form acceptance A or an orchestrated workflow G. **S5 added zero submission authority**, structurally: neither capability is composed or dispatched. No S5 orchestrated preparation was run, so an orchestrated submission count cannot honestly be reported as a live measurement. The general orchestrator still exposes only focus and scroll under `desktop_safe_action`; it has no generic browser write, keyboard, Invoke, or submit capability.

The following requested S5 acceptance remains **unmet**: document result to adopted value to form, a fresh form-target ref and stale-target rejection, cross-orchestration form/adoption replay checks, restart of an orchestrated form/workflow pause, and cockpit selection for these resources. Current resource ownership/expiry guards and M8/M10 direct tests are useful components, but are not substitutes for these end-to-end tests. A future implementation must supply the trusted lineage bridges, then run the full A-H matrix before marking either capability composed.

The M12 document picker remains IPC-only. S2 `inspect_public_page`, `download_document`, and `place_downloaded_file` remain deferred. No migration or packaging inclusion changed.

## Independent review and closure findings

Codex independently reviewed the actual M8, M10 and M12 paths and confirmed both blockers. No Claude review occurred. The review found one Medium privacy leak and one Low test-coverage gap; both were addressed in this closure.

* **Medium — planner-visible private labels.** `attachApprovedDocument` previously registered `file.displayName` as the `document_ref` safe label. That label flows through `orchestrationStateLines` into an independently routed planning provider. It now registers `approved document N` while retaining the file ID in the opaque ref's controller-owned backing. A narrow audit found the same metadata disclosure in `account_context_ref`, which embedded the signed-in site; it now registers `approved account N`. Tests attach a deliberately private filename and site, verify neither reaches any planner request line, and verify the same opaque refs still reach the document/account service. The existing S4 desktop application label and registered-app label remain as designed: they are software or registry display names, not document filenames or account sites; S4 already added a rule treating them as untrusted display text, never instructions. `project_ref` is already generic.
* **Low — coordinator dispatch coverage.** The domain test alone pinned vocabulary, not execution. Coordinator regressions now deliberately offer `form_prepare` and `workflow_prepare` from a faulty graph and require refusal before any child service, API or effect call. Planner-schema regressions reject attempted raw value, URL, path, filename and overwrite fields. The closed dispatch still contains no branch for either capability.

The later document-to-form, stale-target, cross-orchestration adoption, workflow restart and orchestrated submission acceptance cases remain future work because no S5 composition exists. The existing direct M10 browser acceptance's zero-submission assertion is separate evidence for the direct workflow.

## Validation

Validation on this closure checkout (using the repository's existing virtual environment for Python, equivalent to the requested `uv run` commands):

* `python -m mypy app tests`: passes, 313 source files.
* Targeted Python resource/orchestration/account tests: 83 passed, including the S5 domain regression.
* Targeted orchestration coordinator and planner Vitest: 88 passed, including neutral-label, opaque-ref, closed-dispatch, and raw-authority schema regressions.
* Direct M8 form/M10 workflow browser regressions: 6 passed. The M10 acceptance test asserts zero submissions for its direct workflow only.
* `npm.cmd run typecheck` and `npm.cmd run build`: pass.
* Broad non-browser/non-`desktop_uia` pytest: 309 passed, then stopped on the existing booking-worker error-code assertion (`browser_worker_unavailable` versus expected `browser_worker_not_configured`). This closure touches neither booking path.
* Full Vitest: 2,632 passed, 22 skipped; one unrelated Windows CRLF-sensitive accessibility assertion failed and two vision-model suites failed loading absent/incomplete local model assets.

No `desktop_uia` rerun: this closure changed no desktop code. No package run: runtime inclusion did not change. The S5 A-H end-to-end matrix remains unrun because neither capability is composed. This document makes no claim of an implemented composition or a passing S5 acceptance.
