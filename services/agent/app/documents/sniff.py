"""Decide what a byte string *is* from its content, never from its name alone (Milestone 10 S1/S2).

The extension a file carries is a claim. `sniff` reads the signature and, for ZIP containers, the
declared OOXML content type. A caller that knows the name also passes the extension's claim, and a
disagreement is `type_mismatch`: a PDF named `resume.docx`, or an executable named `resume.pdf`, is
refused rather than trusted either way.

`dangerous_kind` names byte strings that must never be placed, opened or extracted: Windows
executables, scripts and shortcuts, and macro-enabled Office documents.
"""

import io
import zipfile
from typing import Final

from app.documents.errors import ExtractionRefusal

_PDF: Final = b"%PDF-"
_ZIP: Final = b"PK\x03\x04"
_MZ: Final = b"MZ"
_LNK: Final = b"L\x00\x00\x00\x01\x14\x02\x00"
_OLE: Final = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ELF: Final = b"\x7fELF"
_MACHO: Final = (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe")
_SCRIPT_PREFIXES: Final = (b"#!", b"@echo", b"<script", b"<?php", b"<job", b"<package", b"<html", b"<!doctype html")

DOCX_MAIN: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_MACRO_MARKERS: Final = ("macroenabled", "vbaproject", "vbadata")

EXTENSION_CLAIMS: Final = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "txt"}


def _ooxml_kind(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        if len(archive.infolist()) > 1000:
            return "zip"
        names = [name.casefold() for name in archive.namelist()]
        if any(marker in name for name in names for marker in ("vbaproject.bin", "vbadata.xml")):
            return "macro_office"
        if "[content_types].xml" not in names:
            return "zip"
        info = archive.getinfo(next(n for n in archive.namelist() if n.casefold() == "[content_types].xml"))
        if info.file_size > 1024 * 1024:
            return "zip"
        declared = archive.read(info).decode("utf-8", errors="replace").casefold()
    except Exception:  # noqa: BLE001 - untrusted archive bytes: any parser surprise (zlib.error, EOFError,
        # a corrupt member) is simply "not a DOCX", a clean refusal and never a 500 (S1 review finding 5).
        return "zip"
    if any(marker in declared for marker in _MACRO_MARKERS):
        return "macro_office"
    if DOCX_MAIN in declared:
        return "docx"
    return "zip"


def _looks_like_text(data: bytes) -> bool:
    if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return True
    sample = data[:65536]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as error:
        # A multi-byte sequence cut at the sample edge is still text.
        if error.start < len(sample) - 4:
            return False
    controls = sum(1 for byte in sample if byte < 9 or 13 < byte < 32)
    return controls <= len(sample) // 100


def sniff(data: bytes) -> str:
    """One of `pdf`, `docx`, `txt`, `executable`, `script`, `shortcut`, `macro_office`, `ole`, `zip`, `unknown`."""
    head = data[:512]
    lowered = head.lstrip().lower()
    if head.startswith(_MZ) or head.startswith(_ELF) or head.startswith(_MACHO):
        return "executable"
    if head.startswith(_LNK):
        return "shortcut"
    if head.startswith(_OLE):
        return "ole"
    if head.startswith(_PDF):
        return "pdf"
    if head.startswith(_ZIP):
        return _ooxml_kind(data)
    if any(lowered.startswith(prefix) for prefix in _SCRIPT_PREFIXES):
        return "script"
    if _looks_like_text(data):
        return "txt"
    return "unknown"


def dangerous_kind(kind: str) -> bool:
    return kind in {"executable", "script", "shortcut", "macro_office", "ole"}


def check_claim(data: bytes, extension: str | None) -> str:
    """The sniffed supported format, agreeing with the extension's claim when there is one."""
    kind = sniff(data)
    if kind == "macro_office":
        raise ExtractionRefusal("macro_document_refused")
    if kind not in ("pdf", "docx", "txt"):
        raise ExtractionRefusal("unsupported_format")
    if extension is not None:
        claimed = EXTENSION_CLAIMS.get(extension.casefold())
        if claimed is None or claimed != kind:
            raise ExtractionRefusal("type_mismatch")
    return kind
