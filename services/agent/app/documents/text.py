"""Plain text: UTF-8 (optionally with a BOM) or BOM-marked UTF-16. Nothing is guessed."""

import codecs
import unicodedata

from app.documents.errors import ExtractionRefusal

_KEEP_CONTROLS = {"\n", "\t"}


def decode_text(data: bytes) -> str:
    if data.startswith(codecs.BOM_UTF8):
        encoding, body = "utf-8", data[len(codecs.BOM_UTF8):]
    elif data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        encoding, body = "utf-16", data
    else:
        encoding, body = "utf-8", data
    try:
        text = body.decode(encoding)
    except UnicodeDecodeError:
        raise ExtractionRefusal("unsupported_encoding") from None
    if "\x00" in text:
        raise ExtractionRefusal("unsupported_encoding")
    return text


def clean(text: str) -> str:
    """One document's text, normalised: NFC, CRLF folded, control and format characters dropped."""
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    kept = []
    for character in text:
        category = unicodedata.category(character)
        if character in _KEEP_CONTROLS or category not in ("Cc", "Cf", "Cs", "Co", "Cn"):
            kept.append(character)
    lines = [" ".join(line.split()) for line in "".join(kept).split("\n")]
    # Collapse runs of blank lines to one.
    collapsed: list[str] = []
    for line in lines:
        if line == "" and collapsed and collapsed[-1] == "":
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip()


def extract_text(data: bytes) -> str:
    return clean(decode_text(data))
