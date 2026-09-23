"""A deliberately small, bounded PDF text extractor using only the standard library (Milestone 10 S1).

Lumi has no PDF dependency, and M10 does not add one. This module reads the reviewed subset of PDF that
carries a text layer, and refuses the rest instead of guessing:

```text
supported                                      refused / ignored
indirect objects, incremental updates          /Encrypt (encrypted_pdf)
object streams (/Type /ObjStm)                 more than MAX_PAGES pages (too_many_pages)
/FlateDecode, /ASCIIHexDecode, /ASCII85Decode  any other filter: that stream is skipped
the page tree (/Root /Pages /Kids)             Form XObjects, inline images, annotations: skipped
Tj  TJ  '  "  and Td TD T* Tm line breaks      no text at all (an image-only scan): unsupported_pdf
/ToUnicode bfchar + bfrange; WinAnsi/MacRoman  /JavaScript /OpenAction /Launch /URI /EmbeddedFile:
                                                counted as active_content_ignored, never acted on
```

Every loop is bounded: object count, nesting depth, decoded bytes (whole document and per stream),
content operators and text characters. Nothing is executed, fetched, rendered or written.
"""

import base64
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final, Union

from app.documents.errors import ExtractionRefusal
from app.documents.limits import (
    MAX_PAGES,
    MAX_PDF_CONTENT_OPERATIONS,
    MAX_PDF_DECODED_BYTES,
    MAX_PDF_NESTING,
    MAX_PDF_OBJECTS,
    MAX_PDF_STREAM_BYTES,
    MAX_TEXT_CHARS,
)
from app.documents.text import clean

_WHITESPACE: Final = b" \t\r\n\x0c\x00"
_DELIMITERS: Final = b"()<>[]{}/%"
_OBJECT_HEADER: Final = re.compile(rb"(?<![0-9])(\d{1,10})[ \t\r\n\x0c\x00]+(\d{1,5})[ \t\r\n\x0c\x00]+obj(?![A-Za-z])")
_ACTIVE_KEYS: Final = ("JavaScript", "JS", "OpenAction", "Launch", "URI", "EmbeddedFile", "AA", "SubmitForm")
_MAX_CMAP_ENTRIES: Final = 65_536
_MAX_REF_HOPS: Final = 16


class Name(str):
    """A PDF name (`/Font`), kept distinct from a string."""


class Keyword(str):
    """A bare token: `true`, `null`, `obj`, or a content-stream operator."""


@dataclass(frozen=True, slots=True)
class Ref:
    number: int
    generation: int


@dataclass
class Stream:
    dictionary: dict[str, "PdfObject"]
    raw: bytes


PdfObject = Union[None, bool, int, float, bytes, Name, Keyword, Ref, list["PdfObject"], dict[str, "PdfObject"], Stream]


@dataclass
class PdfText:
    text: str
    pages: int
    truncated: bool
    flags: dict[str, int] = field(default_factory=dict)


def _refuse(code: str) -> ExtractionRefusal:
    return ExtractionRefusal(code)


# ---- the lexer / parser ----------------------------------------------------------------------


