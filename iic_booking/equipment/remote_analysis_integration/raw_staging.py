"""Stage booking RAW/results files into an analysis workspace RawData folder."""

from __future__ import annotations

import hashlib
import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from django.core.cache import cache
from django.core.files import File

from iic_booking.equipment.booking_results_service import (
    CONTROL_RESULT_FILE_NAMES,
    booking_result_attachments_qs,
    booking_result_files_qs,
    merge_booking_result_files,
    resolve_dsa_attachment_path,
)
from iic_booking.equipment.models import Booking
from iic_booking.sync.services.results_s3 import list_results_s3_objects, open_results_s3_stream, uploaded_before

logger = logging.getLogger(__name__)

RESULTS_LISTING_CACHE_SECONDS = 120
SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


def _listing_cache_key(virtual: str, prefix_only: bool = False) -> str:
    digest = hashlib.sha1(virtual.encode("utf-8")).hexdigest()
    return f"ra:results-s3:v2{':p' if prefix_only else ''}:{digest}"


def cached_results_s3_objects(
    virtual: str, *, fresh: bool = False, prefix_only: bool = False, not_before=None
) -> list[dict[str, Any]]:
    """S3 results for a virtual booking id, cached briefly so status polls never page the bucket.

    ``not_before`` (the booking's created_at) drops objects uploaded before the booking existed;
    display IDs are reused after a booking wipe.
    """
    key = _listing_cache_key(virtual, prefix_only)
    entries = None
    if not fresh:
        hit = cache.get(key)
        if hit is None and prefix_only:
            hit = cache.get(_listing_cache_key(virtual))
        entries = hit
    if entries is None:
        try:
            entries = (
                list_results_s3_objects(virtual, prefix_only=True) if prefix_only else list_results_s3_objects(virtual)
            )
        except Exception:  # noqa: BLE001
            return []
        cache.set(key, entries, RESULTS_LISTING_CACHE_SECONDS)
    if not_before is None:
        return list(entries)
    return [e for e in entries if not uploaded_before(e.get("last_modified"), not_before)]


def is_material_entry(entry: dict[str, Any]) -> bool:
    """Real instrument/user data (not a control marker or empty stub)."""
    name = str(entry.get("name") or "").strip()
    if not name:
        return False
    leaf = Path(name).name.strip().lower()
    if leaf in CONTROL_RESULT_FILE_NAMES:
        return False
    size = int(entry.get("size_bytes") or 0)
    # S3 listings may omit size; treat named non-control objects as material.
    if size > 0 or (entry.get("key") or entry.get("download_url")):
        return not (size == 0 and leaf.startswith("."))
    return False


def spool_stream(stream, *, close: bool = True) -> tuple[Any, str, int]:
    """Copy a readable stream into a temp file (memory-bounded) while hashing it."""
    spooled = tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES)
    hasher = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            spooled.write(chunk)
            hasher.update(chunk)
            size += len(chunk)
    except Exception:
        spooled.close()
        raise
    finally:
        if close:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
    spooled.seek(0)
    return spooled, hasher.hexdigest(), size


def sha256_path(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


class BookingRawStagingService:
    """Copy booking results (S3 / DSA / operator uploads) into workspace RawData."""

    @staticmethod
    def _virtual(booking: Booking) -> str:
        return (booking.virtual_booking_id or f"booking-{booking.pk}").strip()

    def list_raw_entries(self, booking: Booking, *, request=None, fresh: bool = False) -> list[dict[str, Any]]:
        s3_files = cached_results_s3_objects(self._virtual(booking), fresh=fresh, not_before=booking.created_at)
        return merge_booking_result_files(booking=booking, s3_files=s3_files, request=request)

    def has_raw_files(self, booking: Booking, *, request=None) -> bool:
        """True when real instrument/user data files exist (ignore control markers / empty stubs).

        Database-backed results are checked first; S3 comes from the short-lived listing cache.
        """
        for brf in booking_result_files_qs(booking).only("file", "original_name"):
            if brf.file and is_material_entry(
                {"name": brf.original_name or Path(brf.file.name).name, "key": f"booking_result:{brf.pk}"}
            ):
                return True
        for att in booking_result_attachments_qs(booking).select_related("upload_session"):
            entry = {"name": att.file_name, "size_bytes": int(att.size_bytes or 0)}
            if (att.s3_key or "").strip():
                entry["key"] = att.s3_key
            elif resolve_dsa_attachment_path(att) is not None:
                entry["key"] = f"dsa:{att.id}"
            else:
                continue
            if is_material_entry(entry):
                return True
        return any(
            is_material_entry(e)
            for e in cached_results_s3_objects(self._virtual(booking), not_before=booking.created_at)
        )

    def stage_into_workspace(
        self,
        booking: Booking,
        workspace,
        *,
        actor=None,
        request=None,
        entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Stage booking results into RawData.

        ``entries`` accepts a caller-filtered subset of ``list_raw_entries`` output
        (R12 data browser selection); when omitted every result file is staged.
        """
        from iic_booking.remote_analysis.workspace.transfer import TransferError, TransferManager
        from iic_booking.remote_analysis.workspace_models import WorkspaceFile

        if entries is None:
            entries = self.list_raw_entries(booking, request=request, fresh=True)
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
                with self.open_entry(booking, entry) as opened:
                    if opened is None:
                        errors.append(f"{name}: unavailable")
                        continue
                    fileobj, sha256, _size = opened
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
                    mgr.upload(
                        workspace,
                        File(fileobj, name=name.split("/")[-1]),
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

    @contextmanager
    def open_entry(self, booking: Booking, entry: dict[str, Any]) -> Iterator[tuple[Any, str, int] | None]:
        """Yield (readable file positioned at 0, sha256, size) for a merged result entry, or None.

        Never loads a whole file into memory: S3 / storage streams are spooled to a temp file.
        """
        opened = self._open(booking, entry)
        try:
            yield opened
        finally:
            if opened is not None:
                try:
                    opened[0].close()
                except Exception:  # noqa: BLE001
                    pass

    def _open(self, booking: Booking, entry: dict[str, Any]) -> tuple[Any, str, int] | None:
        source = str(entry.get("source") or "")
        s3_key = (entry.get("s3_key") or entry.get("key") or "").strip()
        plain_s3 = bool(s3_key) and not s3_key.startswith(("dsa:", "booking_result:"))

        if source in {"s3", "dsa"} and plain_s3:
            body = open_results_s3_stream(s3_key)
            if body is not None:
                return spool_stream(body)

        attachment_id = entry.get("attachment_id")
        if attachment_id:
            att = booking_result_attachments_qs(booking).filter(id=attachment_id).first()
            if att:
                s3 = (att.s3_key or "").strip()
                if s3:
                    body = open_results_s3_stream(s3)
                    if body is not None:
                        return spool_stream(body)
                path = resolve_dsa_attachment_path(att)
                if path is not None:
                    digest, size = sha256_path(path)
                    return open(path, "rb"), digest, size

        file_id = entry.get("file_id")
        if file_id:
            brf = booking_result_files_qs(booking).filter(pk=file_id).first()
            if brf and brf.file:
                return spool_stream(brf.file.open("rb"))

        if plain_s3:
            body = open_results_s3_stream(s3_key)
            if body is not None:
                return spool_stream(body)

        return None
