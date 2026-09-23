# Milestone 10, S2: controlled download and file placement

> **Lumi can now download ONE approved public file (PDF, Word .docx or text) into its own quarantine and, as a separate step, save it under ONE approved name into a folder you approved for saving. It never replaces an existing file, it never opens or runs a download, and it keeps the file's Mark-of-the-Web.**

> **It still cannot choose a path, overwrite, delete, move, open, execute or upload a file.** Project execution is S3. Cross-app preparation is S4.

Status: **S2 COMPLETE (engineering).** S3 through S5 are not started.

```text
M9                                 COMPLETE (merged to main)
Windows signing infrastructure     READY
Production certificate             NOT CONFIGURED
Real-account release               BLOCKED

M10 S1 approved documents + broker COMPLETE
    S2 downloads + placement       COMPLETE (engineering)
    S3 project recipes             NOT STARTED
    S4 cross-app preparation       NOT STARTED
    S5 cross-executor recovery     NOT STARTED
```

Only synthetic files were used. The public-page fixture serves a generated PDF, a fake `MZ` executable (under `.exe` and under `.pdf`), a batch script under `.pdf`, an oversize PDF, a redirect and a 404. No real site, account or document was used.

## 1. The authority

A transfer has one `file_transfer` grant (a new `task_grants.kind`). It is created from four inputs:

* the URL;
* the id of a root with `can_create`;
* one file name;
* a typed intent.

The grant scope binds all of the following, and its SHA-256 digest is re-checked in SQL every time a step is spent:

