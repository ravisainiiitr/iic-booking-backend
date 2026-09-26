"""
Upload validation and safe-serving policy for research files.

Filenames are never trusted as paths. The real type is sniffed from the first bytes stored in S3
(not from the extension or the browser-declared type), and only a small allowlist of verified
formats is ever served inline; everything else downloads as an attachment with a generic type.
This is type validation, not malware scanning (no scanner exists in this deployment).
"""

from __future__ import annotations

import os
import re
import unicodedata

from django.conf import settings

MAX_NAME_LENGTH = 200
SNIFF_BYTES = 8192

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_CONTROL_CHARS_EXCEPT_WHITESPACE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

TEXT_EXTENSIONS = {".txt", ".log", ".md", ".dat", ".xy", ".asc", ".json", ".xml", ".ini", ".cfg"}
CSV_EXTENSIONS = {".csv", ".tsv"}
INLINE_IMAGE_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
INLINE_CONTENT_TYPES = {"pdf": "application/pdf", **INLINE_IMAGE_TYPES}


class InvalidFilename(ValueError):
    pass


def clean_display_name(raw: str) -> str:
    """Human-facing name: last path component only, no control characters, bounded length."""
    name = unicodedata.normalize("NFC", str(raw or ""))
    name = name.replace("\\", "/").split("/")[-1]
    name = _CONTROL_CHARS.sub("", name).strip().strip(".").strip()
    if not name:
        raise InvalidFilename("A file name is required.")
    if len(name) > MAX_NAME_LENGTH:
        stem, ext = os.path.splitext(name)
        ext = ext[:20]
        name = stem[: MAX_NAME_LENGTH - len(ext)] + ext
    return name


def strip_control_chars(raw, *, multiline: bool = False) -> str:
    """PostgreSQL text columns reject NUL bytes, so free-text input must never carry control characters."""
    pattern = _CONTROL_CHARS_EXCEPT_WHITESPACE if multiline else _CONTROL_CHARS
    return pattern.sub("", str(raw or ""))


def clean_folder_name(raw: str) -> str:
    name = _CONTROL_CHARS.sub("", unicodedata.normalize("NFC", str(raw or ""))).strip()
    if not name or name in {".", ".."}:
        raise InvalidFilename("A folder name is required.")
    if "/" in name or "\\" in name:
        raise InvalidFilename("Folder names cannot contain / or \\.")
    if len(name) > 120:
        raise InvalidFilename("Folder names can be at most 120 characters.")
    return name


def storage_safe_name(display_name: str) -> str:
    """ASCII-only object key component derived from the display name."""
    ascii_name = unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode("ascii")
    safe = _KEY_UNSAFE.sub("_", ascii_name).strip("._") or "file"
    return safe[:120]


def extension_of(name: str) -> str:
    return os.path.splitext(name or "")[1].lower()


def blocked_extensions() -> set[str]:
    raw = getattr(settings, "MY_RESEARCH_BLOCKED_EXTENSIONS", "") or ""
    return {e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}" for e in raw.split(",") if e.strip()}


def is_blocked_name(name: str) -> bool:
    lowered = (name or "").lower()
    return any(lowered.endswith(ext) for ext in blocked_extensions())


def sniff_type(head: bytes) -> str:
    """Identify common formats from magic bytes. Returns a short type code."""
    if not head:
        return "empty"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return "tiff"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"
    if head.startswith(b"MZ"):
        return "executable"
    if head.startswith(b"\x7fELF"):
        return "executable"
    if head[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"):
        return "executable"
    if head.startswith(b"\x89HDF\r\n\x1a\n"):
        return "hdf5"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if _looks_like_text(head):
        return "text"
    return "binary"


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        # A multi-byte character cut at the sniff boundary is still text.
        if exc.start >= len(head) - 4:
            return True
    printable = sum(1 for b in head if b in (9, 10, 13) or 32 <= b < 127 or b >= 160)
    return printable / max(len(head), 1) > 0.95


def rejection_reason(detected_type: str) -> str | None:
    if detected_type == "executable":
        return "Executable programs cannot be stored in My Research."
    return None


def preview_kind(detected_type: str, display_name: str) -> str:
    """How the UI may preview a file: pdf, image, text, csv or none."""
    if detected_type == "pdf":
        return "pdf"
    if detected_type in INLINE_IMAGE_TYPES:
        return "image"
    if detected_type in {"text", "empty"}:
        ext = extension_of(display_name)
        if ext in CSV_EXTENSIONS:
            return "csv"
        if ext in TEXT_EXTENSIONS:
            return "text"
    return "none"


def serving_content_type(detected_type: str, disposition: str) -> tuple[str, str]:
    """(disposition, content type) actually used for a presigned GET. Unknown data never renders inline."""
    if disposition == "inline" and detected_type in INLINE_CONTENT_TYPES:
        return "inline", INLINE_CONTENT_TYPES[detected_type]
    return "attachment", "application/octet-stream"
