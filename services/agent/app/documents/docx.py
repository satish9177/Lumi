"""DOCX text with the standard library only: `zipfile` for the container, `ElementTree` for one XML part.

What is refused, before any XML is parsed:

* more members, more total expansion, a larger member or a higher per-member compression ratio than the
  limits allow (a ZIP bomb);
* a macro-enabled package (`vbaProject.bin`, a `macroEnabled` content type) -- no macro is ever run
  anyway, but a macro document is not what a person means by "my resume";
* any `<!DOCTYPE` or `<!ENTITY` in an XML part Lumi reads, so no entity is ever expanded (no
  billion-laughs, no external entity).

What is ignored and never touched: external relationships (hyperlinks, an attached remote template,
linked OLE objects) are *counted* so the result can say so, and never fetched; field instructions
(`w:instrText`, e.g. `INCLUDETEXT`) are skipped, never evaluated; embedded objects and images are not
read. Only `word/document.xml` contributes text.
"""

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Final
from xml.etree import ElementTree

from app.documents.errors import ExtractionRefusal
from app.documents.limits import (
    MAX_COMPRESSION_RATIO,
    MAX_TEXT_CHARS,
    MAX_ZIP_MEMBER_BYTES,
    MAX_ZIP_MEMBERS,
    MAX_ZIP_UNCOMPRESSED_BYTES,
)
from app.documents.sniff import DOCX_MAIN
from app.documents.text import clean

_W: Final = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_REL: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DECLARATIONS: Final = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_XML_DECLARATION: Final = re.compile(rb"^\s*<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9._-]+)[\"']")
_MAIN_PART: Final = "word/document.xml"
_RELS_PART: Final = "word/_rels/document.xml.rels"


@dataclass
class DocxText:
    text: str
    truncated: bool
    flags: dict[str, int] = field(default_factory=dict)


def _check_archive(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_ZIP_MEMBERS:
        raise ExtractionRefusal("archive_too_large")
    total = 0
    by_name: dict[str, zipfile.ZipInfo] = {}
    for info in members:
        if info.flag_bits & 0x1:
            raise ExtractionRefusal("malformed_document")  # an encrypted member
        if info.file_size > MAX_ZIP_MEMBER_BYTES:
            raise ExtractionRefusal("archive_too_large")
        if info.file_size > 0 and (info.compress_size == 0 or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO):
            raise ExtractionRefusal("archive_bomb")
        total += info.file_size
        if total > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ExtractionRefusal("archive_too_large")
        lowered = info.filename.casefold()
        if "vbaproject.bin" in lowered or "vbadata.xml" in lowered:
            raise ExtractionRefusal("macro_document_refused")
        by_name[lowered] = info
    return by_name


def _read_xml(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> ElementTree.Element:
    # Read with an explicit bound, even though the header's size was checked: a lying header must not
    # let the decompressor run past the limit.
    with archive.open(info) as handle:
        raw = handle.read(MAX_ZIP_MEMBER_BYTES + 1)
    if len(raw) > MAX_ZIP_MEMBER_BYTES:
        raise ExtractionRefusal("archive_bomb")
    # UTF-8 only (S1 review finding 4): a UTF-16 part would hide `<!DOCTYPE` from the byte scan below while
    # the XML parser still honoured it. Word writes UTF-8; anything else is refused, not decoded.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw:
        raise ExtractionRefusal("xml_entities_refused")
    declaration = _XML_DECLARATION.match(raw)
    if declaration is not None and declaration.group(1).lower() not in (b"utf-8", b"utf8"):
        raise ExtractionRefusal("xml_entities_refused")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ExtractionRefusal("malformed_document") from None
    if _DECLARATIONS.search(raw):
        raise ExtractionRefusal("xml_entities_refused")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise ExtractionRefusal("malformed_document") from None


def extract_docx(data: bytes) -> DocxText:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError):
        raise ExtractionRefusal("malformed_document") from None
    with archive:
        members = _check_archive(archive)
        types_info = members.get("[content_types].xml")
        if types_info is None:
            raise ExtractionRefusal("malformed_document")
        types = _read_xml(archive, types_info)
        declared = " ".join(
            (element.get("ContentType") or "").casefold() for element in types.iter() if element.get("ContentType")
        )
        if "macroenabled" in declared:
            raise ExtractionRefusal("macro_document_refused")
        if DOCX_MAIN not in declared:
            raise ExtractionRefusal("unsupported_format")
        main_info = members.get(_MAIN_PART)
        if main_info is None:
            raise ExtractionRefusal("malformed_document")
        flags: dict[str, int] = {"external_relationships_ignored": 0, "field_instructions_ignored": 0}
        rels_info = members.get(_RELS_PART)
        if rels_info is not None:
            relationships = _read_xml(archive, rels_info)
            flags["external_relationships_ignored"] = sum(
                1 for element in relationships.iter(f"{_REL}Relationship") if (element.get("TargetMode") or "").casefold() == "external"
            )
        document = _read_xml(archive, main_info)
    parts: list[str] = []
    size = 0
    truncated = False
    for element in document.iter():
        tag = element.tag
        piece: str | None = None
        if tag == f"{_W}t" and element.text:
            piece = element.text
        elif tag == f"{_W}instrText":
            flags["field_instructions_ignored"] += 1
        elif tag == f"{_W}tab":
            piece = "\t"
        elif tag in (f"{_W}br", f"{_W}cr"):
            piece = "\n"
        elif tag == f"{_W}p":
            piece = "\n"
        if piece is None:
            continue
        if size + len(piece) > MAX_TEXT_CHARS:
            truncated = True
            break
        parts.append(piece)
        size += len(piece)
    # `iter()` yields a paragraph before its runs, so a paragraph break is emitted at its start. That is
    # harmless for plain text: `clean` collapses the resulting blank lines.
    return DocxText(text=clean("".join(parts)), truncated=truncated, flags=flags)
