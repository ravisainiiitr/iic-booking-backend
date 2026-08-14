"""Stage booking RAW/results files into an analysis workspace RawData folder."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile

from iic_booking.equipment.booking_results_service import (
    merge_booking_result_files,
    resolve_dsa_attachment_path,
)
from iic_booking.equipment.models import Booking, BookingResultFile
from iic_booking.sync.models import ResultAttachment
from iic_booking.sync.services.results_s3 import download_results_s3_bytes

logger = logging.getLogger(__name__)


class BookingRawStagingService:
    """Copy booking results (S3 / DSA / operator uploads) into workspace RawData."""

    def list_raw_entries(self, booking: Booking, *, request=None) -> list[dict[str, Any]]:
        s3_files: list[dict[str, Any]] = []
        try:
            from iic_booking.equipment.api_views import _list_booking_result_files_from_s3

            virtual = (booking.virtual_booking_id or f"booking-{booking.pk}").strip()
            ok, listed = _list_booking_result_files_from_s3(virtual)
            if ok and listed:
                s3_files = listed
        except Exception as exc:  # noqa: BLE001
            logger.debug("S3 results list failed for booking %s: %s", booking.pk, exc)
            s3_files = []
        return merge_booking_result_files(booking=booking, s3_files=s3_files, request=request)

    def has_raw_files(self, booking: Booking, *, request=None) -> bool:
        return bool(self.list_raw_entries(booking, request=request))

    def stage_into_workspace(
        self,
        booking: Booking,
        workspace,
        *,
        actor=None,
        request=None,
        source_booking: Booking | None = None,
        allow_names: list[str] | None = None,
        folder_prefix: str = "",
    ) -> dict[str, Any]:
        from iic_booking.remote_analysis.workspace.transfer import TransferError, TransferManager
        from iic_booking.remote_analysis.workspace_models import WorkspaceFile

        source = source_booking or booking
        entries = self.list_raw_entries(source, request=request)
        allowed = {
            str(n).replace("\\", "/").lstrip("/").lower()
            for n in (allow_names or [])
            if str(n).strip()
        }
        prefix = (folder_prefix or "").replace("\\", "/").strip("/")
        if allowed:
            entries = [
                e
                for e in entries
                if str(e.get("name") or "").replace("\\", "/").lstrip("/").lower() in allowed
            ]
        elif prefix:
            filtered = []
            for e in entries:
                name = str(e.get("name") or "").replace("\\", "/").lstrip("/")
                if name.startswith(prefix + "/") or name.rsplit("/", 1)[0] == prefix:
                    filtered.append(e)
            entries = filtered
        staged = 0
        skipped = 0
        errors: list[str] = []
        mgr = TransferManager()

        for entry in entries:
            name = (entry.get("name") or "result.bin").replace("\\", "/").lstrip("/")
            if ".." in name.split("/"):
                errors.append(f"Rejected path: {name}")
                continue
            try:
                data, sha256 = self._load_bytes(booking, entry)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {exc}")
                continue
            if data is None:
                errors.append(f"{name}: unavailable")
                continue

            existing = (
                WorkspaceFile.objects.filter(
                    workspace=workspace,
                    relative_path=f"RawData/{name}",
                    deleted=False,
                    is_current=True,
                )
                .first()
            )
            if existing and existing.sha256 and existing.sha256.lower() == sha256.lower():
                if (existing.source or "").strip().lower() in {"", "portal"}:
                    existing.source = "booking_raw"
                    existing.save(update_fields=["source", "modified_at"])
                skipped += 1
                continue

            uploaded = SimpleUploadedFile(name.split("/")[-1], data)
            try:
                mgr.upload(
                    workspace,
                    uploaded,
                    folder="RawData",
                    actor=actor,
                    expected_sha256=sha256,
                    source="booking_raw",
                    relative_name=name,
                    override_quota=True,
                )
                staged += 1
            except TransferError as exc:
                errors.append(f"{name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.exception("RAW staging failed for %s", name)
                errors.append(f"{name}: {exc}")

        return {
            "staged": staged,
            "skipped": skipped,
            "errors": errors,
            "total_source_files": len(entries),
            "success": len(errors) == 0 or staged > 0 or skipped > 0,
        }

    def _load_bytes(self, booking: Booking, entry: dict[str, Any]) -> tuple[bytes | None, str]:
        source = str(entry.get("source") or "")
        s3_key = (entry.get("s3_key") or entry.get("key") or "").strip()

        if source in {"s3", "dsa"} and s3_key and not s3_key.startswith("dsa:") and not s3_key.startswith("booking_result:"):
            data = download_results_s3_bytes(s3_key)
            if data is not None:
                return data, hashlib.sha256(data).hexdigest()

        attachment_id = entry.get("attachment_id")
        if attachment_id:
            att = ResultAttachment.objects.filter(id=attachment_id, result__booking_id=booking.pk).first()
            if att:
                s3 = (att.s3_key or "").strip()
                if s3:
                    data = download_results_s3_bytes(s3)
                    if data is not None:
                        return data, hashlib.sha256(data).hexdigest()
                path = resolve_dsa_attachment_path(att)
                if path is not None:
                    data = path.read_bytes()
                    return data, hashlib.sha256(data).hexdigest()

        file_id = entry.get("file_id")
        if file_id:
            brf = BookingResultFile.objects.filter(pk=file_id, booking_id=booking.pk).first()
            if brf and brf.file:
                with brf.file.open("rb") as fh:
                    data = fh.read()
                return data, hashlib.sha256(data).hexdigest()

        # Fallback: try key as S3
        if s3_key and not s3_key.startswith("dsa:") and not s3_key.startswith("booking_result:"):
            data = download_results_s3_bytes(s3_key)
            if data is not None:
                return data, hashlib.sha256(data).hexdigest()

        return None, ""