class _Parser:
    def __init__(self, data: bytes, position: int = 0) -> None:
        self.data = data
        self.position = position

    def at_end(self) -> bool:
        return self.position >= len(self.data)

    def skip_space(self) -> None:
        data = self.data
        while self.position < len(data):
            byte = data[self.position]
            if byte in _WHITESPACE:
                self.position += 1
            elif byte == 0x25:  # '%' comment to end of line
                while self.position < len(data) and data[self.position] not in b"\r\n":
                    self.position += 1
            else:
                return

    def _token(self) -> bytes:
        start = self.position
        data = self.data
        while self.position < len(data) and data[self.position] not in _WHITESPACE and data[self.position] not in _DELIMITERS:
            self.position += 1
        return data[start:self.position]

    def parse(self, depth: int = 0) -> PdfObject:
        if depth > MAX_PDF_NESTING:
            raise _refuse("malformed_document")
        self.skip_space()
        if self.at_end():
            raise _refuse("malformed_document")
        data = self.data
        byte = data[self.position]
        if byte == 0x2F:  # '/'
            self.position += 1
            return Name(_decode_name(self._token()))
        if byte == 0x28:  # '('
            return self._literal()
        if data.startswith(b"<<", self.position):
            return self._dictionary(depth)
        if byte == 0x3C:  # '<'
            return self._hex()
        if byte == 0x5B:  # '['
            self.position += 1
            items: list[PdfObject] = []
            while True:
                self.skip_space()
                if self.at_end():
                    raise _refuse("malformed_document")
                if data[self.position] == 0x5D:
                    self.position += 1
                    return items
                items.append(self.parse(depth + 1))
                if len(items) > 100_000:
                    raise _refuse("malformed_document")
        if byte in b"+-.0123456789":
            return self._number_or_ref()
        if byte in b")>]}{":
            self.position += 1
            return Keyword(chr(byte))
        token = self._token()
        if not token:
            self.position += 1
            return Keyword(chr(byte))
        if token == b"true":
            return True
        if token == b"false":
            return False
        if token == b"null":
            return None
        return Keyword(token.decode("latin-1"))

    def _number_or_ref(self) -> PdfObject:
        token = self._token()
        number = _number(token)
        if isinstance(number, int) and number >= 0:
            saved = self.position
            self.skip_space()
            second = self._token()
            if second.isdigit():
                self.skip_space()
                if self.data.startswith(b"R", self.position) and (
                    self.position + 1 >= len(self.data)
                    or self.data[self.position + 1] in _WHITESPACE
                    or self.data[self.position + 1] in _DELIMITERS
                ):
                    self.position += 1
                    return Ref(number, int(second))
            self.position = saved
        return number

    def _dictionary(self, depth: int) -> dict[str, PdfObject]:
        self.position += 2
        result: dict[str, PdfObject] = {}
        while True:
            self.skip_space()
            if self.at_end():
                raise _refuse("malformed_document")
            if self.data.startswith(b">>", self.position):
                self.position += 2
                return result
            key = self.parse(depth + 1)
            if not isinstance(key, Name):
                raise _refuse("malformed_document")
            result[str(key)] = self.parse(depth + 1)
            if len(result) > 10_000:
                raise _refuse("malformed_document")

    def _literal(self) -> bytes:
        data = self.data
        self.position += 1
        out = bytearray()
        level = 1
        while self.position < len(data):
            byte = data[self.position]
            self.position += 1
            if byte == 0x5C:  # backslash
                if self.position >= len(data):
                    break
                escaped = data[self.position]
                self.position += 1
                simple = {0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0C, 0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
                if escaped in simple:
                    out.append(simple[escaped])
                elif 0x30 <= escaped <= 0x37:
                    digits = bytes([escaped])
                    while len(digits) < 3 and self.position < len(data) and 0x30 <= data[self.position] <= 0x37:
                        digits += bytes([data[self.position]])
                        self.position += 1
                    out.append(int(digits, 8) & 0xFF)
                elif escaped == 0x0D:
                    if self.position < len(data) and data[self.position] == 0x0A:
                        self.position += 1
                elif escaped == 0x0A:
                    pass
                else:
                    out.append(escaped)
                continue
            if byte == 0x28:
                level += 1
            elif byte == 0x29:
                level -= 1
                if level == 0:
                    return bytes(out)
            out.append(byte)
            if len(out) > MAX_PDF_STREAM_BYTES:
                raise _refuse("malformed_document")
        raise _refuse("malformed_document")

    def _hex(self) -> bytes:
        end = self.data.find(b">", self.position + 1)
        if end < 0:
            self.position = len(self.data)  # unterminated: the whole rest of the file was examined
            raise _refuse("malformed_document")
        digits = bytes(byte for byte in self.data[self.position + 1:end] if byte not in _WHITESPACE)
        self.position = end + 1
        if len(digits) % 2:
            digits += b"0"
        try:
            return bytes.fromhex(digits.decode("ascii"))
        except ValueError:
            raise _refuse("malformed_document") from None


def _decode_name(token: bytes) -> str:
    out = bytearray()
    index = 0
    while index < len(token):
        if token[index] == 0x23 and index + 3 <= len(token):
            try:
                out.append(int(token[index + 1:index + 3], 16))
                index += 3
                continue
            except ValueError:
                pass
        out.append(token[index])
        index += 1
    return out.decode("latin-1")


def _number(token: bytes) -> int | float:
    try:
        text = token.decode("ascii")
        if any(character in text for character in ".eE"):
            return float(text)
        return int(text)
    except (UnicodeDecodeError, ValueError):
        # A malformed number (`--3`, `1.2.3`) is common in the wild; treat it as zero, never fatal.
        return 0


# ---- the document ------------------------------------------------------------------------------


class _Document:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.objects: dict[int, PdfObject] = {}
        self.trailers: list[dict[str, PdfObject]] = []
        self.decoded_total = 0
        self.flags: dict[str, int] = {"active_content_ignored": 0, "streams_skipped": 0, "fonts_without_unicode": 0}
        self._scan()

    # -- scanning -------------------------------------------------------------------------------

    def _scan(self) -> None:
        data = self.data
        consumed = 0
        count = 0
        scanned = 0
        for match in _OBJECT_HEADER.finditer(data):
            if match.start() < consumed:
                continue  # inside a stream or object already parsed
            count += 1
            if count > MAX_PDF_OBJECTS:
                raise _refuse("malformed_document")
            parser = _Parser(data, match.end())
            try:
                value = parser.parse()
            except ExtractionRefusal:
                # An unterminated string or array runs to the end of the file. Rescanning from every later
                # header would be quadratic (S1 review finding 6), so one such object ends the scan.
                if parser.position >= len(data) - 1:
                    raise _refuse("malformed_document") from None
                continue
            finally:
                scanned += parser.position - match.end()
                if scanned > 4 * len(data) + 65_536:
                    raise _refuse("malformed_document")
            parser.skip_space()
            if isinstance(value, dict) and data.startswith(b"stream", parser.position):
                start = parser.position + len(b"stream")
                if data.startswith(b"\r\n", start):
                    start += 2
                elif data.startswith(b"\n", start) or data.startswith(b"\r", start):
                    start += 1
                end = self._stream_end(value, start)
                if end is None:
                    continue
                raw = data[start:end]
                value = Stream(dictionary=value, raw=raw)
                parser.position = end
            self.objects[int(match.group(1))] = value
            close = data.find(b"endobj", parser.position)
            consumed = close + len(b"endobj") if close >= 0 else parser.position
        for match in re.finditer(rb"trailer[ \t\r\n\x0c\x00]*<<", data):
            parser = _Parser(data, match.end() - 2)
            try:
                trailer = parser.parse()
            except ExtractionRefusal:
                continue
            if isinstance(trailer, dict):
                self.trailers.append(trailer)
        for value in list(self.objects.values()):
            if isinstance(value, Stream) and value.dictionary.get("Type") == "XRef":
                self.trailers.append(value.dictionary)
        self._expand_object_streams()

    def _stream_end(self, dictionary: dict[str, PdfObject], start: int) -> int | None:
        data = self.data
        length = dictionary.get("Length")
        if isinstance(length, int) and 0 <= length <= MAX_PDF_STREAM_BYTES:
            end = start + length
            tail = data[end:end + 32].lstrip(_WHITESPACE)
            if tail.startswith(b"endstream"):
                return end
        found = data.find(b"endstream", start, start + MAX_PDF_STREAM_BYTES + 64)
        if found < 0:
            return None
        end = found
        if data[end - 2:end] == b"\r\n":
            end -= 2
        elif end > start and data[end - 1:end] in (b"\n", b"\r"):
            end -= 1
        return end

    def _expand_object_streams(self) -> None:
        for value in list(self.objects.values()):
            if not isinstance(value, Stream) or value.dictionary.get("Type") != "ObjStm":
                continue
            decoded = self.decode(value)
            if decoded is None:
                continue
            count = value.dictionary.get("N")
            first = value.dictionary.get("First")
            if not isinstance(count, int) or not isinstance(first, int) or count < 0 or count > MAX_PDF_OBJECTS:
                continue
            header = _Parser(decoded, 0)
            pairs: list[tuple[int, int]] = []
            try:
                for _ in range(count):
                    number = header.parse()
                    offset = header.parse()
                    if isinstance(number, int) and isinstance(offset, int):
                        pairs.append((number, offset))
            except ExtractionRefusal:
                continue
            for number, offset in pairs:
                if number in self.objects:
                    continue  # a directly written object wins
                if len(self.objects) > MAX_PDF_OBJECTS:
                    raise _refuse("malformed_document")
                try:
                    self.objects[number] = _Parser(decoded, first + offset).parse()
                except ExtractionRefusal:
                    continue

    # -- access ---------------------------------------------------------------------------------

    def resolve(self, value: PdfObject) -> PdfObject:
        hops = 0
        while isinstance(value, Ref):
            hops += 1
            if hops > _MAX_REF_HOPS:
                return None
            value = self.objects.get(value.number)
        return value

    def dictionary(self, value: PdfObject) -> dict[str, PdfObject]:
        resolved = self.resolve(value)
        if isinstance(resolved, Stream):
            return resolved.dictionary
        return resolved if isinstance(resolved, dict) else {}

    def decode(self, stream: Stream) -> bytes | None:
        filters = self.resolve(stream.dictionary.get("Filter"))
        names = [filters] if isinstance(filters, Name) else [self.resolve(item) for item in filters] if isinstance(filters, list) else []
        parameters = self.resolve(stream.dictionary.get("DecodeParms"))
        if isinstance(parameters, dict) and isinstance(self.resolve(parameters.get("Predictor")), int):
            if int(self.resolve(parameters.get("Predictor")) or 1) > 1:  # type: ignore[arg-type]
                self.flags["streams_skipped"] += 1
                return None
        data = stream.raw
        for name in names:
            if name == "FlateDecode":
                data = self._inflate(data)
            elif name == "ASCIIHexDecode":
                digits = bytes(byte for byte in data.split(b">")[0] if byte not in _WHITESPACE)
                data = bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode("ascii", errors="ignore"))
            elif name == "ASCII85Decode":
                try:
                    body = data.strip()
                    body = body[:-2] if body.endswith(b"~>") else body
                    body = body[2:] if body.startswith(b"<~") else body
                    data = base64.a85decode(body, adobe=False, ignorechars=_WHITESPACE)
                except ValueError:
                    self.flags["streams_skipped"] += 1
                    return None
            else:
                self.flags["streams_skipped"] += 1
                return None
            if len(data) > MAX_PDF_STREAM_BYTES:
                raise _refuse("archive_bomb")
        self.decoded_total += len(data)
        if self.decoded_total > MAX_PDF_DECODED_BYTES:
            raise _refuse("archive_bomb")
        return data

    @staticmethod
    def _inflate(raw: bytes) -> bytes:
        inflater = zlib.decompressobj()
        try:
            out = inflater.decompress(raw, MAX_PDF_STREAM_BYTES + 1)
        except zlib.error:
            try:
                inflater = zlib.decompressobj(-15)
                out = inflater.decompress(raw[2:], MAX_PDF_STREAM_BYTES + 1)
            except zlib.error:
                return b""
        if len(out) > MAX_PDF_STREAM_BYTES or inflater.unconsumed_tail:
            raise _refuse("archive_bomb")
        return out

    # -- structure ------------------------------------------------------------------------------

    def encrypted(self) -> bool:
        return any("Encrypt" in trailer for trailer in self.trailers)

    def pages(self) -> list[dict[str, PdfObject]]:
        root: dict[str, PdfObject] = {}
        for trailer in reversed(self.trailers):
            candidate = self.dictionary(trailer.get("Root"))
            if candidate:
                root = candidate
                break
        found: list[dict[str, PdfObject]] = []
        seen: set[int] = set()

        def walk(node_value: PdfObject, depth: int) -> None:
            if depth > MAX_PDF_NESTING or len(found) > MAX_PAGES:
                return
            if isinstance(node_value, Ref):
                if node_value.number in seen:
                    return
                seen.add(node_value.number)
            node = self.dictionary(node_value)
            kind = node.get("Type")
            if kind == "Page" or ("Kids" not in node and "Contents" in node):
                found.append(node)
                return
            kids = self.resolve(node.get("Kids"))
            if isinstance(kids, list):
                for kid in kids[: MAX_PAGES * 4]:
                    walk(kid, depth + 1)

        if root:
            walk(root.get("Pages"), 0)
        if not found:
            numbered = sorted(
                (number, value) for number, value in self.objects.items() if isinstance(value, dict) and value.get("Type") == "Page"
            )
            found = [value for _, value in numbered if isinstance(value, dict)]
        return found

    def inherited(self, page: dict[str, PdfObject], key: str) -> PdfObject:
        node: dict[str, PdfObject] = page
        for _ in range(MAX_PDF_NESTING):
            if key in node:
                return node[key]
            parent = node.get("Parent")
            if parent is None:
                return None
            node = self.dictionary(parent)
        return None

    def count_active_content(self) -> int:
        count = 0
        for value in self.objects.values():
            dictionary = value.dictionary if isinstance(value, Stream) else value if isinstance(value, dict) else None
            if dictionary is None:
                continue
            if any(key in dictionary for key in _ACTIVE_KEYS) or dictionary.get("S") in ("JavaScript", "Launch", "URI"):
                count += 1
        return count


