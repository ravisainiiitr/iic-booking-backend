"""Validation and per-page text extraction for uploaded equipment manual PDFs."""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

from django.conf import settings

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
# Minimum extracted characters for a manual to count as text-bearing (scanned PDFs yield ~0).
MIN_TEXT_CHARS = 200
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class PdfRejected(ValueError):
    """The upload is not an acceptable manual PDF. `code` is stable for API responses."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PdfInfo:
    page_count: int


@dataclass
class PdfText:
    pages: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def char_count(self) -> int:
        return sum(len(p) for p in self.pages)


def max_bytes() -> int:
    return int(getattr(settings, "RESEARCH_COPILOT_MANUAL_MAX_BYTES", 50 * 1024**2) or 50 * 1024**2)


def max_pages() -> int:
    return int(getattr(settings, "RESEARCH_COPILOT_MANUAL_MAX_PAGES", 1500) or 1500)


def _reader(data: bytes):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        return PdfReader(io.BytesIO(data), strict=False)
    except (PdfReadError, ValueError, OSError) as exc:
        raise PdfRejected("PDF_UNREADABLE", "The file could not be read as a PDF.") from exc


def validate_pdf(data: bytes) -> PdfInfo:
    """Cheap checks run at upload time, before anything is stored."""
    if not data:
        raise PdfRejected("EMPTY_FILE", "The file is empty.")
    if len(data) > max_bytes():
        raise PdfRejected("FILE_TOO_LARGE", f"The file exceeds the {max_bytes() // (1024 * 1024)} MB limit.")
    if not data[:1024].lstrip().startswith(PDF_MAGIC):
        raise PdfRejected("NOT_A_PDF", "Only PDF files are accepted.")
    reader = _reader(data)
    if reader.is_encrypted:
        raise PdfRejected("PDF_ENCRYPTED", "Password-protected PDFs are not supported. Upload an unlocked copy.")
    try:
        pages = len(reader.pages)
    except Exception as exc:  # noqa: BLE001 - pypdf raises a variety of errors on malformed trees
        raise PdfRejected("PDF_UNREADABLE", "The PDF page structure could not be read.") from exc
    if pages <= 0:
        raise PdfRejected("PDF_NO_PAGES", "The PDF has no pages.")
    if pages > max_pages():
        raise PdfRejected("TOO_MANY_PAGES", f"The PDF has {pages} pages; the limit is {max_pages()}.")
    return PdfInfo(page_count=pages)


def _clean(text: str) -> str:
    text = (text or "").replace("\x00", "")
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def extract_pages(data: bytes) -> PdfText:
    """Text per page (index 0 = page 1). Pages that fail to extract become empty strings."""
    validate_pdf(data)
    reader = _reader(data)
    pages: list[str] = []
    for index, page in enumerate(reader.pages):
        try:
            pages.append(_clean(page.extract_text() or ""))
        except Exception:  # noqa: BLE001 - one broken page must not fail the whole manual
            logger.warning("copilot manual: text extraction failed on page %s", index + 1, exc_info=True)
            pages.append("")
    result = PdfText(pages=pages)
    if result.char_count < MIN_TEXT_CHARS:
        raise PdfRejected(
            "NO_EXTRACTABLE_TEXT",
            "No readable text was found. The PDF is probably scanned images; upload a text-based PDF or an OCR'd copy.",
        )
    return result
