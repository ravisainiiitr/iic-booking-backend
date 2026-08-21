"""Unified booking results: S3 Results/ + DSA ResultAttachment + BookingResultFile."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from django.db.models import Exists, OuterRef
from django.http import HttpRequest

from iic_booking.equipment.models import Booking, BookingResultFile
from iic_booking.sync.models import ResultAttachment
from iic_booking.sync.services.results_s3 import download_results_s3_bytes, presign_results_s3_get
from iic_booking.sync.services.upload import _storage_root

logger = logging.getLogger(__name__)

CONTROL_RESULT_FILE_NAMES = {
    "workspace-ready",
    "workspace.ready",
    ".workspace-ready",
    ".workspace_ready",
    "workspace_ready",
    "thumbs.db",
    "desktop.ini",
    ".ds_store",
}

CONTROL_RESULT_EXTENSIONS = {
    ".tmp",
    ".temp",
    ".partial",
    ".part",
    ".crdownload",
    ".download",
    ".lock",
    ".swp",
    ".swo",
}


def booking_has_results_annotation():
    """ORM annotation: operator complete files OR DSA-imported attachments."""
    return Exists(BookingResultFile.objects.filter(booking_id=OuterRef("pk"))) | Exists(
        ResultAttachment.objects.filter(result__booking_id=OuterRef("pk"))
    )


def resolve_dsa_attachment_path(attachment: ResultAttachment) -> Path | None:
    """Resolve ResultAttachment.storage_path to an absolute readable file under storage root."""
    raw = (attachment.storage_path or "").strip()
    if not raw:
        session = attachment.upload_session
        if session and session.server_path:
            raw = session.server_path.strip()
    if not raw:
        return None

    root = _storage_root().resolve()
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    if not path.is_file():
        return None
    return path


def list_dsa_result_files(booking: Booking, request: HttpRequest | None = None) -> list[dict[str, Any]]:
    """Files imported by Department Sync Agent for this booking (S3 preferred, local fallback)."""
    attachments = (
        ResultAttachment.objects.filter(result__booking_id=booking.pk)
        .select_related("result", "upload_session")
        .order_by("created_at")
    )
    files: list[dict[str, Any]] = []
    for att in attachments:
        uploaded_by = (att.result.processed_by or "").strip() or "Department Sync Agent"
        s3_key = (att.s3_key or "").strip()
        path = resolve_dsa_attachment_path(att)

        if s3_key:
            download_url = presign_results_s3_get(s3_key) or ""
            if not download_url:
                # Fall back to authenticated portal download endpoint
                download_path = f"/api/bookings/{booking.pk}/results/attachments/{att.id}/"
                download_url = download_path
                if request is not None:
                    try:
                        download_url = request.build_absolute_uri(download_path)
                    except Exception:  # noqa: BLE001
                        download_url = download_path
            files.append(
                {
                    "key": s3_key,
                    "name": att.file_name or s3_key.split("/")[-1],
                    "download_url": download_url,
                    "source": "dsa",
                    "attachment_id": str(att.id),
                    "uploaded_at": att.created_at.isoformat() if att.created_at else None,
                    "uploaded_by": uploaded_by,
                    "size_bytes": int(att.size_bytes or 0),
                    "content_type": att.content_type or "",
                    "s3_key": s3_key,
                }
            )
            continue

        if path is None:
            logger.warning(
                "DSA result attachment missing on disk and S3 | booking=%s attachment=%s path=%s",
                booking.pk,
                att.id,
                att.storage_path,
            )
            continue

        download_path = f"/api/bookings/{booking.pk}/results/attachments/{att.id}/"
        download_url = download_path
        if request is not None:
            try:
                download_url = request.build_absolute_uri(download_path)
            except Exception:  # noqa: BLE001
                download_url = download_path
        files.append(
            {
                "key": f"dsa:{att.id}",
                "name": att.file_name or path.name,
                "download_url": download_url,
                "source": "dsa",
                "attachment_id": str(att.id),
                "uploaded_at": att.created_at.isoformat() if att.created_at else None,
                "uploaded_by": uploaded_by,
                "size_bytes": int(att.size_bytes or path.stat().st_size),
                "content_type": att.content_type or "",
            }
        )
    return files


def list_booking_result_file_entries(
    booking: Booking, request: HttpRequest | None = None
) -> list[dict[str, Any]]:
    """Operator Complete multipart uploads (BookingResultFile)."""
    files: list[dict[str, Any]] = []
    for brf in BookingResultFile.objects.filter(booking_id=booking.pk).order_by("created_at"):
        if not brf.file:
            continue
        name = (brf.original_name or Path(brf.file.name).name).strip() or Path(brf.file.name).name
        download_path = f"/api/bookings/{booking.pk}/results/files/{brf.pk}/"
        download_url = download_path
        if request is not None:
            try:
                download_url = request.build_absolute_uri(download_path)
            except Exception:  # noqa: BLE001
                download_url = download_path
        try:
            size_bytes = brf.file.size
        except Exception:
            size_bytes = 0
        files.append(
            {
                "key": f"booking_result:{brf.pk}",
                "name": name,
                "download_url": download_url,
                "source": "booking_result",
                "file_id": brf.pk,
                "uploaded_at": brf.created_at.isoformat() if brf.created_at else None,
                "uploaded_by": "Lab operator",
                "size_bytes": int(size_bytes or 0),
                "content_type": "",
            }
        )
    return files


def merge_booking_result_files(
    *,
    booking: Booking,
    s3_files: list[dict[str, Any]] | None,
    request: HttpRequest | None = None,
) -> list[dict[str, Any]]:
    """Union of S3 + DSA + operator complete files (dedupe by name+size when possible)."""
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    seen_keys: set[str] = set()

    def _add(entry: dict[str, Any]) -> None:
        key = str(entry.get("key") or "")
        if key and key in seen_keys:
            return
        name = str(entry.get("name") or "")
        size = int(entry.get("size_bytes") or 0)
        fingerprint = (name.lower(), size)
        if size > 0 and fingerprint in seen:
            return
        if key:
            seen_keys.add(key)
        if size > 0:
            seen.add(fingerprint)
        merged.append(entry)

    for f in s3_files or []:
        enriched = dict(f)
        enriched.setdefault("source", "s3")
        enriched.setdefault("uploaded_by", "Lab results folder")
        enriched.setdefault("uploaded_at", None)
        enriched.setdefault("size_bytes", 0)
        _add(enriched)

    for f in list_dsa_result_files(booking, request):
        _add(f)

    for f in list_booking_result_file_entries(booking, request):
        _add(f)

    return merged


def has_material_result_files(booking: Booking) -> bool:
    """
    True when booking has at least one usable user/equipment data file.

    Ignores known control/marker files (e.g. workspace-ready) and missing/empty entries.
    Uses the existing unified result-source merge (S3 + DSA + operator uploads).
    """
    s3_files: list[dict[str, Any]] = []
    try:
        from iic_booking.equipment.api_views import _list_booking_result_files_from_s3

        virtual = (booking.virtual_booking_id or f"booking-{booking.pk}").strip()
        ok, listed = _list_booking_result_files_from_s3(virtual)
        if ok and listed:
            s3_files = listed
    except Exception:  # noqa: BLE001
        s3_files = []
    merged = merge_booking_result_files(booking=booking, s3_files=s3_files, request=None)
    for entry in merged:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        leaf = Path(name).name.strip().lower()
        if not leaf or leaf.startswith("."):
            continue
        if leaf in CONTROL_RESULT_FILE_NAMES:
            continue
        if Path(leaf).suffix in CONTROL_RESULT_EXTENSIONS:
            continue
        size = int(entry.get("size_bytes") or 0)
        if size <= 0:
            continue
        return True
    return False


def iter_dsa_zip_members(booking: Booking):
    """
    Yield zip members for DSA attachments.
    Yields either (arcname, Path) for local files or (arcname, bytes) for S3-backed files.
    """
    virtual = (booking.virtual_booking_id or f"booking-{booking.pk}").strip()
    for att in (
        ResultAttachment.objects.filter(result__booking_id=booking.pk)
        .select_related("upload_session")
        .order_by("created_at")
    ):
        name = att.file_name or "result.bin"
        arcname = f"{virtual}/dsa/{name}"
        s3_key = (att.s3_key or "").strip()
        if s3_key:
            data = download_results_s3_bytes(s3_key)
            if data is not None:
                yield arcname, data
                continue
        path = resolve_dsa_attachment_path(att)
        if path is None:
            continue
        yield arcname, path


def iter_booking_result_zip_members(booking: Booking):
    """Yield (arcname, file handle opener) for BookingResultFile."""
    virtual = (booking.virtual_booking_id or f"booking-{booking.pk}").strip()
    for brf in BookingResultFile.objects.filter(booking_id=booking.pk).order_by("created_at"):
        if not brf.file:
            continue
        name = (brf.original_name or Path(brf.file.name).name).strip() or Path(brf.file.name).name
        yield f"{virtual}/uploaded/{name}", brf.file