# ---- fonts -------------------------------------------------------------------------------------


@dataclass
class _Font:
    width: int = 1
    mapping: dict[bytes, str] = field(default_factory=dict)
    fallback: str | None = "cp1252"


def _parse_cmap(data: bytes) -> tuple[int, dict[bytes, str]]:
    mapping: dict[bytes, str] = {}
    width = 1
    for block in re.finditer(rb"begincodespacerange(.*?)endcodespacerange", data, re.S):
        for low in re.findall(rb"<([0-9A-Fa-f]+)>\s*<[0-9A-Fa-f]+>", block.group(1)):
            width = max(width, len(low) // 2)
    for block in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for source, target in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block.group(1)):
            if len(mapping) >= _MAX_CMAP_ENTRIES:
                break
            mapping[bytes.fromhex(source.decode())] = _utf16(target)
    for block in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        body = block.group(1)
        for low_hex, high_hex, rest in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]*>)", body):
            low = int(low_hex, 16)
            high = int(high_hex, 16)
            size = len(low_hex) // 2
            if high < low or high - low > _MAX_CMAP_ENTRIES or len(mapping) + (high - low) > _MAX_CMAP_ENTRIES:
                continue
            if rest.startswith(b"["):
                targets = re.findall(rb"<([0-9A-Fa-f]*)>", rest)
                for offset, target in enumerate(targets[: high - low + 1]):
                    mapping[(low + offset).to_bytes(size, "big")] = _utf16(target)
            else:
                base = _utf16(rest[1:-1])
                if not base:
                    continue
                for offset in range(high - low + 1):
                    mapping[(low + offset).to_bytes(size, "big")] = base[:-1] + chr(ord(base[-1]) + offset)
    return width, mapping


