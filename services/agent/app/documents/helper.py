"""The extraction helper process: bytes in on stdin, one bounded JSON line out on stdout.

Run as `python -E -s -S -m app.documents.helper --format pdf|docx|txt`. It is given no path and opens
none; it reads at most `MAX_FILE_BYTES + 1` bytes from stdin, extracts text with the stdlib-only
extractors, and exits. The runtime launches it in a Job Object that caps its memory, forbids it from
starting any other process, and kills it at the time limit, with an environment that carries no
credential of any kind.
"""

import argparse
import json
import sys

from app.documents.docx import extract_docx
from app.documents.errors import ExtractionRefusal
from app.documents.limits import MAX_FILE_BYTES, MAX_TEXT_CHARS, SUPPORTED_FORMATS
from app.documents.pdf import extract_pdf
from app.documents.sniff import check_claim
from app.documents.text import extract_text


def extract(data: bytes, declared: str) -> dict[str, object]:
    """Pure: the helper's whole job, importable for unit tests."""
    if len(data) > MAX_FILE_BYTES:
        raise ExtractionRefusal("file_too_large")
    kind = check_claim(data, None)
    if kind != declared:
        raise ExtractionRefusal("type_mismatch")
    pages: int | None = None
    flags: dict[str, int] = {}
    truncated = False
    if kind == "pdf":
        pdf = extract_pdf(data)
        text, pages, truncated, flags = pdf.text, pdf.pages, pdf.truncated, pdf.flags
    elif kind == "docx":
        docx = extract_docx(data)
        text, truncated, flags = docx.text, docx.truncated, docx.flags
    else:
        text = extract_text(data)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        truncated = True
    return {"ok": True, "format": kind, "text": text, "pages": pages, "truncated": truncated, "flags": flags}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.documents.helper", add_help=False)
    parser.add_argument("--format", choices=SUPPORTED_FORMATS, required=True)
    options = parser.parse_args(argv)
    data = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
    try:
        result = extract(data, options.format)
    except ExtractionRefusal as refusal:
        result = {"ok": False, "code": refusal.code}
    except (RecursionError, MemoryError):
        result = {"ok": False, "code": "malformed_document"}
    except Exception:  # noqa: BLE001 - any parser surprise is a refusal, never a traceback with text in it.
        result = {"ok": False, "code": "extraction_failed"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess.
    sys.setrecursionlimit(200)
    raise SystemExit(main())
