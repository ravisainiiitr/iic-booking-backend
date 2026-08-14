"""R12/R14 analysis data browser — authorized current/previous booking files.

Reuses BookingRawStagingService / merge_booking_result_files. Does not expose
other users' data. Virtual booking IDs are presentation-only.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from django.db.models import Q
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingResultFile, BookingSampleTrace
from iic_booking.equipment.remote_analysis_integration.raw_staging import BookingRawStagingService
from iic_booking.sync.models import ResultAttachment

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
DEFAULT_FILE_LIMIT = 40
MAX_FILE_LIMIT = 100


def _virtual_id(booking: Booking) -> str:
    return (getattr(booking, "virtual_booking_id", None) or "").strip() or f"booking-{booking.pk}"


def _slot_bounds(booking: Booking) -> tuple[datetime | None, datetime | None]:
    slot = booking.daily_slots.order_by("start_datetime").first()
    if not slot:
        return None, None
    return slot.start_datetime, slot.end_datetime


def _sample_label(booking: Booking) -> str:
    row = (
        BookingSampleTrace.objects.filter(booking_id=booking.pk)
        .exclude(sample_identifiers="")
        .order_by("-created_at")
        .first()
    )
    if row and (row.sample_identifiers or "").strip():
        return row.sample_identifiers.strip()[:200]
    return ""


def _cheap_file_stats(booking: Booking) -> tuple[int, int]:
    att_qs = ResultAttachment.objects.filter(result__booking_id=booking.pk)
    op_count = BookingResultFile.objects.filter(booking_id=booking.pk).count()
    count = att_qs.count() + op_count
    size = 0
    for value in att_qs.values_list("size_bytes", flat=True):
        size += int(value or 0)
    return count, size


def _folder_name_from_path(relative: str) -> tuple[str, str]:
    rel = (relative or "").replace("\\", "/").lstrip("/")
    if "/" not in rel:
        return "Root", ""
    folder = rel.rsplit("/", 1)[0]
    return folder.split("/")[0] or "Root", folder


def _matches_query(haystack: str, q: str) -> bool:
    if not q:
        return True
    return q in (haystack or "").lower()


def _group_entries(
    entries: list[dict[str, Any]],
    *,
    q: str = "",
    file_type: str = "",
    folder_prefix: str = "",
    file_offset: int = 0,
    file_limit: int = DEFAULT_FILE_LIMIT,
) -> tuple[list[dict[str, Any]], int, int]:
    folders: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "path": "", "files": [], "file_count": 0, "total_size_bytes": 0, "has_more_files": False}
    )
    q = (q or "").strip().lower()
    file_type = (file_type or "").strip().lower().lstrip(".")
    folder_prefix = (folder_prefix or "").replace("\\", "/").strip("/")
    matched: list[dict[str, Any]] = []

    for entry in entries:
        name = str(entry.get("name") or entry.get("relative_path") or "").replace("\\", "/").lstrip("/")
        if not name or ".." in name.split("/"):
            continue
        leaf = Path(name).name
        ext = Path(leaf).suffix.lower().lstrip(".")
        folder_label, folder_path = _folder_name_from_path(name)
        if folder_prefix and not (folder_path == folder_prefix or folder_path.startswith(folder_prefix + "/") or name.startswith(folder_prefix + "/")):
            continue
        if file_type and ext != file_type:
            continue
        blob = " ".join(
            [
                name.lower(),
                leaf.lower(),
                folder_label.lower(),
                folder_path.lower(),
                str(entry.get("source") or "").lower(),
            ]
        )
        if q and q not in blob:
            continue
        size = int(entry.get("size_bytes") or entry.get("size") or 0)
        matched.append(
            {
                "name": leaf,
                "relative_path": name,
                "size": size,
                "size_bytes": size,
                "type": ext or "file",
                "modified_at": entry.get("uploaded_at") or entry.get("modified_at"),
                "source": entry.get("source") or "",
                "entry_key": entry.get("key") or name,
                "_folder_label": folder_label,
                "_folder_path": folder_path,
            }
        )

    total_files = len(matched)
    total_size = sum(int(f["size_bytes"] or 0) for f in matched)
    window = matched[file_offset : file_offset + file_limit]
    for item in window:
        key = item["_folder_path"] or item["_folder_label"] or "Root"
        bucket = folders[key]
        bucket["name"] = item["_folder_label"] or "Root"
        bucket["path"] = item["_folder_path"]
        bucket["file_count"] = bucket["file_count"] + 1
        bucket["total_size_bytes"] = int(bucket["total_size_bytes"] or 0) + int(item["size_bytes"] or 0)
        clean = {k: v for k, v in item.items() if not k.startswith("_")}
        bucket["files"].append(clean)

    # Preserve unmatched folder counts when pagination sliced files away
    if total_files > file_offset + file_limit:
        for bucket in folders.values():
            bucket["has_more_files"] = True

    rows = list(folders.values())
    rows.sort(key=lambda r: (r.get("path") or r.get("name") or "").lower())
    return rows, total_files, total_size


def _serialize_booking_row(
    booking: Booking,
    *,
    current: Booking,
    include_folders: bool,
    q: str = "",
    file_type: str = "",
    folder_prefix: str = "",
    file_offset: int = 0,
    file_limit: int = DEFAULT_FILE_LIMIT,
    request=None,
) -> dict[str, Any]:
    start, end = _slot_bounds(booking)
    sample = _sample_label(booking)
    virtual = _virtual_id(booking)
    equipment = booking.equipment
    row: dict[str, Any] = {
        "booking_id": virtual,
        "booking_pk": booking.pk,
        "booking_reference": virtual,
        "virtual_booking_id": virtual,
        "equipment_name": getattr(equipment, "name", "") or "",
        "equipment_code": getattr(equipment, "code", "") or "",
        "sample_name": sample,
        "booking_date": start.date().isoformat() if start else None,
        "booking_time": start.strftime("%H:%M") if start else None,
        "is_current": booking.pk == current.pk,
        "file_count": 0,
        "total_size_bytes": 0,
        "folders": [],
    }
    if include_folders:
        entries = BookingRawStagingService().list_raw_entries(booking, request=request)
        folders, count, size = _group_entries(
            entries,
            q=q,
            file_type=file_type,
            folder_prefix=folder_prefix,
            file_offset=file_offset,
            file_limit=file_limit,
        )
        row["folders"] = folders
        row["file_count"] = count
        row["total_size_bytes"] = size
    else:
        count, size = _cheap_file_stats(booking)
        row["file_count"] = count
        row["total_size_bytes"] = size
    return row


class AnalysisDataBrowserService:
    """List and confirm analysis input datasets for a booking session."""

    def authorized_queryset(self, current: Booking):
        """Same owner + same equipment. Never other users' bookings."""
        return (
            Booking.objects.filter(
                user_id=current.user_id,
                equipment_id=current.equipment_id,
            )
            .exclude(status__in=["CANCELLED", "REFUNDED"])
            .select_related("equipment", "user")
        )

    def resolve_source_booking(self, current: Booking, source_booking_id: int) -> Booking | None:
        if not source_booking_id:
            return None
        return self.authorized_queryset(current).filter(pk=int(source_booking_id)).first()

    def browse(
        self,
        current: Booking,
        *,
        user,
        q: str = "",
        scope: str = "current",
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        source_booking_id: int | None = None,
        prefix: str = "",
        file_offset: int = 0,
        file_limit: int = DEFAULT_FILE_LIMIT,
        file_type: str = "",
        request=None,
    ) -> dict[str, Any]:
        page = max(1, int(page or 1))
        page_size = min(MAX_PAGE_SIZE, max(1, int(page_size or DEFAULT_PAGE_SIZE)))
        file_offset = max(0, int(file_offset or 0))
        file_limit = min(MAX_FILE_LIMIT, max(1, int(file_limit or DEFAULT_FILE_LIMIT)))
        q = (q or "").strip()
        scope = (scope or "current").strip().lower()
        if scope not in {"current", "previous", "all"}:
            scope = "current"

        qs = self.authorized_queryset(current)
        if scope == "current":
            qs = qs.filter(pk=current.pk)
        elif scope == "previous":
            qs = qs.exclude(pk=current.pk)

        if q:
            file_hits = list(
                ResultAttachment.objects.filter(
                    result__booking_id__in=qs.values("pk"),
                    file_name__icontains=q,
                ).values_list("result__booking_id", flat=True)[:200]
            )
            op_hits = list(
                BookingResultFile.objects.filter(
                    booking_id__in=qs.values("pk"),
                    original_name__icontains=q,
                ).values_list("booking_id", flat=True)[:200]
            )
            qs = qs.filter(
                Q(virtual_booking_id__icontains=q)
                | Q(sample_trace_events__sample_identifiers__icontains=q)
                | Q(pk__in=set(file_hits + op_hits))
            ).distinct()

        # Equipment/sample/file-type query params are ignored as UI filters.
        # file_type only applies when expanding folders for a single booking.

        if source_booking_id:
            source = self.resolve_source_booking(current, int(source_booking_id))
            if source is None:
                return {
                    "datasets": [],
                    "pagination": {"page": 1, "page_size": 1, "has_more": False, "total": 0},
                    "scope": scope,
                    "equipment_code": getattr(current.equipment, "code", "") or "",
                    "virtual_booking_id": _virtual_id(current),
                }
            row = _serialize_booking_row(
                source,
                current=current,
                include_folders=True,
                q=q,
                file_type=file_type,
                folder_prefix=prefix,
                file_offset=file_offset,
                file_limit=file_limit,
                request=request,
            )
            return {
                "datasets": [row],
                "pagination": {"page": 1, "page_size": 1, "has_more": False, "total": 1},
                "scope": scope,
                "equipment_code": getattr(current.equipment, "code", "") or "",
                "virtual_booking_id": _virtual_id(current),
            }

        total = qs.count()
        offset = (page - 1) * page_size
        bookings = list(qs.order_by("-booking_id")[offset : offset + page_size])
        include_folders = scope == "current" and len(bookings) == 1 and not q
        datasets = [
            _serialize_booking_row(
                b,
                current=current,
                include_folders=include_folders,
                q=q,
                file_type=file_type,
                request=request,
            )
            for b in bookings
        ]
        # When searching, still attach folders for the current booking so file/folder hits are visible.
        if q:
            for row, b in zip(datasets, bookings, strict=False):
                detailed = _serialize_booking_row(
                    b,
                    current=current,
                    include_folders=True,
                    q=q,
                    file_type=file_type,
                    request=request,
                )
                if detailed["file_count"] or _matches_query(_virtual_id(b), q.lower()) or _matches_query(
                    _sample_label(b), q.lower()
                ):
                    row.update(detailed)
        return {
            "datasets": datasets,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "has_more": offset + page_size < total,
                "total": total,
            },
            "scope": scope,
            "equipment_code": getattr(current.equipment, "code", "") or "",
            "virtual_booking_id": _virtual_id(current),
        }

    def save_selection(
        self,
        current: Booking,
        *,
        user,
        source_booking_id: int,
        folder_path: str = "",
        file_names: list[str] | None = None,
        source_kind: str = "",
    ) -> dict[str, Any]:
        source = self.resolve_source_booking(current, int(source_booking_id))
        if source is None:
            raise PermissionError("You are not authorized to use that booking's data.")
        names = [str(n).replace("\\", "/").lstrip("/") for n in (file_names or []) if str(n).strip()]
        for name in names:
            if ".." in name.split("/"):
                raise ValueError("Invalid file path.")
        folder_path = (folder_path or "").replace("\\", "/").strip("/")
        kind = (source_kind or "").strip().lower()
        if not kind:
            kind = "current" if source.pk == current.pk else "previous"

        entries = BookingRawStagingService().list_raw_entries(source)
        if names:
            allowed = {n.lower() for n in names}
            selected_entries = [
                e
                for e in entries
                if str(e.get("name") or "").replace("\\", "/").lstrip("/").lower() in allowed
            ]
        elif folder_path:
            selected_entries = [
                e
                for e in entries
                if str(e.get("name") or "").replace("\\", "/").lstrip("/").startswith(folder_path + "/")
                or str(e.get("name") or "").replace("\\", "/").rsplit("/", 1)[0] == folder_path
            ]
        else:
            selected_entries = list(entries)

        preview_names = [
            Path(str(e.get("name") or "")).name for e in selected_entries[:12] if e.get("name")
        ]
        total_size = sum(int(e.get("size_bytes") or e.get("size") or 0) for e in selected_entries)
        virtual = _virtual_id(source)
        payload = {
            "source": kind,
            "source_booking_id": source.pk,
            "virtual_booking_id": virtual,
            "folder_path": folder_path,
            "file_names": names or [str(e.get("name") or "").replace("\\", "/").lstrip("/") for e in selected_entries],
            "file_count": len(selected_entries) if not names else len(names) or len(selected_entries),
            "total_size_bytes": total_size,
            "sample_name": _sample_label(source),
            "equipment_name": getattr(source.equipment, "name", "") or "",
            "confirmed_at": timezone.now().isoformat(),
            "confirmed_by_id": getattr(user, "pk", None),
        }
        current.analysis_data_selection = payload
        current.save(update_fields=["analysis_data_selection", "updated_at"])
        return {
            "ok": True,
            "selection": payload,
            "preview": {
                "virtual_booking_id": virtual,
                "folder": folder_path or (preview_names[0] if len(preview_names) == 1 else folder_path),
                "files": preview_names,
                "file_count": payload["file_count"],
                "size": total_size,
                "sample_name": payload["sample_name"],
            },
        }

    def save_upload_selection(self, current: Booking, *, user, file_names: list[str] | None = None) -> dict[str, Any]:
        names = [str(n).strip() for n in (file_names or []) if str(n).strip()]
        payload = {
            "source": "upload",
            "source_booking_id": current.pk,
            "virtual_booking_id": _virtual_id(current),
            "folder_path": "RawData",
            "file_names": names,
            "file_count": len(names),
            "total_size_bytes": 0,
            "confirmed_at": timezone.now().isoformat(),
            "confirmed_by_id": getattr(user, "pk", None),
        }
        current.analysis_data_selection = payload
        current.save(update_fields=["analysis_data_selection", "updated_at"])
        return {"ok": True, "selection": payload, "preview": payload}