def _utf16(hex_digits: bytes) -> str:
    try:
        raw = bytes.fromhex(hex_digits.decode())
    except ValueError:
        return ""
    if len(raw) % 2:
        raw = b"\x00" + raw
    return raw.decode("utf-16-be", errors="replace")


# ---- content ------------------------------------------------------------------------------------


def _content_tokens(parser: _Parser) -> Iterator[PdfObject]:
    while True:
        parser.skip_space()
        if parser.at_end():
            return
        value = parser.parse()
        if isinstance(value, Keyword) and value == "BI":
            # Skip an inline image's binary payload: everything up to a whitespace-delimited EI.
            match = re.compile(rb"[ \t\r\n\x0c\x00]EI(?=[ \t\r\n\x0c\x00]|$)").search(parser.data, parser.position)
            parser.position = match.end() if match else len(parser.data)
            continue
        yield value


class _PageText:
    def __init__(self, document: _Document, fonts: dict[str, _Font], budget: list[int]) -> None:
        self.document = document
        self.fonts = fonts
        self.budget = budget
        self.parts: list[str] = []
        self.font = _Font()
        self.last_y: float | None = None

    def emit(self, text: str) -> None:
        if text:
            self.parts.append(text)

    def show(self, value: PdfObject) -> None:
        if isinstance(value, bytes):
            self.emit(_decode_with_font(value, self.font))

    def run(self, content: bytes) -> None:
        operands: list[PdfObject] = []
        for token in _content_tokens(_Parser(content)):
            if not isinstance(token, Keyword):
                operands.append(token)
                if len(operands) > 1_000:
                    operands = operands[-16:]
                continue
            self.budget[0] += 1
            if self.budget[0] > MAX_PDF_CONTENT_OPERATIONS:
                raise _refuse("malformed_document")
            self._operator(str(token), operands)
            operands = []

    def _operator(self, operator: str, operands: list[PdfObject]) -> None:
        if operator == "Tf" and len(operands) >= 2 and isinstance(operands[-2], Name):
            self.font = self.fonts.get(str(operands[-2]), _Font())
        elif operator == "Tj" and operands:
            self.show(operands[-1])
        elif operator in ("'", '"') and operands:
            self.emit("\n")
            self.show(operands[-1])
        elif operator == "TJ" and operands and isinstance(operands[-1], list):
            for item in operands[-1]:
                if isinstance(item, bytes):
                    self.show(item)
                elif isinstance(item, (int, float)) and item < -200:
                    self.emit(" ")
        elif operator in ("Td", "TD") and len(operands) >= 2:
            dy = operands[-1]
            if isinstance(dy, (int, float)) and abs(dy) > 0.1:
                self.emit("\n")
            else:
                self.emit(" ")
        elif operator == "T*":
            self.emit("\n")
        elif operator == "Tm" and len(operands) >= 6:
            y = operands[-1]
            if isinstance(y, (int, float)):
                if self.last_y is not None and abs(y - self.last_y) > 0.1:
                    self.emit("\n")
                else:
                    self.emit(" ")
                self.last_y = float(y)
        elif operator == "ET":
            self.emit("\n")
        elif operator == "Do":
            self.document.flags["xobjects_ignored"] = self.document.flags.get("xobjects_ignored", 0) + 1


