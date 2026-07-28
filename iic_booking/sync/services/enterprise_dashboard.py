"""Enterprise dashboard aggregates (Milestone 14)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Avg
from django.utils import timezone

from iic_booking.sync.admin.constants import heartbeat_timeout_seconds
from iic_booking.sync.models import (
    AgentHeartbeat,
    AgentLifecycleStatus,
    AgentStatistics,
    AgentUploadSession,
    AgentUploadSessionStatus,
    Building,
    DepartmentSyncAgent,
    ResultProcessingQueue,
    ResultProcessingStatus,
    SyncAgentAssignment,
)
from iic_booking.sync.services.agent_registry import AgentRegistryService
from iic_booking.sync.services.topology import TopologyService
from iic_booking.users.models import Department


class EnterpriseDashboardService:
    def summary(self, *, department_id=None) -> dict[str, Any]:
        agents = AgentRegistryService().scoped_agents(department_id=department_id)
        cutoff = timezone.now() - timedelta(seconds=heartbeat_timeout_seconds())
        online = agents.filter(last_heartbeat_at__gte=cutoff).count()
        total = agents.count()
        maintenance = agents.filter(status=AgentLifecycleStatus.MAINTENANCE).count()
        draining = agents.filter(status=AgentLifecycleStatus.DRAINING).count()
        buildings = Building.objects.filter(is_active=True)
        if department_id:
            buildings = buildings.filter(department_id=department_id)

        agent_ids = list(agents.values_list("id", flat=True))
        pending_uploads = AgentUploadSession.objects.filter(
            sync_agent_id__in=agent_ids,
            status__in=[
                AgentUploadSessionStatus.PENDING,
                AgentUploadSessionStatus.RECEIVING,
            ],
        ).count()
        pending_processing = ResultProcessingQueue.objects.filter(
            sync_agent_id__in=agent_ids,
            status__in=[
                ResultProcessingStatus.PENDING,
                ResultProcessingStatus.VALIDATING,
                ResultProcessingStatus.PARSING,
                ResultProcessingStatus.IMPORTING,
            ],
        ).count()

        hb_stats = AgentHeartbeat.objects.filter(
            sync_agent_id__in=agent_ids,
            reported_at__gte=timezone.now() - timedelta(hours=1),
        ).aggregate(
            avg_cpu=Avg("cpu_percent"),
            avg_mem=Avg("memory_percent"),
            avg_disk=Avg("disk_percent"),
        )

        departments = TopologyService().list_departments(department_id=department_id)
        return {
            "departments": departments,
            "building_count": buildings.count(),
            "agents": {
                "total": total,
                "online": online,
                "offline": max(0, total - online),
                "maintenance": maintenance,
                "draining": draining,
            },
            "queues": {
                "uploads_pending": pending_uploads,
                "processing_pending": pending_processing,
            },
            "health": {
                "avg_cpu_percent": hb_stats.get("avg_cpu"),
                "avg_memory_percent": hb_stats.get("avg_mem"),
                "avg_disk_percent": hb_stats.get("avg_disk"),
            },
            "assignments_active": SyncAgentAssignment.objects.filter(
                is_active=True,
                **({"department_id": department_id} if department_id else {}),
            ).count(),
            "generated_at": timezone.now().isoformat(),
        }

    def capture_statistics(self, *, department_id=None) -> dict[str, Any]:
        summary = self.summary(department_id=department_id)
        now = timezone.now()
        dept = None
        if department_id:
            dept = Department.objects.filter(pk=department_id).first()
        AgentStatistics.objects.create(
            department=dept,
            period_start=now - timedelta(hours=1),
            period_end=now,
            metrics=summary,
        )
        return summary
