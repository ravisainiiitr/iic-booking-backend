"""Dashboard metrics for Department Sync Operations."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Prefetch, Q
from django.utils import timezone

from iic_booking.sync.models import (
    AgentAssignment,
    AgentHeartbeat,
    AgentLifecycleStatus,
    DepartmentSyncAgent,
    EquipmentSyncProfile,
    SyncLog,
    SyncLogSeverity,
)

from .constants import heartbeat_timeout_seconds
from .validation import collect_system_validation_issues


def build_operations_dashboard_context(*, limit_recent: int = 12) -> dict:
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(seconds=heartbeat_timeout_seconds())

    agents = DepartmentSyncAgent.objects.all()
    registered = agents.filter(status=AgentLifecycleStatus.REGISTERED).count()
    enrolled = agents.filter(status=AgentLifecycleStatus.ENROLLED).count()
    disabled = agents.filter(status=AgentLifecycleStatus.DISABLED).count()
    reporting = agents.filter(last_heartbeat_at__gte=cutoff).count()
    not_reporting = agents.exclude(last_heartbeat_at__gte=cutoff).count()

    profiles = EquipmentSyncProfile.objects.all()
    sync_enabled = profiles.filter(sync_enabled=True).count()
    without_agent = (
        profiles.filter(sync_enabled=True)
        .annotate(active_assignments=Count("assignments", filter=Q(assignments__is_active=True)))
        .filter(active_assignments=0)
        .count()
    )

    config_mismatch = 0
    for agent in (
        DepartmentSyncAgent.objects.filter(status=AgentLifecycleStatus.ENROLLED)
        .prefetch_related(
            Prefetch(
                "assignments",
                queryset=AgentAssignment.objects.filter(is_active=True).select_related("sync_profile"),
            )
        )[:500]
    ):
        if agent.last_reported_configuration_version is None:
            continue
        for assignment in agent.assignments.all():
            if assignment.sync_profile.configuration_version > agent.last_reported_configuration_version:
                config_mismatch += 1
                break

    heartbeat_alerts = not_reporting
    logs_today = SyncLog.objects.filter(created_at__gte=today_start)
    uploads_today = logs_today.filter(event_code__startswith="UPLOAD-").count()
    errors_today = logs_today.filter(severity=SyncLogSeverity.ERROR).count()
    warnings_today = logs_today.filter(severity=SyncLogSeverity.WARNING).count()
    critical_today = logs_today.filter(severity=SyncLogSeverity.CRITICAL).count()

    recent_activity = list(
        SyncLog.objects.select_related("sync_agent", "equipment").order_by("-created_at")[:limit_recent]
    )
    recent_heartbeats = list(
        AgentHeartbeat.objects.select_related("sync_agent").order_by("-reported_at")[:limit_recent]
    )
    recent_errors = list(
        SyncLog.objects.filter(severity__in=[SyncLogSeverity.ERROR, SyncLogSeverity.CRITICAL])
        .select_related("sync_agent", "equipment")
        .order_by("-created_at")[:limit_recent]
    )
    recent_registrations = list(
        DepartmentSyncAgent.objects.select_related("department", "equipment")
        .order_by("-registered_at")[:limit_recent]
    )

    validation_issues = collect_system_validation_issues(limit=40)

    return {
        "cards": {
            "registered_agents": registered,
            "enrolled_agents": enrolled,
            "disabled_agents": disabled,
            "agents_reporting": reporting,
            "agents_not_reporting": not_reporting,
            "equipment_sync_enabled": sync_enabled,
            "equipment_without_agent": without_agent,
            "configuration_mismatch": config_mismatch,
            "heartbeat_alerts": heartbeat_alerts,
            "uploads_today": uploads_today,
            "errors_today": errors_today,
            "warning_events": warnings_today,
            "critical_events": critical_today,
        },
        "recent_activity": recent_activity,
        "recent_heartbeats": recent_heartbeats,
        "recent_errors": recent_errors,
        "recent_registrations": recent_registrations,
        "validation_issues": validation_issues,
        "heartbeat_timeout_seconds": heartbeat_timeout_seconds(),
        "generated_at": now,
    }
