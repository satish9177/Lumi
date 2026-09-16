# Milestone 4 independent review record

Status: implementation in progress; not an acceptance declaration.

## Baseline

Astra independently executed the clean Milestone 3 baseline at d4edfd7:

| Check | Result |
| --- | --- |
| npm.cmd run typecheck | Passed |
| npm.cmd run build | Passed |
| npm.cmd test | 1511 passed, 17 skipped, 1 assertion failure; 2 collection errors |
| uv run pytest -v | 319 passed, including real Chromium and hard-kill scenarios |
| uv run mypy | Passed, 72 files |

The Vitest baseline failures are the missing local CLIP vocabulary in
real-inference.test.ts, invalid/missing tokenizer pack in tokenizer-pack.test.ts,
and the CRLF-sensitive CSS assertion in accessibility.test.tsx:73.

## Review findings during implementation

1. BLOCKER (draft, not executed): parent_watchdog.py used os.kill(pid, 0)
   as a liveness probe. On Windows that can terminate the target process.
   Sent to Sol for retained Windows process-handle implementation and a live
   child regression. See Python 3.12 os.kill documentation:
   https://docs.python.org/3.12/library/os.html#os.kill.
2. MEDIUM (draft): string hmac.compare_digest raises on non-ASCII bearer input.
   Sent to Sol for safe byte comparison / rejection and malformed-token test.

Fix status and exact final source locations will be recorded after inspection.

## Required final verification

- Renderer uses only typed agent bridge; no sidecar/worker credentials or URLs.
- Main validates exact frame/sender, payload shape, response schema, generation.
- Fixed process executable/arguments, shell disabled, minimal child environment.
- All runtime endpoints authenticate before routing; Host/Origin fail closed.
- All approval values derive from immutable persisted proposal; renderer supplies
  action ID and expected revision only; transaction enforces revision.
- Approval remains expiring and single-use; durable attempt precedes dispatch.
- No mutation retries; unknown stays unknown until authoritative read-only lookup.
- Timeline reload/reconnect preserves sequence and task identity without duplicates.
- Changed-resource UX reports approved/current facts and that no booking occurred.
- Hard restart cleans owned descendants and reuses durable PostgreSQL state.
- Both Electron E2Es prove 1 task / 1 action / 1 attempt / 1 submission / 1 booking.
- Re-run full checks after critical fixes; separate existing baseline failures.

## Independent resume probes

Astra executed direct probes against the worktree implementation:

- Retained Windows process handle detects live/dead child without terminating it.
- Windows kill-on-close job terminates a real descendant after owner hard-kill.
- ASGI authentication precedes routing for health, docs, OpenAPI, tasks, lifecycle
  and unknown paths; missing auth returns 401, supplied Origin returns 403,
  hostile Host returns 400, non-ASCII bearer input is refused without an exception.

These validate the draft fixes, but do not replace committed regression suites or
actual Electron acceptance testing. Sol also confirmed and Astra backed up/restored
three accidental partial draft writes to the primary checkout; coding remains in
the isolated worktree.

## Supervisor draft review

- HIGH: deferred port allocation lets launch spawn after stop() has returned.
  Astra reproduced `{spawnsAfterStop:1, finalStatus:"starting"}` using the actual
  transpiled supervisor with a deferred findPort. Sent to Sol for serialized
  lifecycle / cancellation guards and regression.
- HIGH: stop currently reports stopped after an unsuccessful second exit wait;
  replacement must not start until old-process exit is confirmed.
- MEDIUM: exitCode alone misses signalCode termination; track actual exit.
- MEDIUM: bare taskkill.exe uses executable search; use a fixed trusted path or
  owned-process termination with the Windows job.
- MEDIUM: token-bearing fetches must refuse redirects explicitly.

All remain draft review items pending inspection of finalized source and tests.

## Slice 1 progression gate

Source review approved for progression after the focused checks reported by Sol:
TypeScript passed; supervisor 6/6; Python focused run 47 passed plus a test-only
NameError corrected, then lifecycle 4/4 passed. Final combined check / mypy and
commit evidence remain to be attached. Astra independently corroborated the
supervisor tests, mandatory authentication behavior, process-handle safety, and
real Windows descendant cleanup. This is a slice gate, not milestone acceptance.

Next scope: desktop-specific safe DTOs, narrow runtime client, contract drift
checks, typed IPC/preload and exact sender/frame validation. Full UI follows a
separate trust-boundary review. No generic request/dispatch/result-write surfaces.

## Follow-up lifecycle security finding

HIGH: main selected a free loopback port, closed its listener, and immediately
sent a bearer credential while waiting for the child to bind. Another local
listener could win the port race and receive the bootstrap credential. Sent to
Sol for bounded readiness confirmation over the owned child process pipe after
actual bind, before any authenticated HTTP request; child exit must invalidate
readiness. No raw child output may cross to renderer or logs. This newly found
issue must be resolved before the next trust-boundary gate / final integration.

## Slice 1 committed evidence

Sol commit: `72d4799` (`feat(agent): secure runtime supervision`).
Reported executed checks: Python focused combined 48 passed; lifecycle real-process
4 passed; authenticated persistence 2 passed; supervisor Vitest 8 passed;
TypeScript passed; mypy passed for 67 files. Worktree clean at handoff.

Readiness-token finding fixed with fd3 post-bind signal, self.started/should_exit
guard, and supervisor tests confirming no credential before readiness or after
an early child exit. Astra separately verified actual Windows Node-to-Python fd3
communication and the new security test cases (one stale stdio assertion was then
corrected by Sol). Earlier lifecycle findings were fixed in the inspected source.

Gate accepted for slices 2–4. Remaining integration obligations include owned
browser-worker lifecycle, remaining subprocess auth adaptation, real Electron
acceptance, full-suite checks, and final independent review. Packaged Python
bundling remains explicitly deferred by the requested scope.
