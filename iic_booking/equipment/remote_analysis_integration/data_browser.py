"""R12 — human-friendly browser over booking result data available for analysis.

Presentation layer only: dataset discovery reuses the existing result merge
(``BookingRawStagingService.list_raw_entries`` → S3 + DSA + operator uploads) and
staging reuses ``BookingRawStagingService.stage_into_workspace``. Nothing here
talks to S3, DSA or the agent directly.

Responses carry metadata only — presigned/download URLs and raw storage keys are
stripped so a browse response can never be used as a durable data link.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any

from django.db.models import Max, Min
from django.utils import timezone

from iic_booking.equipment.models import Booking
from iic_booking.equipment.remote_analysis_integration.raw_staging import BookingRawStagingService
from iic_booking.equipment.remote_analysis_integration.workspace import BookingWorkspaceFacade

logger = logging.getLogger(__name__)

SCOPE_CURRENT = "current"
SCOPE_PREVIOUS = "previous"
SCOPE_ALL = "all"
VALID_SCOPES = (SCOPE_CURRENT, SCOPE_PREVIOUS, SCOPE_ALL)

#: Cap on sibling bookings pulled in for the "previous" scope.
MAX_PREVIOUS_BOOKINGS = 25

#: Audit action used to persist a user's dataset selection (no schema change).
SELECTION_AUDIT_ACTION = "booking_data_selection"

ROOT_FOLDER_LABEL = "Booking results"

#: Labels on equipment dynamic input fields that identify the sample.
_SAMPLE_LABEL_HINTS = ("sample name", "sample id", "sample code", "sample")


def _as_date(value: str | None) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _file_type(name: str) -> str:
    suffix = PurePosixPath(name).suffix.lstrip(".").lower()
    return suffix or "file"


def _split_entry_path(raw_name: str) -> tuple[str, str]:
    """Return ``(folder_path, file_name)`` for a merged result entry name."""
    cleaned = (raw_name or "").replace("\\", "/").strip("/")
    if not cleaned:
        return "", "result.bin"
    parts = [p for p in cleaned.split("/") if p and p != "."]
    if len(parts) <= 1:
        return "", parts[0] if parts else "result.bin"
    return "/".join(parts[:-1]), parts[-1]


class BookingAnalysisDataBrowserService:
    """Build human-friendly dataset listings and record analysis-data selections."""

    def __init__(self) -> None:
        self.staging = BookingRawStagingService()
        self.workspace = BookingWorkspaceFacade()

    # ------------------------------------------------------------------ browse

    def browse(
        self,
        booking: Booking,
        user,
        *,
        q: str = "",
        equipment: str = "",
        sample: str = "",
        date_from: str = "",
        date_to: str = "",
        scope: str = SCOPE_CURRENT,
        request=None,
        can_access,
    ) -> dict[str, Any]:
        """List datasets this user may read for ``booking`` and its sibling bookings.

        ``can_access(user, booking) -> bool`` is injected so the view keeps a single
        source of truth for analysis-file authorization.
        """
        scope = (scope or SCOPE_CURRENT).strip().lower()
        if scope not in VALID_SCOPES:
            scope = SCOPE_CURRENT

        candidates = self._candidate_bookings(booking, scope)
        allowed = [b for b in candidates if can_access(user, b)]

        query = (q or "").strip()
        equipment_filter = (equipment or "").strip().lower()
        sample_filter = (sample or "").strip().lower()
        from_date = _as_date(date_from)
        to_date = _as_date(date_to)

        datasets: list[dict[str, Any]] = []
        for candidate in allowed:
            dataset = self._build_dataset(
                candidate,
                is_current=candidate.pk == booking.pk,
                request=request,
            )
            if equipment_filter and equipment_filter not in dataset["_equipment_haystack"]:
                continue
            if sample_filter and sample_filter not in (dataset["sample_name"] or "").lower():
                continue
            booking_day = dataset["_booking_date_obj"]
            if from_date and (booking_day is None or booking_day < from_date):
                continue
            if to_date and (booking_day is None or booking_day > to_date):
                continue
            filtered = self._apply_search(dataset, query)
            if filtered is not None:
                datasets.append(filtered)

        return {
            "datasets": datasets,
            "query": query,
            "scope": scope,
            "filters": {
                "equipment": equipment or "",
                "sample": sample or "",
                "date_from": date_from or "",
                "date_to": date_to or "",
            },
            "counts": {
                "datasets": len(datasets),
                "files": sum(len(folder["files"]) for ds in datasets for folder in ds["folders"]),
            },
        }

    def _candidate_bookings(self, booking: Booking, scope: str) -> list[Booking]:
        current = [booking] if scope in (SCOPE_CURRENT, SCOPE_ALL) else []
        if scope == SCOPE_CURRENT:
            return current

        # "Previous" means earlier bookings by the SAME user on the SAME equipment —
        # never a widening of the caller's own data visibility.
        previous_qs = (
            Booking.objects.filter(user_id=booking.user_id, equipment_id=booking.equipment_id)
            .exclude(pk=booking.pk)
            .filter(booking_id__lt=booking.pk)
            .select_related("equipment", "user")
            .order_by("-booking_id")[:MAX_PREVIOUS_BOOKINGS]
        )
        return current + list(previous_qs)

    def _build_dataset(self, booking: Booking, *, is_current: bool, request=None) -> dict[str, Any]:
        try:
            entries = self.staging.list_raw_entries(booking, request=request)
        except Exception as exc:  # noqa: BLE001 — one bad booking must not break the browse
            logger.warning("Data browser: result listing failed for booking %s: %s", booking.pk, exc)
            entries = []

        start, end = self._booking_window(booking)
        equipment_name = getattr(booking.equipment, "name", "") or ""
        equipment_code = getattr(booking.equipment, "code", "") or ""
        virtual_id = (booking.virtual_booking_id or "").strip()

        return {
            "booking_id": virtual_id or str(booking.pk),
            "booking_pk": booking.pk,
            "virtual_booking_id": virtual_id,
            "equipment_name": equipment_name,
            "equipment_code": equipment_code,
            "sample_name": self._sample_name(booking),
            "booking_date": timezone.localtime(start).date().isoformat() if start else None,
            "booking_time": timezone.localtime(start).strftime("%H:%M") if start else None,
            "booking_end_time": timezone.localtime(end).strftime("%H:%M") if end else None,
            "status": booking.status,
            "is_current": is_current,
            "folders": self._folders(entries),
            "_booking_date_obj": timezone.localtime(start).date() if start else None,
            "_equipment_haystack": f"{equipment_name} {equipment_code}".lower(),
        }

    @staticmethod
    def _booking_window(booking: Booking) -> tuple[datetime | None, datetime | None]:
        agg = booking.daily_slots.aggregate(start=Min("start_datetime"), end=Max("end_datetime"))
        return agg.get("start"), agg.get("end")

    @staticmethod
    def _sample_name(booking: Booking) -> str:
        """Best-effort human sample label from traces, notes, or dynamic inputs."""
        # Prefer sample lifecycle identifiers when present.
        try:
            trace = (
                booking.sample_trace_events.exclude(sample_identifiers="")
                .order_by("-created_at")
                .first()
            )
            if trace and (trace.sample_identifiers or "").strip():
                return str(trace.sample_identifiers).strip()
        except Exception:  # noqa: BLE001
            pass

        notes = (getattr(booking, "notes", None) or "").strip()
        if notes:
            return notes[:120]

        values = booking.input_values or {}
        if not isinstance(values, dict) or not values:
            return ""
        try:
            fields = list(booking.equipment.input_fields.all())
        except Exception:  # noqa: BLE001
            fields = []
        for hint in _SAMPLE_LABEL_HINTS:
            for field in fields:
                if hint in (field.field_label or "").strip().lower():
                    value = values.get(field.field_key)
                    if isinstance(value, (str, int, float)) and str(value).strip():
                        return str(value).strip()
        return ""

    @staticmethod
    def _folders(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Group merged result entries into folders, metadata only (no URLs/keys)."""
        buckets: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            folder_path, file_name = _split_entry_path(str(entry.get("name") or ""))
            buckets.setdefault(folder_path, []).append(
                {
                    "name": file_name,
                    "size": int(entry.get("size_bytes") or 0),
                    "type": _file_type(file_name),
                    "modified_at": entry.get("uploaded_at"),
                    "source": entry.get("source") or "s3",
                }
            )
        folders: list[dict[str, Any]] = []
        for path in sorted(buckets):
            folders.append(
                {
                    "name": PurePosixPath(path).name if path else ROOT_FOLDER_LABEL,
                    "path": path,
                    "files": sorted(buckets[path], key=lambda f: f["name"].lower()),
                }
            )
        return folders

    @staticmethod
    def _apply_search(dataset: dict[str, Any], query: str) -> dict[str, Any] | None:
        """Return the dataset (whole or file-filtered) when it matches ``query``."""
        dataset = {k: v for k, v in dataset.items() if not k.startswith("_")}
        if not query:
            return dataset

        needle = query.lower()
        searchable = ("equipment_name", "equipment_code", "sample_name", "virtual_booking_id", "booking_id")
        if any(needle in str(dataset.get(key) or "").lower() for key in searchable):
            return dataset

        folders: list[dict[str, Any]] = []
        for folder in dataset["folders"]:
            if needle in folder["name"].lower() or needle in folder["path"].lower():
                folders.append(folder)
                continue
            files = [f for f in folder["files"] if needle in f["name"].lower()]
            if files:
                folders.append({**folder, "files": files})
        if not folders:
            return None
        return {**dataset, "folders": folders}

    # --------------------------------------------------------------- selection

    def select(
        self,
        booking: Booking,
        user,
        *,
        source_booking_id: Any,
        folder_path: str | None = None,
        file_names: list[str] | None = None,
        stage: bool = True,
        request=None,
        can_access,
    ) -> dict[str, Any]:
        """Validate + record a dataset selection, optionally staging it into RawData.

        Raises ``PermissionError`` when the caller may not read the source booking
        and ``ValueError`` for unusable input.
        """
        source = self._resolve_source_booking(booking, source_booking_id)
        if not can_access(user, source):
            raise PermissionError("You do not have access to the selected booking data.")

        entries = self.staging.list_raw_entries(source, request=request)
        matched = self._match_entries(entries, folder_path=folder_path, file_names=file_names)
        if not matched:
            raise ValueError("No matching files found for the selection.")

        selection = {
            "source_booking_pk": source.pk,
            "source_virtual_booking_id": (source.virtual_booking_id or "").strip(),
            "folder_path": (folder_path or "").strip("/"),
            "file_names": [str(n) for n in (file_names or [])],
            "matched_file_names": [str(e.get("name") or "") for e in matched],
            "selected_at": timezone.now().isoformat(),
            "selected_by_id": getattr(user, "pk", None),
        }

        workspace = self.workspace.get_for_booking(booking)
        self._record_selection(booking, workspace, user, selection)

        staged: dict[str, Any] | None = None
        if stage and workspace is not None:
            staged = self.staging.stage_into_workspace(
                source,
                workspace,
                actor=user,
                request=request,
                entries=matched,
            )

        return {
            "selection": selection,
            "selected_files": len(matched),
            "workspace_id": str(workspace.id) if workspace else None,
            "staged": staged,
            "detail": (
                "Selection recorded and staged into RawData."
                if staged
                else "Selection recorded. Files will be staged when the workspace is created."
            ),
        }

    @staticmethod
    def _resolve_source_booking(booking: Booking, source_booking_id: Any) -> Booking:
        raw = str(source_booking_id or "").strip()
        if not raw:
            return booking
        qs = Booking.objects.select_related("equipment", "user")
        if raw.isdigit():
            found = qs.filter(pk=int(raw)).first()
            if found:
                return found
        found = qs.filter(virtual_booking_id=raw).first()
        if not found:
            raise ValueError(f"Unknown source booking '{raw}'.")
        return found

    @staticmethod
    def _match_entries(
        entries: list[dict[str, Any]],
        *,
        folder_path: str | None,
        file_names: list[str] | None,
    ) -> list[dict[str, Any]]:
        wanted_folder = (folder_path or "").replace("\\", "/").strip("/")
        wanted_files = {str(n).strip().lower() for n in (file_names or []) if str(n).strip()}

        matched: list[dict[str, Any]] = []
        for entry in entries:
            entry_folder, entry_file = _split_entry_path(str(entry.get("name") or ""))
            if folder_path is not None and entry_folder != wanted_folder:
                continue
            if wanted_files and entry_file.lower() not in wanted_files:
                continue
            matched.append(entry)
        return matched

    @staticmethod
    def _record_selection(booking: Booking, workspace, user, selection: dict[str, Any]) -> None:
        """Persist the selection on the existing workspace audit trail (no new schema)."""
        from iic_booking.remote_analysis.workspace_models import WorkspaceAudit

        payload = dict(selection)
        payload["booking_pk"] = booking.pk
        try:
            WorkspaceAudit.objects.create(
                workspace=workspace,
                action=SELECTION_AUDIT_ACTION,
                details=json.dumps(payload, default=str)[:8000],
                actor=user if getattr(user, "pk", None) else None,
                success=True,
            )
        except Exception as exc:  # noqa: BLE001 — audit must never block the selection
            logger.warning("Data browser: failed to audit selection for booking %s: %s", booking.pk, exc)

    def latest_selection(self, booking: Booking) -> dict[str, Any] | None:
        """Most recent recorded selection for this booking's workspace, if any."""
        from iic_booking.remote_analysis.workspace_models import WorkspaceAudit

        workspace = self.workspace.get_for_booking(booking)
        if workspace is None:
            return None
        row = (
            WorkspaceAudit.objects.filter(workspace=workspace, action=SELECTION_AUDIT_ACTION)
            .order_by("-created_at")
            .first()
        )
        if not row:
            return None
        try:
            return json.loads(row.details)
        except (TypeError, ValueError):
            return None

# Compatibility alias for older imports.
AnalysisDataBrowserService = BookingAnalysisDataBrowserService

