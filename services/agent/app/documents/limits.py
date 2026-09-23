"""Closed extraction limits (Milestone 10 S1). Every one is enforced, and exceeding one refuses cleanly."""

from typing import Final

#: Largest file the runtime will read and hand to the helper.
MAX_FILE_BYTES: Final = 10 * 1024 * 1024
#: Largest PDF page count accepted at all (more is refused, not silently cut).
MAX_PAGES: Final = 50
#: Extracted text kept per document, in characters. More is truncated and says so.
MAX_TEXT_CHARS: Final = 120_000
#: Wall-clock limit for one helper run; the helper is killed at this point.
HELPER_TIMEOUT_SECONDS: Final = 20.0
#: Memory the helper process may commit (a Job Object limit on Windows).
HELPER_MEMORY_BYTES: Final = 512 * 1024 * 1024
#: Largest helper stdout accepted by the runtime.
MAX_HELPER_OUTPUT_BYTES: Final = 2 * 1024 * 1024

# DOCX (a ZIP archive of XML parts)
MAX_ZIP_MEMBERS: Final = 400
MAX_ZIP_UNCOMPRESSED_BYTES: Final = 40 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES: Final = 20 * 1024 * 1024
MAX_COMPRESSION_RATIO: Final = 100

# PDF
MAX_PDF_OBJECTS: Final = 20_000
MAX_PDF_DECODED_BYTES: Final = 32 * 1024 * 1024
MAX_PDF_STREAM_BYTES: Final = 8 * 1024 * 1024
MAX_PDF_NESTING: Final = 32
MAX_PDF_CONTENT_OPERATIONS: Final = 400_000

SUPPORTED_FORMATS: Final = ("pdf", "docx", "txt")