def _decode_with_font(value: bytes, font: _Font) -> str:
    if font.mapping:
        out: list[str] = []
        width = font.width
        index = 0
        while index < len(value):
            code = value[index:index + width]
            mapped = font.mapping.get(code)
            if mapped is None and width > 1:
                mapped = font.mapping.get(value[index:index + 1])
                if mapped is not None:
                    index += 1
                    out.append(mapped)
                    continue
            out.append(mapped if mapped is not None else "")
            index += width
        return "".join(out)
    if font.fallback is None:
        return ""
    return value.decode(font.fallback, errors="replace")


def _fonts_for(document: _Document, page: dict[str, PdfObject]) -> dict[str, _Font]:
    resources = document.dictionary(document.inherited(page, "Resources"))
    fonts: dict[str, _Font] = {}
    for name, reference in document.dictionary(resources.get("Font")).items():
        font_dict = document.dictionary(reference)
        font = _Font()
        to_unicode = document.resolve(font_dict.get("ToUnicode"))
        if isinstance(to_unicode, Stream):
            decoded = document.decode(to_unicode)
            if decoded:
                font.width, font.mapping = _parse_cmap(decoded)
        if not font.mapping:
            subtype = font_dict.get("Subtype")
            encoding = document.resolve(font_dict.get("Encoding"))
            if subtype == "Type0":
                font.fallback = None
                document.flags["fonts_without_unicode"] += 1
            elif encoding == "MacRomanEncoding":
                font.fallback = "mac_roman"
        fonts[name] = font
    return fonts


