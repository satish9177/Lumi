"""Milestone 10 S2: controlled downloads and file placement.

```text
trusted card: source URL + origin, what it is for, destination folder (CREATE) + file name, size and type
limits, NO overwrite, expiry  ->  trusted click: a `file_transfer` grant (task_grants), ACTIVE
   -> step 1 `transfer_download`: a step authorization from that grant -> the worker fetches the URL into
      <quarantine>/<transfer_id>/ (never a user folder, never opened) -> verified by length + SHA-256
   -> step 2 `transfer_place`: a second step authorization -> sniff again, refuse dangerous bytes -> atomic
      no-overwrite rename into the approved folder, relative to a held directory handle
```

The scope names exactly one source URL, one destination folder and one validated file name. There is no
field for a path, a header, a cookie, an overwrite choice or a second destination, and a model can supply
none of them: the card is built from what the person typed and chose in trusted UI.
"""

import hashlib
import re
import uuid
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.digest import canonical_json
from app.files.names import FileNameRefusal, validate_file_name

FILE_TRANSFER_KIND: Final = "file_transfer"
FILE_TRANSFER_POLICY_VERSION: Final = "file-transfer-v1"
TRANSFER_TASK_TYPE: Final = "file_transfer_task"
TOOL_DOWNLOAD: Final = "transfer_download"
TOOL_PLACE: Final = "transfer_place"
#: The one browser operation a transfer dispatches.
DOWNLOAD_TO_QUARANTINE: Final = "download_to_quarantine"
DEFAULT_MAX_BYTES: Final = 10 * 1024 * 1024
DEFAULT_GRANT_TTL_SECONDS: Final = 10 * 60
MAX_INTENT_CHARS: Final = 120
ALLOWED_TYPES: Final = ("pdf", "docx", "txt")
EXTENSION_KIND: Final = {".pdf": "pdf", ".docx": "docx", ".txt": "txt"}

_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")


class TransferRefusal(ValueError):
    """`code` is stable; it never carries a URL, a path or a file name."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The transfer was refused ({code}).")
        self.code = code


STATE_CODES: Final = frozenset(
    {
        "transfer_not_found",
        "grant_not_found",
        "grant_not_pending",
        "grant_not_active",
        "grant_expired",
        "grant_changed",
        "root_not_found",
        "root_revoked",
        "root_changed",
        "root_unavailable",
        "permission_missing",
        "destination_exists",
        "wrong_phase",
        "effect_locked",
        "quarantine_changed",
        "download_not_verified",
        "nothing_to_reconcile",
    }
)


def validate_destination_name(value: object) -> tuple[str, str]:
    """A single file name with a supported extension. Returns (name, expected kind)."""
    try:
        name = validate_file_name(value)
    except FileNameRefusal as refusal:
        raise TransferRefusal(refusal.code) from None
    extension = ("." + name.rsplit(".", 1)[-1].casefold()) if "." in name else ""
    kind = EXTENSION_KIND.get(extension)
    if kind is None:
        # .exe, .msi, .ps1, .lnk, .docm, ... and anything else: M10 places documents only.
        raise TransferRefusal("destination_type_refused")
    return name, kind


def validate_intent(value: object) -> str:
    if not isinstance(value, str):
        raise TransferRefusal("intent_invalid")
    text = " ".join(value.split())
    if not text or len(text) > MAX_INTENT_CHARS or _CONTROL.search(text):
        raise TransferRefusal("intent_invalid")
    return text


def source_digest(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class TransferScope(BaseModel):
    """Exactly what one `file_transfer` grant authorises. Immutable once inserted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["file_transfer"] = FILE_TRANSFER_KIND
    policy_version: Literal["file-transfer-v1"] = FILE_TRANSFER_POLICY_VERSION
    task_id: uuid.UUID
    transfer_id: uuid.UUID
    #: The source browser/session: a fresh, cookie-less context under the public destination policy.
    source_session: Literal["public_ephemeral"] = "public_ephemeral"
    source_url: str = Field(min_length=8, max_length=2048)
    source_origin: str = Field(min_length=8, max_length=300)
    intent: str = Field(min_length=1, max_length=MAX_INTENT_CHARS)
    dest_root_id: uuid.UUID
    dest_root_label: str = Field(min_length=1, max_length=64)
    dest_name: str = Field(min_length=1, max_length=255)
    expected_kind: Literal["pdf", "docx", "txt"]
    allowed_types: tuple[str, ...] = ALLOWED_TYPES
    max_bytes: int = Field(ge=1, le=DEFAULT_MAX_BYTES)
    overwrite: Literal[False] = False
    #: One download step and one placement step. Never a second of either.
    steps: tuple[str, ...] = (TOOL_DOWNLOAD, TOOL_PLACE)

    @field_validator("allowed_types")
    @classmethod
    def _fixed_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != ALLOWED_TYPES:
            raise ValueError("the allowed types are a fixed, closed set")
        return value

    @field_validator("steps")
    @classmethod
    def _fixed_steps(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != (TOOL_DOWNLOAD, TOOL_PLACE):
            raise ValueError("the steps are a fixed, closed pair")
        return value

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json")).encode("utf-8")).hexdigest()
