"""Read-side selectors for Remote Analysis dashboards and lists."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Avg, Count, Q, QuerySet
from django.utils import timezone

from iic_booking.remote_analysis.constants import HEARTBEAT_OFFLINE_SECONDS, WorkstationStatus
from iic_booking.remote_analysis.models import (
    AnalysisWorkstation,
    InstalledSoftware,
    RemoteCommand,
    TelemetrySnapshot,
    WorkstationEvent,
    WorkstationHeartbeat,
)


def workstations_queryset(*, department_id: int | None = None) -> QuerySet[AnalysisWorkstation]:
    qs = AnalysisWorkstation.objects.select_related("department", "capabilities", "inventory")
    if department_id is not None:
        qs = qs.filter(Q(department_id=department_id) | Q(department_id__isnull=True))
    return qs


def workstation_by_id(workstation_id) -> AnalysisWorkstation | None:
    return (
        AnalysisWorkstation.objects.select_related("department", "capabilities", "inventory")
        .filter(pk=workstation_id)
        .first()
    )


def workstation_by_agent_id(agent_id: str) -> AnalysisWorkstation | None:
    return AnalysisWorkstation.objects.filter(agent_id=agent_id).first()


def recent_heartbeats(workstation: AnalysisWorkstation, *, limit: int = 50):
    return WorkstationHeartbeat.objects.filter(workstation=workstation).order_by("-received_at")[:limit]


def installed_software(workstation: AnalysisWorkstation | None = None, *, present_only: bool = True):
    qs = InstalledSoftware.objects.select_related("workstation")
    if workstation is not None:
        qs = qs.filter(workstation=workstation)
    if present_only:
        qs = qs.filter(is_present=True)
    return qs.order_by("software_name")


def recent_commands(*, workstation: AnalysisWorkstation | None = None, limit: int = 50):
    qs = RemoteCommand.objects.select_related("workstation", "created_by")
    if workstation is not None:
        qs = qs.filter(workstation=workstation)
    return qs.order_by("-created_at")[:limit]


def recent_events(*, workstation: AnalysisWorkstation | None = None, limit: int = 100):
    qs = WorkstationEvent.objects.select_related("workstation", "actor")
    if workstation is not None:
        qs = qs.filter(workstation=workstation)
    return qs.order_by("-created_at")[:limit]


def dashboard_metrics(*, department_id: int | None = None) -> dict:
    qs = workstations_queryset(department_id=department_id)
    total = qs.count()
    online_statuses = {
        WorkstationStatus.ONLINE,
        WorkstationStatus.AVAILABLE,
        WorkstationStatus.PREPARING,
        WorkstationStatus.BUSY,
        WorkstationStatus.RESERVED,
        WorkstationStatus.CLEANING,
    }
    online = qs.filter(status__in=online_statuses).count()
    offline = qs.filter(status=WorkstationStatus.OFFLINE).count()
    busy = qs.filter(
        status__in=[WorkstationStatus.BUSY, WorkstationStatus.PREPARING, WorkstationStatus.RESERVED]
    ).count()
    maintenance = qs.filter(status=WorkstationStatus.MAINTENANCE).count()
    calibration = qs.filter(status=WorkstationStatus.CALIBRATION).count()
    software_update = qs.filter(status=WorkstationStatus.SOFTWARE_UPDATE).count()
    faulty = qs.filter(
        status__in=[WorkstationStatus.HARDWARE_FAULT, WorkstationStatus.ERROR]
    ).count()
    available = qs.filter(
        status__in=[WorkstationStatus.AVAILABLE, WorkstationStatus.ONLINE]
    ).count()

    since = timezone.now() - timedelta(hours=1)
    hb = WorkstationHeartbeat.objects.filter(received_at__gte=since)
    if department_id is not None:
        hb = hb.filter(workstation__department_id=department_id)
    aggregates = hb.aggregate(avg_cpu=Avg("cpu"), avg_memory=Avg("memory"), avg_disk=Avg("disk"))

    alerts = (
        WorkstationEvent.objects.filter(success=False, created_at__gte=timezone.now() - timedelta(hours=24))
        .select_related("workstation")
        .order_by("-created_at")[:20]
    )
    if department_id is not None:
        alerts = alerts.filter(Q(workstation__department_id=department_id) | Q(workstation__isnull=True))

    last_heartbeats = (
        qs.exclude(last_heartbeat__isnull=True)
        .order_by("-last_heartbeat")
        .values("id", "hostname", "display_name", "status", "last_heartbeat", "health_score")[:20]
    )

    avg_health = qs.aggregate(avg=Avg("health_score"))["avg"] or 0

    fleet = {
        "total_analysis_pcs": total,
        "available": available,
        "busy": busy,
        "maintenance": maintenance,
        "calibration": calibration,
        "software_update": software_update,
        "offline": offline,
        "faulty": faulty,
    }

    return {
        "total_workstations": total,
        "online": online,
        "offline": offline,
        "busy": busy,
        "maintenance": maintenance,
        "calibration": calibration,
        "available": available,
        "faulty": faulty,
        "fleet": fleet,
        "average_cpu": round(aggregates["avg_cpu"] or 0, 2),
        "average_memory": round(aggregates["avg_memory"] or 0, 2),
        "average_disk": round(aggregates["avg_disk"] or 0, 2),
        "average_health_score": round(avg_health, 2),
        "recent_alerts": [
            {
                "id": str(a.id),
                "action": a.action,
                "details": a.details,
                "workstation": a.workstation.hostname if a.workstation else None,
                "created_at": a.created_at.isoformat(),
            }
            for a in alerts
        ],
        "last_heartbeats": list(last_heartbeats),
        "stale_threshold_seconds": HEARTBEAT_OFFLINE_SECONDS,
    }


def telemetry_for_workstation(workstation: AnalysisWorkstation, metric_name: str, *, limit: int = 100):
    return TelemetrySnapshot.objects.filter(
        workstation=workstation,
        metric_name=metric_name,
    ).order_by("-recorded_at")[:limit]
