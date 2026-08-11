"""Heartbeat processing, offline detection, and alert signals."""

from __future__ import annotations

from typing import Any

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    AuditCategory,
    DISK_FULL_THRESHOLD,
    HEARTBEAT_OFFLINE_SECONDS,
    HIGH_CPU_THRESHOLD,
    LOW_MEMORY_THRESHOLD,
    WorkstationStatus,
)
from iic_booking.remote_analysis.models import (
    AnalysisWorkstation,
    TelemetrySnapshot,
    WorkstationHeartbeat,
    WorkstationStateHistory,
)
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.services.health import update_workstation_health


def _maybe_alert(workstation: AnalysisWorkstation, heartbeat: WorkstationHeartbeat) -> list[str]:
    alerts: list[str] = []
    if heartbeat.cpu >= HIGH_CPU_THRESHOLD:
        alerts.append(f"High CPU ({heartbeat.cpu}%)")
    if heartbeat.memory >= LOW_MEMORY_THRESHOLD:
        alerts.append(f"Low memory (used {heartbeat.memory}%)")
    if heartbeat.disk >= DISK_FULL_THRESHOLD:
        alerts.append(f"Disk full ({heartbeat.disk}%)")
    for alert in alerts:
        record_event(
            category=AuditCategory.HEARTBEAT,
            action="Alert",
            details=alert,
            success=False,
            workstation=workstation,
        )
    return alerts


