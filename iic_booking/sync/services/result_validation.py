"""Result validation helpers for Milestone 10 import."""

from __future__ import annotations

from iic_booking.sync.exceptions import SyncControlPlaneError


class ResultValidationError(SyncControlPlaneError):
    code = "RESULT_VALIDATION_FAILED"
    status_code = 400
    default_message = "Result validation failed."


SUPPORTED_EXTENSIONS = {".csv", ".json", ".xml", ".txt", ".pdf", ".zip"}
MEASUREMENT_EXTENSIONS = {".csv", ".json", ".xml", ".txt"}
ATTACHMENT_ONLY_EXTENSIONS = {".pdf", ".zip"}


def normalize_extension(file_name: str) -> str:
    name = (file_name or "").strip()
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def validate_import_payload(data: dict) -> None:
    if not data.get("agent_upload_id"):
        raise ResultValidationError("agent_upload_id is required.")
    if not data.get("booking_id"):
        raise ResultValidationError("booking_id is required.")
    if not data.get("equipment_id"):
        raise ResultValidationError("equipment_id is required.")
    file_name = data.get("file_name") or ""
    ext = normalize_extension(file_name)
    if ext and ext not in SUPPORTED_EXTENSIONS:
        raise ResultValidationError(f"Unsupported extension: {ext}")
    measurements = data.get("measurements") or []
    if ext in MEASUREMENT_EXTENSIONS and not isinstance(measurements, list):
        raise ResultValidationError("measurements must be a list.")
