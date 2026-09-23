"""The download quarantine (Milestone 10 S2): one Lumi-owned directory per transfer.

```text
<quarantine_root>/<transfer_id>/
    started.json            written and flushed BEFORE any network request is made
    payload.part            streamed bytes while the transfer is in progress
    payload.bin             the complete payload (renamed from .part), with a Zone.Identifier stream
    complete.json           written LAST: length, SHA-256, sniffed kind, content type
    absent.json             written only by RECONCILIATION, in a directory it created itself (see below)
```

The directory name is the transfer's UUID and the file names are fixed, so no caller -- not the runtime's
request, not a page, not a model -- ever names a path in the quarantine. The payload has no extension, is
never opened, never executed and never handed to a provider by path; the only way out is the S2 placement
step, which re-verifies it and moves it with an atomic, no-overwrite rename.

**What the markers prove, and what they do not.** No `started.json` alone does NOT prove that no request
was made: a dispatch the runtime gave up on may still reach the worker later (S2 review finding 2). Absence
is made authoritative by `claim_absence`: reconciliation creates the transfer directory itself, which makes
the worker's own `begin` (an exclusive create of the same directory, before any request) fail for ever.
Only then is "no request was made" a fact. `started.json`
without a verifying `complete.json` means the request may have been made and the outcome is unknown.
`complete.json` whose length and hash match `payload.bin` means the download completed.
"""

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

STARTED: Final = "started.json"
ABSENT: Final = "absent.json"
PARTIAL: Final = "payload.part"
PAYLOAD: Final = "payload.bin"
COMPLETE: Final = "complete.json"
ZONE_STREAM: Final = "Zone.Identifier"
#: A quarantined transfer that was never placed is removed after this long.
QUARANTINE_LIFETIME_SECONDS: Final = 24 * 60 * 60


def default_quarantine_root() -> str:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return str(Path(base) / "Lumi" / "quarantine")


def transfer_directory(root: str, transfer_id: uuid.UUID) -> Path:
    """The ONLY way a quarantine path is formed: a configured root plus a UUID the runtime minted."""
    if not isinstance(transfer_id, uuid.UUID):  # pragma: no cover - typed callers.
        raise ValueError("transfer id must be a UUID")
    return Path(root) / str(transfer_id)


@dataclass(frozen=True, slots=True)
class CompleteManifest:
    length: int
    sha256: str
    kind: str
    content_type: str


def _write_json(path: Path, payload: dict[str, object]) -> None:
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def begin(root: str, transfer_id: uuid.UUID, *, source_digest: str) -> Path:
    """Create the transfer's directory and its `started` marker. Refuses if it already exists: a transfer id
    is used for exactly one network request, ever."""
    Path(root).mkdir(parents=True, exist_ok=True)
    directory = transfer_directory(root, transfer_id)
    directory.mkdir(exist_ok=False)
    _write_json(directory / STARTED, {"transfer_id": str(transfer_id), "source_digest": source_digest})
    return directory


def claim_absence(root: str, transfer_id: uuid.UUID) -> bool | None:
    """Reconciliation's tombstone. True: this call (or an earlier reconciliation) owns the transfer directory,
    so no request was or ever can be made for this id. False: the worker's `begin` created it first. None:
    the directory exists without either marker (a `begin` or a tombstone was interrupted) -- unknown."""
    Path(root).mkdir(parents=True, exist_ok=True)
    directory = transfer_directory(root, transfer_id)
    try:
        directory.mkdir(exist_ok=False)
    except FileExistsError:
        if (directory / STARTED).is_file():
            return False
        return True if (directory / ABSENT).is_file() else None
    _write_json(directory / ABSENT, {"transfer_id": str(transfer_id)})
    return True


def zone_identifier(host_url: str) -> str:
    """Mark-of-the-Web for the Internet zone (3). Only the origin is recorded as HostUrl."""
    return f"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl={host_url}\r\n"


def finish(directory: Path, data: bytes, *, kind: str, content_type: str, host_url: str) -> CompleteManifest:
    """Write the payload, give it Mark-of-the-Web, then (last) the completion manifest."""
    partial = directory / PARTIAL
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view[: 1 << 20])
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    payload = directory / PAYLOAD
    os.rename(partial, payload)
    if os.name == "nt":
        stream = os.open(f"{payload}:{ZONE_STREAM}", os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
        try:
            os.write(stream, zone_identifier(host_url).encode("ascii", errors="replace"))
        finally:
            os.close(stream)
    manifest = CompleteManifest(length=len(data), sha256=hashlib.sha256(data).hexdigest(), kind=kind, content_type=content_type)
    _write_json(
        directory / COMPLETE,
        {"length": manifest.length, "sha256": manifest.sha256, "kind": manifest.kind, "content_type": manifest.content_type},
    )
    return manifest


def started(root: str, transfer_id: uuid.UUID) -> bool:
    return (transfer_directory(root, transfer_id) / STARTED).is_file()


def read_complete(root: str, transfer_id: uuid.UUID) -> CompleteManifest | None:
    """The completion manifest, but only if the payload on disk still matches it exactly."""
    directory = transfer_directory(root, transfer_id)
    try:
        raw = json.loads((directory / COMPLETE).read_text(encoding="utf-8"))
        manifest = CompleteManifest(
            length=int(raw["length"]), sha256=str(raw["sha256"]), kind=str(raw["kind"]), content_type=str(raw["content_type"])
        )
        with open(directory / PAYLOAD, "rb") as handle:
            data = handle.read(manifest.length + 1)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if len(data) != manifest.length or hashlib.sha256(data).hexdigest() != manifest.sha256:
        return None
    return manifest


def has_zone_identifier(path: str) -> bool:
    if os.name != "nt":  # pragma: no cover
        return True
    try:
        with open(f"{path}:{ZONE_STREAM}", "rb") as handle:
            return b"ZoneId=3" in handle.read(4096)
    except OSError:
        return False