class HeartbeatService:
    @transaction.atomic
    def process(self, workstation: AnalysisWorkstation, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("heartbeat") or payload
        now = timezone.now()

        heartbeat = WorkstationHeartbeat.objects.create(
            workstation=workstation,
            cpu=float(data.get("CPU") or data.get("cpu") or 0),
            memory=float(data.get("Memory") or data.get("memory") or 0),
            disk=float(data.get("Disk") or data.get("disk") or 0),
            gpu=_optional_float(data.get("GPU") if "GPU" in data else data.get("gpu")),
            windows_uptime_hours=float(data.get("WindowsUptimeHours") or data.get("windowsUptimeHours") or 0),
            idle=bool(data.get("Idle") if "Idle" in data else data.get("idle", False)),
            idle_time_minutes=float(data.get("IdleTimeMinutes") or data.get("idleTimeMinutes") or 0),
            logged_in_user=str(data.get("LoggedInUser") or data.get("loggedInUser") or ""),
            running_software=str(
                data.get("RunningLicensedSoftware")
                or data.get("runningLicensedSoftware")
                or data.get("runningSoftware")
                or ""
            ),
            running_processes=int(data.get("RunningProcesses") or data.get("runningProcesses") or 0),
            software_count=int(data.get("SoftwareCount") or data.get("softwareCount") or 0),
            portal_latency_ms=_optional_float(data.get("portalLatencyMs") or data.get("PortalLatency")),
            current_state=str(data.get("CurrentStatus") or data.get("currentStatus") or data.get("current_state") or ""),
            network=bool(data.get("Network") if "Network" in data else data.get("network", True)),
            online=bool(data.get("Online") if "Online" in data else data.get("online", True)),
            antivirus_status=str(data.get("AntivirusStatus") or data.get("antivirusStatus") or ""),
            windows_updates_pending=int(
                data.get("WindowsUpdatesPending") or data.get("windowsUpdatesPending") or 0
            ),
            raw_payload=data if isinstance(data, dict) else {},
        )

        workstation.last_heartbeat = now
        workstation.current_command = str(data.get("CurrentCommand") or data.get("currentCommand") or "")

        # R9 safe path / cleanup metadata (local FS only — no secrets)
        update_fields = [
            "last_heartbeat",
            "current_command",
            "status",
            "updated_at",
        ]
        if "dataRoot" in data or "data_root" in data:
            workstation.data_root = str(data.get("dataRoot") or data.get("data_root") or "")[:1024]
            update_fields.append("data_root")
        if "inputPath" in data or "input_path" in data:
            workstation.input_path = str(data.get("inputPath") or data.get("input_path") or "")[:1024]
            update_fields.append("input_path")
        if "outputPath" in data or "output_path" in data:
            workstation.output_path = str(data.get("outputPath") or data.get("output_path") or "")[:1024]
            update_fields.append("output_path")
        if "workspaceDiskFreeBytes" in data or "workspace_disk_free_bytes" in data:
            workstation.workspace_disk_free_bytes = _optional_int(
                data.get("workspaceDiskFreeBytes") or data.get("workspace_disk_free_bytes")
            )
            update_fields.append("workspace_disk_free_bytes")
        if "inputBytes" in data or "input_bytes" in data:
            workstation.input_bytes = _optional_int(data.get("inputBytes") or data.get("input_bytes"))
            update_fields.append("input_bytes")
        if "outputBytes" in data or "output_bytes" in data:
            workstation.output_bytes = _optional_int(data.get("outputBytes") or data.get("output_bytes"))
            update_fields.append("output_bytes")
        if "cleanupStatus" in data or "cleanup_status" in data:
            workstation.cleanup_status = str(
                data.get("cleanupStatus") or data.get("cleanup_status") or "idle"
            )[:32]
            update_fields.append("cleanup_status")
        if "lastSyncAt" in data or "last_sync_at" in data:
            raw_sync = data.get("lastSyncAt") or data.get("last_sync_at")
            workstation.last_sync_at = _optional_dt(raw_sync)
            update_fields.append("last_sync_at")
        if "diskLow" in data or "disk_low" in data:
            workstation.disk_low = bool(data.get("diskLow") if "diskLow" in data else data.get("disk_low"))
            update_fields.append("disk_low")

        agent_reported = (heartbeat.current_state or "").upper()
        # Sticky operational statuses must not block recovery when the agent is idle again.
        # Without this, CLEAN_WORKSTATION → AVAILABLE on the agent never clears portal BUSY,
        # and the workstation deadlocks for allocation.
        sticky_operational = {
            WorkstationStatus.PREPARING,
            WorkstationStatus.BUSY,
            WorkstationStatus.RESERVED,
            WorkstationStatus.CLEANING,
        }
        protected = {
            WorkstationStatus.MAINTENANCE,
            WorkstationStatus.CALIBRATION,
            WorkstationStatus.SOFTWARE_UPDATE,
            WorkstationStatus.HARDWARE_FAULT,
            WorkstationStatus.DISABLED,
        }
        idle_reported = agent_reported in {
            WorkstationStatus.AVAILABLE,
            WorkstationStatus.ONLINE,
        }
        if (
            workstation.enabled
            and workstation.status in sticky_operational
            and idle_reported
            and not _workstation_has_active_hold(workstation)
        ):
            WorkstationStateHistory.objects.create(
                workstation=workstation,
                from_status=workstation.status,
                to_status=agent_reported,
                reason="Agent heartbeat cleared sticky status (no active hold)",
            )
            workstation.status = agent_reported
        elif workstation.enabled and workstation.status not in (protected | sticky_operational):
            if agent_reported in {s.value for s in WorkstationStatus}:
                if workstation.status != agent_reported:
                    WorkstationStateHistory.objects.create(
                        workstation=workstation,
                        from_status=workstation.status,
                        to_status=agent_reported,
                        reason="Agent heartbeat status",
                    )
                    workstation.status = agent_reported
            elif workstation.status in {WorkstationStatus.OFFLINE, WorkstationStatus.UNKNOWN, WorkstationStatus.ERROR}:
                WorkstationStateHistory.objects.create(
                    workstation=workstation,
                    from_status=workstation.status,
                    to_status=WorkstationStatus.ONLINE,
                    reason="Heartbeat received",
                )
                workstation.status = WorkstationStatus.ONLINE

        # Keep ERROR if cleanup failed so allocation stays blocked.
        if (getattr(workstation, "cleanup_status", "") or "").lower() == "failed":
            if workstation.status not in {
                WorkstationStatus.BUSY,
                WorkstationStatus.PREPARING,
                WorkstationStatus.CLEANING,
                WorkstationStatus.MAINTENANCE,
                WorkstationStatus.DISABLED,
                WorkstationStatus.RESERVED,
            }:
                workstation.status = WorkstationStatus.ERROR

        workstation.save(update_fields=list(dict.fromkeys(update_fields)))

        for metric, value, unit in (
            ("cpu", heartbeat.cpu, "%"),
            ("memory", heartbeat.memory, "%"),
            ("disk", heartbeat.disk, "%"),
        ):
            TelemetrySnapshot.objects.create(
                workstation=workstation,
                metric_name=metric,
                value=value,
                unit=unit,
            )

        alerts = _maybe_alert(workstation, heartbeat)
        health = update_workstation_health(workstation)

        return {
            "accepted": True,
            "heartbeat_id": heartbeat.id,
            "health_score": health,
            "alerts": alerts,
            "status": workstation.status,
        }


def mark_stale_workstations_offline() -> int:
    """Detect missed heartbeats and mark workstations offline.

    Workstations with an in-flight reservation are left alone so a brief agent
    pause cannot yank an already-allocated session back into the queue.
    """
    from django.db.models import Q

    from iic_booking.remote_analysis.constants import ReservationStatus
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation

    cutoff = timezone.now() - timedelta(seconds=HEARTBEAT_OFFLINE_SECONDS)
    protected_ids = set(
        AnalysisReservation.objects.filter(
            status__in=[
                ReservationStatus.RESERVED,
                ReservationStatus.AWAITING_CHECKIN,
                ReservationStatus.PREPARING,
                ReservationStatus.READY,
                ReservationStatus.ACTIVE,
            ],
            workstation_id__isnull=False,
        ).values_list("workstation_id", flat=True)
    )
    qs = (
        AnalysisWorkstation.objects.filter(enabled=True)
        .filter(Q(last_heartbeat__lt=cutoff) | Q(last_heartbeat__isnull=True))
        .exclude(status=WorkstationStatus.REGISTERING)
        .exclude(
            status__in=[
                WorkstationStatus.OFFLINE,
                WorkstationStatus.DISABLED,
                WorkstationStatus.MAINTENANCE,
                WorkstationStatus.CALIBRATION,
                WorkstationStatus.SOFTWARE_UPDATE,
                WorkstationStatus.HARDWARE_FAULT,
            ]
        )
        .exclude(id__in=protected_ids)
    )
    count = 0
    for ws in qs:
        WorkstationStateHistory.objects.create(
            workstation=ws,
            from_status=ws.status,
            to_status=WorkstationStatus.OFFLINE,
            reason="Missed heartbeats",
        )
        ws.status = WorkstationStatus.OFFLINE
        ws.save(update_fields=["status", "updated_at"])
        update_workstation_health(ws)
        record_event(
            category=AuditCategory.STATUS,
            action="Offline",
            details="Missed heartbeats",
            success=False,
            workstation=ws,
        )
        count += 1
    return count


def _workstation_has_active_hold(workstation: AnalysisWorkstation) -> bool:
    """True when a live reservation or desktop session still owns this workstation."""
    from iic_booking.remote_analysis.constants import ReservationStatus, SessionStatus
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
    from iic_booking.remote_analysis.session_models import RemoteDesktopSession

    if AnalysisReservation.objects.filter(
        workstation=workstation,
        status__in=[
            ReservationStatus.RESERVED,
            ReservationStatus.AWAITING_CHECKIN,
            ReservationStatus.PREPARING,
            ReservationStatus.READY,
            ReservationStatus.ACTIVE,
        ],
    ).exists():
        return True
    return RemoteDesktopSession.objects.filter(
        workstation=workstation,
        status__in=[
            SessionStatus.CREATED,
            SessionStatus.PREPARING,
            SessionStatus.READY,
            SessionStatus.TOKEN_GENERATED,
            SessionStatus.LAUNCHED,
            SessionStatus.CONNECTING,
            SessionStatus.CONNECTED,
            SessionStatus.ACTIVE,
            SessionStatus.IDLE,
            SessionStatus.DISCONNECTING,
        ],
    ).exists()


def _optional_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_dt(value):
    if value is None or value == "":
        return None
    if hasattr(value, "isoformat"):
        return value
    try:
        from django.utils.dateparse import parse_datetime

        return parse_datetime(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
