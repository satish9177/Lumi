"""The one refusal type extraction raises. `code` is stable and never carries document text."""

from typing import Final

EXTRACTION_CODES: Final = frozenset(
    {
        "unsupported_format",
        "type_mismatch",
        "file_too_large",
        "malformed_document",
        "unsupported_encoding",
        "too_many_pages",
        "encrypted_pdf",
        "unsupported_pdf",
        "archive_too_large",
        "archive_bomb",
        "xml_entities_refused",
        "macro_document_refused",
        "extraction_timeout",
        "extraction_failed",
    }
)


class ExtractionRefusal(ValueError):
    def __init__(self, code: str) -> None:
        if code not in EXTRACTION_CODES:  # pragma: no cover - a programming error, not an input.
            code = "extraction_failed"
        super().__init__(f"The document could not be read safely ({code}).")
        self.code = code