* **Task and transfer:** the task, and the transfer id (a UUID the runtime minted, which is also the quarantine directory's name).
* **Source:** the canonical source URL and its origin. The URL must pass the runtime's `PublicUrlPolicy` both at creation and again at download.
* **Destination:** the destination root (id, label and identity), and one destination name. The name passes `validate_file_name` (no separators, streams, reserved device names, trailing dots or spaces, or bidi/format characters) and must have the extension `.pdf`, `.docx` or `.txt`.
* **Type:** the expected kind, taken from that extension.
* **Size:** `max_bytes`, at most 10 MB.
* **Overwrite:** `overwrite: Literal[False]`. There is no way to express `true`.
* **Steps and expiry:** exactly two steps (`transfer_download`, `transfer_place`), each spendable once, and an expiry.

Creating the card fetches nothing. The destination must not already exist; the check is case-insensitive through the directory handle.

**The approval is confirmed twice.** First the renderer's card. Then `TransferController.grantTransfer` re-reads the card **from the runtime** and shows a native `dialog.showMessageBox` in main naming the source, the folder, the name, the type, the limit and "never replaces". A compromised renderer cannot approve a download by itself. Download and placement are two further separate clicks.

## 2. Quarantine first (`app/files/quarantine.py`)

`<LOCALAPPDATA>\Lumi\quarantine\<transferId>\` holds a fixed set of files, written in this order:

| File | Written |
| --- | --- |
| `started.json` | O_EXCL, fsynced, **before any request** |
| `payload.part` | while downloading |
| `payload.bin` | renamed from the part file, then given `Zone.Identifier` (`ZoneId=3`, `HostUrl` = origin only) |
| `complete.json` | **last**: length, SHA-256, sniffed kind, content type |

* **Every write is an exclusive create.** Nothing is ever truncated or reopened, and the only rename is `.part` → `.bin`. A source-scan test pins this.
* **Names are never supplied from outside.** The directory is the runtime's UUID and every file name is a constant; nothing from a page, a model or the renderer ever names a quarantine path.
* **The quarantine cannot be approved as a root.** Its root is in the file broker's `forbidden_roots`, and the default location is inside LocalAppData, which is a protected known folder.
* **`read_complete` checks the payload.** It trusts `complete.json` only if the payload's length and hash still match.

## 3. The download (`download_to_quarantine`)

This is the one new browser operation, with target `DOWNLOAD`, effect `DOWNLOAD`, `RECONCILE_BEFORE_RETRY` and `INSPECT_QUARANTINE`. The registry test pins it as the only one of its kind.

**Its input is the URL, the transfer id and `max_bytes`, and nothing else.** There is no path, header, cookie, selector or script.

**The context.** It runs in a fresh, cookie-less context (`accept_downloads=False`, service workers blocked), with the PublicNetworkGuard in **capture mode**:

* the main document's body is kept in memory;
* the request must be a GET with status 200;
* both `content-length` and the body must be within the limit;
* redirects are validated one hop at a time.

Before the request is made, the worker writes `started.json` and sets `submitted = true`.

**Type safety comes from the bytes, never from the name or the content type.** `sniff` refuses:

* executables: `MZ`, ELF, Mach-O;
* shortcuts (`.lnk`);
* OLE containers;
* macro-enabled OOXML;
* scripts: `#!`, `@echo`, `<script`, `<?php`, `<html`, WSF `<job>` and `<package>`;
* anything that is not a PDF, DOCX or text file.

A refused body never reaches `payload.bin`.

**The runtime does not trust the worker's answer.** It re-reads the quarantine: the manifest must verify against the payload bytes and must match the length and hash the worker reported. A verified download of the wrong kind (a DOCX served for `resume.pdf`) is a *known* download: the attempt is SUCCEEDED and the transfer is FAILED with `type_mismatch`, so it is never placeable.

## 4. Placement (`app/files/place.py`)

This is the one file mutation in M10. It is a rename, never a copy-then-delete.

1. **Hold the destination directory.** It is opened with `FILE_FLAG_OPEN_REPARSE_POINT` and **without** `FILE_SHARE_DELETE`. The handle proves:
   * it is not a reparse point;
   * it is a directory;
   * its final path is the verified root;
   * its (volume, 64-bit file id) is the root's recorded identity.
2. **Hold the payload.** It is opened with `GENERIC_READ | DELETE`, sharing **read only**, so no writer can open it while it is held, and none may already hold it open. From that handle, Lumi proves:
   * its final path is inside the quarantine;
   * it has a single link and is not a reparse point;
   * it has the recorded identity and size;
   * it is on the same volume as the destination. A different volume is **refused** (`cross_volume_refused`), never emulated by copying.
   Its bytes are then read **through that same handle**: the SHA-256 must match and the signature must still be the approved kind (review finding 4).
3. **Rename.** `NtSetInformationFile(FileRenameInformation)` with `ReplaceIfExists = FALSE` and `RootDirectory` = the held directory handle. An existing file, including one that differs only in case or an 8.3 alias, gives `STATUS_OBJECT_NAME_COLLISION`, recorded as `destination_exists`.
4. **Re-prove.** From the same handle, check the new final path, and that the identity and size are unchanged.

A same-volume rename keeps the file index and its alternate data streams. As a result:

* **Mark-of-the-Web travels with the file.** Placement also refuses a payload whose `Zone.Identifier` was stripped (`provenance_missing`).
* **"Placed or not" has exactly one true answer.** It is found by file index at the destination versus in the quarantine.

## 5. Lost responses and crashes: evidence, never a second effect

Every step goes through `start_scoped_attempt`: the attempt is committed before any effect. Reconciliation is read-only and **never downloads again**.

| Situation | Evidence | Result |
| --- | --- | --- |
| Crash or lost answer after the download completed | `complete.json` verifies against the payload | SUCCEEDED → QUARANTINED |
| No `started.json` | the reconciler **creates the transfer directory itself** (an atomic tombstone), so the worker's own exclusive `begin` fails for ever | FAILED, authoritative (review finding 2) |
| Directory exists but neither marker is present | a `begin` or a tombstone was interrupted | stays OUTCOME_UNKNOWN |
| `started.json` present, partial or no `complete.json` | the request may have been made | stays OUTCOME_UNKNOWN; effect stays locked |
| Payload changed after completion (hash mismatch) | the manifest no longer verifies | stays OUTCOME_UNKNOWN |
| Crash after the rename, before the commit | the destination has the quarantined file index and the quarantine is empty | SUCCEEDED → PLACED, grant COMPLETED |
| Rename did not happen | the quarantine still has the index and the destination is absent | FAILED → transfer FAILED (`not_placed`) |
| Anything else | — | stays OUTCOME_UNKNOWN |

A repeated `download` or `place` request is a no-op view. Four layers stop a second fetch:

* the idempotency key per step;
* the in-process running set;
* main's per-transfer in-flight set;
* the worker's exclusive `begin`.

The step actions are found by (task, idempotency key), not only through the transfer row. A crash between the attempt's commit and the row update is therefore still reconcilable, and its effect keys are not locked for good (review finding 1).

## 6. The cross-executor effect lock (`action_effect_keys`)

This is the foundation S5 builds on. An action that has a material effect inserts closed-shape keys in the **same transaction** that authorizes it:

* a download holds `download:source:<sha256(url)>` and `file_create:dest:<root>:<casefolded name>`;
* a placement holds the destination key.

It then takes `pg_advisory_xact_lock` on each key in sorted order and checks for a conflict:

* **Same key:** any other action with the same key in EXECUTING, OUTCOME_UNKNOWN or RECONCILING.
* **Global tier:** any `external_mutation` or `project_run` action that is OUTCOME_UNKNOWN or RECONCILING.

A conflict is a 409 `effect_locked`, and nothing is done. Tests show that the same URL under another name, or another URL to the same name, is locked while an earlier transfer is unresolved, and that an unrelated transfer is not.

The generic action routes cannot start, finish or reconcile a transfer step, or plant a `transfer_*` action (`use_transfer_route`, review finding 8).

## 7. Sweep

At startup, the quarantine of transfers older than 24 h is removed. That covers:

* transfers that are PLACED, FAILED or CANCELLED;
* QUARANTINED transfers whose grant was revoked, expired or completed (review finding 6).

The sweep keeps some directories:

* **OUTCOME_UNKNOWN quarantines** are kept as evidence.
* **Reconciliation tombstones** are kept for ever (they are tiny).

It removes only the four fixed file names, then the directory. It refuses a directory that is a reparse point or resolves outside the quarantine root (review finding 5), and it never touches an approved folder.

## 8. Electron

* **Bridge.** It has 8 fixed channels (`createTransfer`, `getTransfer`, `getLatestTransfer`, `grantTransfer`, `declineTransfer`, `downloadTransfer`, `placeTransfer`, `reconcileTransfer`), each checking the sender. None takes a path or an overwrite flag, and none is reachable from voice.
* **Validation in main.** The URL must be http(s) with no credentials. The file name must be just a name. Ids are UUIDs.
* **`transfer-wire.ts`.** It refuses any response carrying an absolute path, and any card claiming `overwrite: true`.
* **Supervisor allowlist.** It pins `/transfers`, `/transfers/latest`, `/transfers/{uuid}` and `/transfers/{uuid}/(grant|revoke|download|place|reconcile)`.
* **`TransferPanel`.** It sits inside the Documents panel and offers only folders with "Save new downloads here". The card renders every value as inert text. When a step is uncertain, the only button is "Check what happened".

## 9. Tests

**Python:**

* **`test_transfers_service`** (35 tests), against real PostgreSQL and a real NTFS folder, with a fake worker following the real quarantine protocol. It covers:
  * the happy path, with MOTW preserved;
  * a duplicate request;
  * unsafe or reserved names and executable, script, macro and shortcut extensions;
  * read-only folders;
  * an existing destination, and an overwrite that appears in the race window;
  * URLs outside the policy;
  * no trusted click;
  * a wrong kind (`type_mismatch`), a worker-refused type, a quarantined file swapped for an executable, stripped provenance, and a same-size rewrite after the check;
  * a destination folder replaced after approval;
  * a lost answer after the effect;
  * a dispatch that never reached the worker, and one arriving after reconciliation;
  * a partial transfer, and the effect lock across transfers;
  * a runtime death mid-download, and a crash between the attempt commit and the row update;
  * a crash after the rename before the commit, and a rename that did not happen;
  * a hash mismatch before reconciliation;
  * cross-task use;
  * the sweep: kept evidence, junction refusal, and a download finished after a revoke;
  * the generic routes.
* **`test_download_worker_browser`** (11 tests, real worker and Chromium). It covers:
  * a PDF landing in quarantine with MOTW;
  * `.exe`, a disguised executable, a script and HTML refused by their bytes;
  * the size limit;
  * a validated redirect, and a redirect to an unapproved origin (the canary is never contacted);
  * a 404;
  * at most one fetch per transfer id;
  * a URL outside the policy creating no marker.
* **Also:** `test_migration_0018` (constraints and downgrade refusal), the registry pin, and source scans (only `quarantine.py` and `place.py` may write, and each is pinned; only `sniff` is imported by the transfer modules).

**TypeScript:** `transfer-controller.test` (21 tests) covers:

* the payload shape;
* path and URL refusals;
* the native re-confirmation, a cancelled dialog and a stale revision;
* double-click → one download;
* error projection without paths;
* wire refusals: absolute path, overwrite, unknown phase or kind;
* an inert card, and "Check what happened" as the only action when a step is uncertain.

## 10. Independent adversarial review (fresh Claude reviewer; no external model)

Brief: "Try to turn browser download into arbitrary filesystem write, overwrite an existing file, smuggle an executable/script through extension confusion, escape via reparse/junction race, duplicate a download after a crash, or strip provenance."

**No Critical or High finding.** The reviewer confirmed that no path reaches placement except `<held root handle>\<validated name>`, and it found no overwrite path, no extension-confusion bypass and no provenance strip. Every finding was verified by the lead against the code:

| # | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| 1 | Medium | The step action id was written to the transfer row in a second transaction. A crash between the two commits left an OUTCOME_UNKNOWN action that `reconcile` could not find, with its effect keys locked for good. | Actions are found by (task, idempotency key) and backfilled into the row. Regression test. |
| 2 | Medium | "No `started.json`" was treated as proof of no request. A dispatch the runtime had given up on could still reach the worker after reconciliation released the lock. | Reconciliation creates the transfer directory itself as a tombstone (`claim_absence`), so the worker's exclusive `begin` fails for ever. Only then is absence authoritative, and tombstones survive the sweep. Regression test. |
| 3 | Low | A type-refused body is FAILED even though the GET happened. | Documented: the DOWNLOAD effect is "a payload was kept". A refused body keeps nothing, and the started marker records that a request was made. |
| 4 | Low | The content check read by path before the rename handle was opened. | The bytes are hashed and sniffed **through the rename handle** (`GENERIC_READ \| DELETE`, share read only). Regression test with a same-size in-place rewrite. |
| 5 | Low | The sweep deleted by path, and a junction swapped in could redirect four fixed-name unlinks. | The sweep refuses a reparse point or a directory resolving outside the quarantine root. Regression test with a real junction. |
| 6 | Low | A placement reconciled SUCCEEDED did not complete the grant or record the placed identity. A revoke during a download, or a failed placement, left a QUARANTINED row nothing could clean. | Reconciled placement records the identity and completes the grant. A definite placement failure ends the transfer as FAILED, and `cleanable` includes QUARANTINED rows under a closed or expired grant. Regression test. |
| 7 | Low | A same-volume rename keeps the quarantine's ACL instead of inheriting the destination folder's. | Documented (functional, not an exploit: the file is owned by the same user). |
| 8 | Low | The generic `/actions/{id}/attempts` and `/reconciliation/finish` routes could start or settle a keyed transfer action around the lock (runtime-internal, bearer only). | The generic routes refuse `transfer_*` tools and proposals (`use_transfer_route`). Regression test. |
| 9 | Low (speculative) | A root approved before the quarantine was configured is not re-checked against it at use. | Documented. The default quarantine is inside LocalAppData, a protected known folder refused at registration both as a root and as an ancestor. Only a custom `LUMI_DOWNLOAD_QUARANTINE_ROOT` (a dev and test setting) could predate a root. |

## 11. Validation

* `uv run mypy`: clean (312 files).
* `uv run pytest -m "not browser and not desktop_uia"`: see the S2 closing commit message; the known baseline `test_booking_routes_without_a_worker_answer_503` is deselected.
* Browser regressions (`test_download_worker_browser`, `test_public_page_worker`, `test_network_guard_methods`, `test_egress_broker_browser`, `test_research_browser`): see the closing commit message.
* `npm.cmd run typecheck`: clean.
* `npx vitest run`: all pass except the three known machine baselines (`real-inference`, `tokenizer-pack`, `accessibility`).
* `npm.cmd run build`: clean.

## 12. Residual risks

* **Not a sandbox against the same Windows user.** A process running as the same user can still act inside the quarantine (for example, strip `Zone.Identifier` between the check and the rename, since streams are separate file objects), or hold a writable memory mapping. Placement re-proves identity and bytes through its own handles, and refuses on any mismatch it can see.
* **The placed file keeps the quarantine's ACL** (finding 7).
* **Placement is same-volume only.** A folder on another drive is refused, not supported.
* **The download is one main-document body** of at most 10 MB. Pages that trigger downloads through script are out of scope.
* **No production-signed installed validation exists** (certificate not configured).
