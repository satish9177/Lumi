# Milestone 4 implementation and review plan

Baseline: `d4edfd7` (Milestone 3); primary checkout clean before work.

## Ownership

Astra owns architecture, integration, and final independent review. GPT-5.6 Sol
implements sequential slices in the isolated `codex/m4-secure-runtime-bridge`
worktree. Critical main/preload/shared/root changes are delegated there for review;
no other implementation agent edits those surfaces. Supporting agents are read-only.

## Review gates

1. Mandatory runtime authentication and loopback restrictions; controlled sidecar
   launch, bounded restart, credential rotation, shutdown and orphan prevention.
2. Narrow main-owned domain client and safe desktop response projections; Python /
   TypeScript contract drift detection; strict request and response validation.
3. Typed preload and IPC wiring with exact sender/frame validation. Renderer has
   neither runtime addresses nor credentials nor generic dispatch capability.
4. Focused task panel and persisted approval preview, approval by action ID and
   expected revision only, honest changed-resource and unknown-outcome states.
5. Persisted task restoration and ordered event replay; real Electron normal and
   lost-response/hard-restart integration scenarios.
6. Sol integration report, fresh Astra source/diff review, concrete fixes by Sol,
   and independent final verification.

## Acceptance evidence

Run `npm.cmd run typecheck`, `npm.cmd test`, `npm.cmd run build`, runtime
`uv run pytest -v`, `uv run mypy`, `uv run alembic upgrade head`, and new Electron
integration tests. Keep baseline failures and environment blockers separate.

Both booking scenarios must establish one task, one action, one attempt, one
browser submission, one booking and zero duplicates. The restart scenario must
restore the same task and reconcile read-only without submitting again.

## Constraints

Existing local tools remain owned by PendingActionStore; durable browser actions
remain owned exclusively by the Python ledger. No planner, voice integration,
memory, arbitrary sites, payments, generic automation, or Milestone 5 work.

Preserve proposal immutability, revision/digest-bound single-use approvals,
persist-before-dispatch ordering, worker isolation, authoritative reconciliation,
and the distinction between FAILED and OUTCOME_UNKNOWN. All mutations/external
operations require explicit renderer confirmation. No automatic mutation retries.
