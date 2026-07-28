"""Workstation health score engine (0–100)."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    CommandStatus,
    HEARTBEAT_OFFLINE_SECONDS,
    HEARTBEAT_STALE_SECONDS,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation, RemoteCommand, WorkstationHeartbeat


def calculate_health_score(workstation: AnalysisWorkstation) -> int:
    score = 100
    now = timezone.now()

    if not workstation.enabled or workstation.status in {"DISABLED", "ERROR"}:
        score -= 40

    if workstation.status == "MAINTENANCE":
        score -= 15

    if workstation.last_heartbeat is None:
        score -= 35
    else:
        age = (now - workstation.last_heartbeat).total_seconds()
        if age > HEARTBEAT_STALE_SECONDS:
            score -= 30
        elif age > HEARTBEAT_OFFLINE_SECONDS:
            score -= 15

    latest = (
        WorkstationHeartbeat.objects.filter(workstation=workstation)
        .order_by("-received_at")
        .first()
    )
    if latest:
        if latest.cpu >= 90:
            score -= 10
        if latest.memory >= 90:
            score -= 10
        if latest.disk >= 95:
            score -= 15

    if workstation.last_inventory_update is None:
        score -= 5
    else:
        inventory_age_hours = (now - workstation.last_inventory_update).total_seconds() / 3600
        if inventory_age_hours > 48:
            score -= 10
        elif inventory_age_hours > 24:
            score -= 5

    if not workstation.agent_version:
        score -= 5

    recent_failures = RemoteCommand.objects.filter(
        workstation=workstation,
        status=CommandStatus.FAILED,
        created_at__gte=now - timedelta(hours=24),
    ).count()
    score -= min(20, recent_failures * 5)

    return max(0, min(100, int(score)))


def update_workstation_health(workstation: AnalysisWorkstation) -> int:
    score = calculate_health_score(workstation)
    if workstation.health_score != score:
        workstation.health_score = score
        workstation.save(update_fields=["health_score", "updated_at"])
    return score
