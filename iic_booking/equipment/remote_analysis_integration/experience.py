"""Researcher-facing Analysis Workspace experience payload (no infra leakage)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    QueueEntryStatus,
    ReservationStatus,
    SessionStatus,
    WorkstationStatus,
)


QUEUED_RESERVATION = {
    ReservationStatus.QUEUED,
    ReservationStatus.REQUESTED,
    ReservationStatus.VALIDATING,
}

OPEN_SESSION = {
    SessionStatus.CREATED,
    SessionStatus.PREPARING,
    SessionStatus.READY,
    SessionStatus.TOKEN_GENERATED,
    SessionStatus.LAUNCHED,
    SessionStatus.CONNECTING,
    SessionStatus.CONNECTED,
    SessionStatus.ACTIVE,
    SessionStatus.IDLE,
}

ACTIVE_DESKTOP = {
    SessionStatus.LAUNCHED,
    SessionStatus.CONNECTING,
    SessionStatus.CONNECTED,
    SessionStatus.ACTIVE,
    SessionStatus.IDLE,
}


def _ts(dt) -> str | None:
    if not dt:
        return None
    return dt.isoformat()


def _file_stats(files: list[dict], *, prefix: str | None = None) -> dict[str, Any]:
    rows = files
    if prefix:
        rows = [f for f in files if str(f.get("relative_path") or "").startswith(prefix)]
    total_size = 0
    latest = None
    for f in rows:
        total_size += int(f.get("size") or 0)
        updated = f.get("updated_at")
        if updated and (latest is None or str(updated) > str(latest)):
            latest = updated
    return {
        "file_count": len(rows),
        "total_size_bytes": total_size,
        "last_updated": latest,
    }


class AnalysisExperienceBuilder:
    """Assemble timeline / queue / sync / timer UX from existing models."""

    def build(self, booking, *, summary: dict | None = None, files: list | None = None) -> dict[str, Any]:
        from iic_booking.remote_analysis.models import AnalysisWorkstation
        from iic_booking.remote_analysis.scheduler_models import ReservationQueue
        from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings, RemoteDesktopSession
        from iic_booking.remote_analysis.workspace_models import WorkspaceFile

        now = timezone.now()
        equipment = booking.equipment
        reservation = booking.analysis_reservation
        workspace = booking.analysis_workspace
        settings_obj = RemoteAnalysisSettings.get_solo()

        session = None
        if reservation:
            session = (
                RemoteDesktopSession.objects.filter(reservation=reservation)
                .order_by("-created_at")
                .first()
            )
        if session is None:
            session = (
                RemoteDesktopSession.objects.filter(booking_id=booking.pk)
                .order_by("-created_at")
                .first()
            )

        if files is None:
            files = []
            if workspace:
                files = list(
                    WorkspaceFile.objects.filter(workspace=workspace, deleted=False, is_current=True)
                    .order_by("relative_path")
                    .values("id", "original_name", "relative_path", "size", "modified_at", "uploaded_at", "source")
                )
                files = [
                    {
                        "id": str(f["id"]),
                        "name": f["original_name"] or f["relative_path"],
                        "relative_path": f["relative_path"],
                        "size": f["size"] or 0,
                        "updated_at": _ts(f["modified_at"] or f["uploaded_at"]),
                        "source": f.get("source") or "",
                    }
                    for f in files
                ]

        booking_raw = [
            f
            for f in files
            if str(f.get("relative_path") or "").startswith("RawData/")
        ]
        # Treat portal uploads after staging as "additional" when source=portal and name not from ingest —
        # UI also lets user pick; stats: all RawData vs Processed
        raw_stats = _file_stats(files, prefix="RawData/")
        extra_stats = _file_stats(
            [f for f in files if str(f.get("relative_path") or "").startswith("RawData/") and f.get("source") == "portal"],
            prefix=None,
        )
        # If we cannot distinguish, show all RawData as booking RAW and additional as count of portal-only beyond first wave
        if extra_stats["file_count"] == raw_stats["file_count"] and raw_stats["file_count"]:
            # Prefer listing booking RAW as all RawData; additional starts empty until user uploads more
            extra_stats = {"file_count": 0, "total_size_bytes": 0, "last_updated": None}
        output_stats = _file_stats(files, prefix="Processed/")
        if output_stats["file_count"] == 0:
            output_stats = _file_stats(files, prefix="Output/")

        default_session_minutes = int(
            getattr(equipment, "analysis_default_session_minutes", None)
            or getattr(settings_obj, "session_timeout", 30)
            or 30
        )
        extension_minutes = int(getattr(equipment, "analysis_extension_minutes", None) or 15)

        # Environment pool (logical counts only — no hostnames)
        from iic_booking.equipment.remote_analysis_integration.software import SoftwareMappingService
        from iic_booking.remote_analysis.models import InstalledSoftware

        required_software = SoftwareMappingService().required_software_names(equipment)
        ws_qs = AnalysisWorkstation.objects.filter(enabled=True)
        matching_ids: list = []
        if required_software:
            for ws in ws_qs.only("id", "status"):
                if all(
                    InstalledSoftware.objects.filter(
                        workstation_id=ws.id, is_present=True, software_name__icontains=name
                    ).exists()
                    for name in required_software
                ):
                    matching_ids.append(ws.id)
            matching_qs = ws_qs.filter(id__in=matching_ids) if matching_ids else ws_qs.none()
        else:
            matching_qs = ws_qs
        env_total = matching_qs.count() if required_software else ws_qs.count()
        env_available = matching_qs.filter(
            status__in=[WorkstationStatus.AVAILABLE, WorkstationStatus.ONLINE]
        ).count()
        env_busy = matching_qs.filter(
            status__in=[
                WorkstationStatus.BUSY,
                WorkstationStatus.PREPARING,
                WorkstationStatus.RESERVED,
            ]
        ).count()
        matching_total = env_total
        matching_busy = env_busy
        matching_available = env_available
        all_env_total = ws_qs.count()

        from iic_booking.remote_analysis.services.maintenance import MaintenanceService

        maintenance_hint = MaintenanceService().next_compatible_availability(
            required_software=required_software or None,
            matching_workstation_ids=matching_ids if required_software else None,
        )

        waiting = list(
            ReservationQueue.objects.filter(status=QueueEntryStatus.WAITING)
            .select_related("reservation")
            .order_by("priority", "enqueued_at")
        )
        queue_total = len(waiting)
        queue_position = None
        people_ahead = 0
        if reservation:
            for idx, entry in enumerate(waiting, start=1):
                if entry.reservation_id == reservation.id:
                    queue_position = idx
                    people_ahead = idx - 1
                    break
            if queue_position is None and reservation.status in QUEUED_RESERVATION:
                queue_position = queue_total + 1
                people_ahead = queue_total

        avg_minutes = max(5, min(default_session_minutes, 120))
        estimated_wait_minutes = None
        expected_start = None
        if queue_position:
            estimated_wait_minutes = people_ahead * avg_minutes
            expected_start = now + timedelta(minutes=estimated_wait_minutes)

        anyone_waiting = queue_total > 0 and (
            queue_position is None or (queue_position is not None and queue_total > 1)
        )
        # More precise: someone else waiting
        others_waiting = any(e.reservation_id != getattr(reservation, "id", None) for e in waiting)

        remaining_seconds = None
        expires_at = None
        if session and session.status in OPEN_SESSION and session.expires_at:
            expires_at = session.expires_at
            remaining_seconds = max(0, int((session.expires_at - now).total_seconds()))

        can_extend = bool(
            session
            and session.status in ACTIVE_DESKTOP
            and not others_waiting
            and remaining_seconds is not None
        )
        extend_blocked_reason = None
        if session and session.status in ACTIVE_DESKTOP and others_waiting:
            extend_blocked_reason = (
                "Another analysis request is currently waiting. "
                "Session extension is unavailable to ensure fair access."
            )

        sync_phase = getattr(workspace, "sync_phase", None) if workspace else None
        sync_progress = int(getattr(workspace, "sync_progress_percent", 0) or 0) if workspace else 0
        sync_message = (getattr(workspace, "sync_message", "") or "") if workspace else ""

        journey = self._journey(
            booking=booking,
            reservation=reservation,
            session=session,
            workspace=workspace,
            raw_stats=raw_stats,
            output_stats=output_stats,
            queued=bool(reservation and reservation.status in QUEUED_RESERVATION) or queue_position is not None,
            remaining_seconds=remaining_seconds,
        )

        sync_pipeline = self._sync_pipeline(
            booking=booking,
            reservation=reservation,
            session=session,
            workspace=workspace,
            raw_stats=raw_stats,
            output_stats=output_stats,
            sync_phase=sync_phase,
            sync_progress=sync_progress,
        )

        desktop_prepare = self._desktop_prepare(
            session=session,
            workspace=workspace,
            sync_phase=sync_phase,
            sync_progress=sync_progress,
        )

        from iic_booking.remote_analysis.services.checkin import CheckinService

        checkin = CheckinService().checkin_payload(reservation)

        return {
            "virtual_booking_id": (getattr(booking, "virtual_booking_id", None) or "")
            or str(booking.booking_id),
            "equipment_name": getattr(equipment, "name", "") or getattr(equipment, "code", ""),
            "equipment_code": getattr(equipment, "code", ""),
            "current_stage": next((s["id"] for s in reversed(journey) if s["status"] in {"active", "done"}), "booking"),
            "journey": journey,
            "checkin": checkin,
            "input_choice": {
                "prompt": "What data would you like to analyze?",
                "booking_raw": {
                    "label": "Use RAW Data uploaded with this booking",
                    **raw_stats,
                },
                "additional": {
                    "label": "Upload additional / alternative data",
                    **extra_stats,
                },
                "sync_note": "Selected data will be synchronized to the Analysis Environment before the desktop session begins.",
            },
            "queue": {
                "is_queued": bool(
                    (reservation and reservation.status in QUEUED_RESERVATION) or queue_position
                ),
                "title": (
                    "No compatible Analysis Workstation is currently available"
                    if maintenance_hint.get("all_under_maintenance") and matching_available == 0
                    else (
                        "Waiting for an Analysis PC with the required software"
                        if required_software
                        else "Analysis Environment Currently Unavailable"
                    )
                ),
                "body": (
                    [
                        f"Reason: {maintenance_hint.get('reason') or 'Scheduled Maintenance'}",
                        f"Estimated Availability: {maintenance_hint.get('estimated_availability_display') or 'Unknown'}",
                        "Your request remains in the queue and will be allocated automatically.",
                        "You may safely leave this page.",
                    ]
                    if maintenance_hint.get("all_under_maintenance") and matching_available == 0
                    else [
                        (
                            "No Analysis PC with the required software is free right now."
                            if required_software
                            else "All available Analysis Environments are currently processing other requests."
                        ),
                        "Your request has been placed in the Remote Analysis queue.",
                        "A matching Analysis PC will be allocated automatically when one becomes available.",
                        "You may safely leave this page.",
                        "You will receive notifications when your analysis starts.",
                    ]
                ),
                "required_software": required_software,
                "position": queue_position,
                "queue_size": max(queue_total, queue_position or 0),
                "people_ahead": people_ahead,
                "estimated_wait_minutes": estimated_wait_minutes,
                "expected_start_at": _ts(expected_start),
                "maintenance": maintenance_hint,
                "environments": {
                    "total": all_env_total,
                    "matching": matching_total,
                    "available": matching_available,
                    "busy": matching_busy,
                    "waiting": queue_total,
                },
            },
            "session": {
                "id": str(session.id) if session else None,
                "status": session.status if session else None,
                "default_duration_minutes": default_session_minutes,
                "extension_minutes": extension_minutes,
                "expires_at": _ts(expires_at),
                "remaining_seconds": remaining_seconds,
                "warnings": [10, 5, 2, 1],
                "can_extend": can_extend,
                "extend_blocked_reason": extend_blocked_reason,
                "others_waiting": others_waiting,
            },
            "workspace": {
                "label": "Booking Workspace",
                "status": getattr(workspace, "status", None) if workspace else None,
                "sync_phase": sync_phase,
                "sync_progress_percent": sync_progress,
                "sync_message": sync_message,
                "input": {"label": "Input Folder", "logical_name": "Uploaded Input Data", **raw_stats},
                "output": {"label": "Output Folder", "logical_name": "Generated Results", **output_stats},
                "last_synced_at": _ts(getattr(workspace, "last_synced_at", None)) if workspace else None,
                "raw_data_directory": (getattr(equipment, "analysis_raw_data_directory", None) or ""),
                "results_directory": (getattr(equipment, "analysis_results_directory", None) or ""),
                "instructions": [
                    "Raw files have already been copied to your Booking Folder under the Raw Data directory "
                    "(when configured for this equipment)."
                    if (getattr(equipment, "analysis_raw_data_directory", None) or "")
                    else "Raw files are synchronized into the Analysis Environment Input folder before the desktop opens.",
                    (
                        "Please save all analyzed files inside your Booking Folder under the Analyzed Data directory: "
                        f"{(getattr(equipment, 'analysis_results_directory', None) or '').rstrip(chr(92)+'/')}\\"
                        f"{(getattr(booking, 'virtual_booking_id', None) or booking.booking_id)}."
                    )
                    if (getattr(equipment, "analysis_results_directory", None) or "")
                    else "Please save all processed files inside the Analysis Environment Output / Processed folder.",
                    "After End Analysis, all results are uploaded to the booking and local copies are securely deleted.",
                ],
            },
            "sync_pipeline": sync_pipeline,
            "desktop_prepare": desktop_prepare,
            "results": {
                "available": output_stats["file_count"] > 0,
                "file_count": output_stats["file_count"],
                "total_size_bytes": output_stats["total_size_bytes"],
                "label": "Analyzed Data" if output_stats["file_count"] else "Analyzed Data pending",
            },
            "cleanup": {
                "status": self._cleanup_status(session, workspace),
                "message": self._cleanup_message(session, workspace),
            },
            "poll_interval_seconds": 5 if (
                (reservation and reservation.status in QUEUED_RESERVATION)
                or (session and session.status in OPEN_SESSION - {SessionStatus.ACTIVE})
                or (session and session.status == SessionStatus.ACTIVE)
            ) else 15,
        }

    def _cleanup_status(self, session, workspace) -> str:
        if not session:
            return "pending"
        if session.status in {SessionStatus.TERMINATED, SessionStatus.COMPLETED, SessionStatus.EXPIRED}:
            if workspace and str(getattr(workspace, "sync_phase", "")).lower() in {
                "completed",
                "cleaned",
                "archived",
            }:
                return "done"
            return "running"
        return "pending"

    def _cleanup_message(self, session, workspace) -> str:
        status = self._cleanup_status(session, workspace)
        if status == "done":
            return "Workspace cleanup completed. Ready for next analysis session."
        if status == "running":
            return "Cleaning workspace to protect previous user data…"
        return ""

    def _journey(self, **kw) -> list[dict[str, Any]]:
        booking = kw["booking"]
        reservation = kw["reservation"]
        session = kw["session"]
        workspace = kw["workspace"]
        raw_stats = kw["raw_stats"]
        output_stats = kw["output_stats"]
        queued = kw["queued"]
        remaining_seconds = kw["remaining_seconds"]

        def stage(sid, label, status, ts=None, detail=""):
            return {"id": sid, "label": label, "status": status, "timestamp": _ts(ts) if ts and not isinstance(ts, str) else ts, "detail": detail}

        # status: pending | active | done | skipped
        stages = []
        stages.append(stage("booking", "Booking Confirmed", "done", getattr(booking, "updated_at", None)))

        input_done = raw_stats["file_count"] > 0
        stages.append(
            stage(
                "choose_input",
                "Choose Input Data",
                "done" if input_done else "active",
                raw_stats.get("last_updated"),
                f"{raw_stats['file_count']} file(s)" if input_done else "Select booking RAW or upload data",
            )
        )

        if queued and not (reservation and reservation.workstation_id):
            stages.append(stage("waiting", "Waiting for Analysis Environment", "active", None, "In execution queue"))
            for sid, label in [
                ("allocated", "Analysis Environment Allocated"),
                ("sync_in", "Input Data Synchronization"),
                ("started", "Analysis Session Started"),
                ("remaining", "Remaining Session Time"),
                ("completed", "Analysis Completed"),
                ("sync_out", "Result Synchronization"),
                ("cleanup", "Workspace Cleanup"),
                ("ready", "Results Ready"),
                ("download", "Download Results"),
            ]:
                stages.append(stage(sid, label, "pending"))
            return stages

        allocated = bool(reservation and reservation.workstation_id)
        stages.append(
            stage(
                "waiting",
                "Waiting for Analysis Environment",
                "done" if allocated else ("active" if queued else "pending"),
            )
        )
        stages.append(
            stage(
                "allocated",
                "Analysis Environment Allocated",
                "done" if allocated else "pending",
                getattr(reservation, "updated_at", None) if allocated else None,
            )
        )

        sync_phase = str(getattr(workspace, "sync_phase", "") or "")
        input_ready = sync_phase in {
            "InputReady",
            "SessionStarting",
            "SessionActive",
            "CollectingOutput",
            "UploadVerified",
            "Completed",
        } or (session and session.status in ACTIVE_DESKTOP | {SessionStatus.READY, SessionStatus.TOKEN_GENERATED, SessionStatus.LAUNCHED})
        syncing = sync_phase in {"DownloadingInput", "Creating", "Syncing"} or (
            session and session.status == SessionStatus.PREPARING
        )
        stages.append(
            stage(
                "sync_in",
                "Input Data Synchronization",
                "done" if input_ready else ("active" if syncing else ("pending" if allocated else "pending")),
                getattr(workspace, "last_synced_at", None) if input_ready else None,
            )
        )

        started = bool(session and session.status in ACTIVE_DESKTOP)
        stages.append(
            stage(
                "started",
                "Analysis Session Started",
                "done" if started else ("active" if session and session.status in OPEN_SESSION else "pending"),
                getattr(session, "connected_at", None) or getattr(session, "launch_time", None),
            )
        )

        if started and remaining_seconds is not None:
            m, s = divmod(remaining_seconds, 60)
            stages.append(
                stage("remaining", "Remaining Session Time", "active", None, f"{m} min {s} sec remaining")
            )
        else:
            stages.append(stage("remaining", "Remaining Session Time", "pending"))

        completed = bool(
            session and session.status in {SessionStatus.TERMINATED, SessionStatus.COMPLETED, SessionStatus.EXPIRED}
        ) or output_stats["file_count"] > 0
        stages.append(
            stage(
                "completed",
                "Analysis Completed",
                "done" if completed else "pending",
                getattr(session, "disconnected_at", None),
            )
        )

        collecting = sync_phase in {"CollectingOutput", "UploadingOutput"}
        collected = sync_phase in {"UploadVerified", "Completed"} or output_stats["file_count"] > 0
        stages.append(
            stage(
                "sync_out",
                "Result Synchronization",
                "done" if collected else ("active" if collecting else "pending"),
            )
        )

        cleanup_done = self._cleanup_status(session, workspace) == "done"
        stages.append(
            stage(
                "cleanup",
                "Workspace Cleanup",
                "done" if cleanup_done else ("active" if completed and not cleanup_done else "pending"),
            )
        )
        stages.append(
            stage(
                "ready",
                "Results Ready",
                "done" if output_stats["file_count"] > 0 else "pending",
                detail=f"{output_stats['file_count']} file(s)" if output_stats["file_count"] else "",
            )
        )
        stages.append(
            stage(
                "download",
                "Download Results",
                "active" if output_stats["file_count"] > 0 else "pending",
            )
        )
        return stages

    def _sync_pipeline(self, **kw) -> list[dict[str, Any]]:
        raw = kw["raw_stats"]
        out = kw["output_stats"]
        session = kw["session"]
        workspace = kw["workspace"]
        sync_phase = str(kw.get("sync_phase") or "")
        progress = kw.get("sync_progress") or 0

        def row(sid, label, status, **extra):
            return {"id": sid, "label": label, "status": status, **extra}

        input_ready = sync_phase in {
            "InputReady",
            "SessionStarting",
            "SessionActive",
            "CollectingOutput",
            "UploadVerified",
            "Completed",
        }
        syncing = sync_phase in {"DownloadingInput", "Creating", "Syncing"} or (
            session and session.status == SessionStatus.PREPARING
        )
        running = bool(session and session.status in ACTIVE_DESKTOP)
        collected = sync_phase in {"UploadVerified", "Completed"} or out["file_count"] > 0

        return [
            row("raw_uploaded", "RAW files uploaded", "done" if raw["file_count"] else "pending", file_count=raw["file_count"], total_size_bytes=raw["total_size_bytes"]),
            row("sync_to_pc", "Synchronizing to Analysis Environment", "done" if input_ready else ("active" if syncing else "pending"), progress_percent=progress if syncing else (100 if input_ready else 0)),
            row("input_ready", "Input files ready", "done" if input_ready else "pending", logical_folder="Input Folder"),
            row("analysis_running", "Analysis running", "done" if (running or collected) else ("active" if running else "pending")),
            row("results_copied", "Results copied", "done" if out["file_count"] else ("active" if sync_phase == "CollectingOutput" else "pending"), logical_folder="Output Folder", file_count=out["file_count"], total_size_bytes=out["total_size_bytes"]),
            row("portal_sync", "Results synchronized to Portal", "done" if collected else "pending"),
            row("s3_sync", "Results synchronized to cloud storage", "done" if collected else "pending"),
            row("download_ready", "Ready for download", "done" if out["file_count"] else "pending"),
        ]

    def _desktop_prepare(self, *, session, workspace, sync_phase, sync_progress) -> list[dict[str, Any]]:
        sync_phase = str(sync_phase or "")
        preparing = bool(session and session.status == SessionStatus.PREPARING)
        failed = bool(session and session.status in {SessionStatus.FAILED, SessionStatus.TERMINATED, SessionStatus.EXPIRED})
        ready = bool(
            session
            and session.status
            in {
                SessionStatus.READY,
                SessionStatus.TOKEN_GENERATED,
                SessionStatus.LAUNCHED,
                *ACTIVE_DESKTOP,
            }
        )
        input_ready = sync_phase in {
            "InputReady",
            "SessionStarting",
            "SessionActive",
            "CollectingOutput",
            "UploadVerified",
            "Completed",
        } or ready
        allocated = bool(session or workspace)

        def step(sid, label, status):
            return {"id": sid, "label": label, "status": status}

        if failed:
            sync_status = "pending"
            launch_status = "pending"
            ready_status = "pending"
        else:
            sync_status = "done" if input_ready else ("active" if preparing or sync_phase == "DownloadingInput" else "pending")
            launch_status = (
                "done"
                if session and session.status in ACTIVE_DESKTOP | {SessionStatus.LAUNCHED, SessionStatus.TOKEN_GENERATED}
                else ("active" if ready else "pending")
            )
            ready_status = "done" if session and session.status in ACTIVE_DESKTOP else "pending"

        return [
            step("booking", "Booking confirmed", "done"),
            step("allocated", "Analysis Environment allocated", "done" if allocated else "pending"),
            step("sync_input", "Synchronizing input data", sync_status),
            step("launch_desktop", "Launching Analysis Environment", launch_status),
            step("ready", "Ready", ready_status),
        ]
