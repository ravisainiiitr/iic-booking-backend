"""Capacity monitoring (Milestone 15)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Avg, Max, Sum
from django.utils import timezone

from iic_booking.sync.models import (
    AgentUploadSession,
    AgentUploadSessionStatus,
    EquipmentSyncProfile,
    ResultProcessingQueue,
    SystemCapacitySnapshot,
)
from iic_booking.sync.services.agent_registry import AgentRegistryService
from iic_booking.users.models import Department


class CapacityService:
    def capture(self, *, department_id=None, sync_agent=None, metrics: dict | None = None) -> dict[str, Any]:
        agents = AgentRegistryService().scoped_agents(department_id=department_id)
        if sync_agent is not None:
            agents = agents.filter(pk=sync_agent.pk)
        agent_ids = list(agents.values_list("id", flat=True))
        dept = None
        if department_id:
            dept = Department.objects.filter(pk=department_id).first()
        elif sync_agent is not None:
            dept = getattr(sync_agent, "department", None)

        equipment_qs = EquipmentSyncProfile.objects.all()
        if dept is not None:
            equipment_qs = equipment_qs.filter(equipment__internal_department_id=dept.id)
        elif department_id:
            equipment_qs = equipment_qs.filter(equipment__internal_department_id=department_id)
        equipment_count = equipment_qs.count()

        uploads_done = AgentUploadSession.objects.filter(
            sync_agent_id__in=agent_ids,
            status=AgentUploadSessionStatus.COMPLETED,
            updated_at__gte=timezone.now() - timedelta(hours=24),
        ).count()
        processing_done = ResultProcessingQueue.objects.filter(
            sync_agent_id__in=agent_ids,
            updated_at__gte=timezone.now() - timedelta(hours=24),
        ).count()

        m = metrics or {}
        snap = SystemCapacitySnapshot.objects.create(
            department=dept,
            building=getattr(sync_agent, "building", None) if sync_agent else None,
            sync_agent=sync_agent,
            reported_at=timezone.now(),
            storage_used_bytes=m.get("storage_used_bytes"),
            database_size_bytes=m.get("database_size_bytes") or m.get("sqlite_size_bytes"),
            upload_volume=int(m.get("upload_volume") or uploads_done),
            processing_volume=int(m.get("processing_volume") or processing_done),
            plugin_count=int(m.get("plugin_count") or 0),
            equipment_count=int(m.get("equipment_count") or equipment_count),
            agent_count=agents.count(),
            average_upload_size_bytes=m.get("average_upload_size_bytes"),
            peak_processing=int(m.get("peak_processing") or 0),
            peak_queue=int(m.get("peak_queue") or 0),
            metrics=m,
        )
        return self._serialize(snap)

    def trends(self, *, department_id=None, days: int = 30, limit: int = 100) -> list[dict[str, Any]]:
        qs = SystemCapacitySnapshot.objects.filter(
            reported_at__gte=timezone.now() - timedelta(days=max(1, days))
        ).select_related("department", "sync_agent", "building")
        if department_id:
            qs = qs.filter(department_id=department_id)
        return [self._serialize(s) for s in qs.order_by("-reported_at")[: max(1, min(limit, 500))]]

    def summary(self, *, department_id=None) -> dict[str, Any]:
        qs = SystemCapacitySnapshot.objects.all()
        if department_id:
            qs = qs.filter(department_id=department_id)
        recent = qs.filter(reported_at__gte=timezone.now() - timedelta(days=7))
        agg = recent.aggregate(
            avg_storage=Avg("storage_used_bytes"),
            avg_db=Avg("database_size_bytes"),
            sum_uploads=Sum("upload_volume"),
            sum_processing=Sum("processing_volume"),
            peak_queue=Max("peak_queue"),
            peak_processing=Max("peak_processing"),
            max_plugins=Max("plugin_count"),
            max_equipment=Max("equipment_count"),
        )
        latest = qs.order_by("-reported_at").first()
        return {
            "latest": self._serialize(latest) if latest else None,
            "last_7_days": agg,
            "generated_at": timezone.now().isoformat(),
        }

    @staticmethod
    def _serialize(snap: SystemCapacitySnapshot | None) -> dict[str, Any] | None:
        if snap is None:
            return None
        return {
            "id": snap.id,
            "department_id": str(snap.department_id) if snap.department_id else None,
            "building_id": str(snap.building_id) if snap.building_id else None,
            "agent_id": str(snap.sync_agent_id) if snap.sync_agent_id else None,
            "reported_at": snap.reported_at.isoformat() if snap.reported_at else None,
            "storage_used_bytes": snap.storage_used_bytes,
            "database_size_bytes": snap.database_size_bytes,
            "upload_volume": snap.upload_volume,
            "processing_volume": snap.processing_volume,
            "plugin_count": snap.plugin_count,
            "equipment_count": snap.equipment_count,
            "agent_count": snap.agent_count,
            "average_upload_size_bytes": snap.average_upload_size_bytes,
            "peak_processing": snap.peak_processing,
            "peak_queue": snap.peak_queue,
            "metrics": snap.metrics or {},
        }
