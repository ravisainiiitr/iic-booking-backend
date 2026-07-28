"""Portal operational diagnostics APIs (production release candidate)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Count
from django.utils import timezone

from iic_booking.sync.models import (
    AgentHealthSnapshot,
    AgentHeartbeat,
    AlertEvent,
    DepartmentSyncAgent,
    HistoricalMetric,
    ReleasePackage,
    SyncLog,
    UpdateHistory,
)
from iic_booking.sync.services.agent_registry import AgentRegistryService
from iic_booking.sync.services.versioning import VersioningService


class PortalDiagnosticsService:
    def summary(self, *, department_id=None) -> dict[str, Any]:
        agents = AgentRegistryService().scoped_agents(department_id=department_id)
        agent_ids = list(agents.values_list("id", flat=True))
        since = timezone.now() - timedelta(hours=24)
        open_alerts_qs = AlertEvent.objects.filter(
            status__in=["NEW", "ACKNOWLEDGED", "SUPPRESSED"]
        )
        if agent_ids:
            open_alerts_qs = open_alerts_qs.filter(sync_agent_id__in=agent_ids)
        return {
            "agents": agents.count(),
            "heartbeats_24h": AgentHeartbeat.objects.filter(
                sync_agent_id__in=agent_ids,
                reported_at__gte=since,
            ).count()
            if agent_ids
            else 0,
            "health_snapshots": AgentHealthSnapshot.objects.filter(
                sync_agent_id__in=agent_ids
            ).count()
            if agent_ids
            else 0,
            "open_alerts": open_alerts_qs.count(),
            "update_history": UpdateHistory.objects.filter(sync_agent_id__in=agent_ids).count()
            if agent_ids
            else UpdateHistory.objects.count(),
            "sync_logs": SyncLog.objects.filter(sync_agent_id__in=agent_ids).count()
            if agent_ids
            else SyncLog.objects.count(),
            "published_releases": ReleasePackage.objects.filter(status="PUBLISHED").count(),
            "version_distribution": VersioningService().agent_versions(department_id=department_id),
            "generated_at": timezone.now().isoformat(),
        }

    def table_sizes(self, *, department_id=None) -> dict[str, Any]:
        return {
            "AgentHeartbeat": AgentHeartbeat.objects.count(),
            "AgentHealthSnapshot": AgentHealthSnapshot.objects.count(),
            "HistoricalMetric": HistoricalMetric.objects.count(),
            "SyncLog": SyncLog.objects.count(),
            "UpdateHistory": UpdateHistory.objects.count(),
            "DepartmentSyncAgent": DepartmentSyncAgent.objects.count(),
            "generated_at": timezone.now().isoformat(),
        }

    def top_event_codes(self, *, department_id=None, limit: int = 20) -> list[dict[str, Any]]:
        qs = SyncLog.objects.all()
        if department_id:
            qs = qs.filter(sync_agent__department_id=department_id)
        return list(
            qs.values("event_code")
            .annotate(c=Count("id"))
            .order_by("-c")[: max(1, min(limit, 100))]
        )