def extract_pdf(data: bytes) -> PdfText:
    if not data.startswith(b"%PDF-"):
        raise _refuse("unsupported_format")
    document = _Document(data)
    if document.encrypted():
        raise _refuse("encrypted_pdf")
    pages = document.pages()
    if len(pages) > MAX_PAGES:
        raise _refuse("too_many_pages")
    if not pages:
        raise _refuse("malformed_document")
    budget = [0]
    texts: list[str] = []
    size = 0
    truncated = False
    for page in pages:
        page_text = _PageText(document, _fonts_for(document, page), budget)
        contents = document.resolve(page.get("Contents"))
        streams = contents if isinstance(contents, list) else [contents]
        joined = b""
        for item in streams[:64]:
            stream = document.resolve(item)
            if isinstance(stream, Stream):
                decoded = document.decode(stream)
                if decoded:
                    joined += decoded + b"\n"
        page_text.run(joined)
        text = clean("".join(page_text.parts))
        if size + len(text) > MAX_TEXT_CHARS:
            texts.append(text[: max(0, MAX_TEXT_CHARS - size)])
            truncated = True
            break
        texts.append(text)
        size += len(text)
    document.flags["active_content_ignored"] = document.count_active_content()
    joined_text = "\n\n".join(text for text in texts if text).strip()
    if not joined_text:
        raise _refuse("unsupported_pdf")
    return PdfText(text=joined_text, pages=len(pages), truncated=truncated, flags=document.flags)
