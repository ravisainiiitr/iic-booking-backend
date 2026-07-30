"""Result validation helpers for Milestone 10 import."""

from __future__ import annotations

from iic_booking.sync.exceptions import SyncControlPlaneError


class ResultValidationError(SyncControlPlaneError):
    code = "RESULT_VALIDATION_FAILED"
    status_code = 400
    default_message = "Result validation failed."


# Parsed for measurements when present; all other extensions are treated as file attachments.
MEASUREMENT_EXTENSIONS = {".csv", ".json", ".xml", ".txt"}
ATTACHMENT_ONLY_EXTENSIONS = {".pdf", ".zip"}

# Backward-compatible alias used by older callers/tests.
SUPPORTED_EXTENSIONS = MEASUREMENT_EXTENSIONS | ATTACHMENT_ONLY_EXTENSIONS


def normalize_extension(file_name: str) -> str:
    name = (file_name or "").strip()
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def is_attachment_only_extension(ext: str) -> bool:
    """True when the file should be stored as an attachment (not measurement-parsed)."""
    if not ext:
        return True
    return ext not in MEASUREMENT_EXTENSIONS


def validate_import_payload(data: dict) -> None:
    if not data.get("agent_upload_id"):
        raise ResultValidationError("agent_upload_id is required.")
    if not data.get("booking_id"):
        raise ResultValidationError("booking_id is required.")
    if not data.get("equipment_id"):
        raise ResultValidationError("equipment_id is required.")
    file_name = data.get("file_name") or ""
    ext = normalize_extension(file_name)
    # Allow any extension — instrument PCs produce many proprietary formats (.cdr, .raw, …).
    # Unknown types are imported as opaque attachments.
    measurements = data.get("measurements") or []
    if ext in MEASUREMENT_EXTENSIONS and not isinstance(measurements, list):
        raise ResultValidationError("measurements must be a list.")
