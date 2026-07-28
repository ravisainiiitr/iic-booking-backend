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

        agent_reported = (heartbeat.current_state or "").upper()
        if workstation.enabled and workstation.status not in {
            WorkstationStatus.MAINTENANCE,
            WorkstationStatus.DISABLED,
            WorkstationStatus.PREPARING,
            WorkstationStatus.BUSY,
            WorkstationStatus.CLEANING,
        }:
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

        workstation.save(
            update_fields=[
                "last_heartbeat",
                "current_command",
                "status",
                "updated_at",
            ]
        )

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
    """Detect missed heartbeats and mark workstations offline."""
    cutoff = timezone.now() - timedelta(seconds=HEARTBEAT_OFFLINE_SECONDS)
    qs = AnalysisWorkstation.objects.filter(
        enabled=True,
        last_heartbeat__lt=cutoff,
    ).exclude(
        status__in=[
            WorkstationStatus.OFFLINE,
            WorkstationStatus.DISABLED,
            WorkstationStatus.MAINTENANCE,
        ]
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


def _optional_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
