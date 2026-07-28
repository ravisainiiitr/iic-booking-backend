"""Enterprise agent registry (Milestone 14)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from iic_booking.sync.admin.constants import heartbeat_timeout_seconds
from iic_booking.sync.models import (
    AgentCapability,
    AgentLifecycleStatus,
    DepartmentSyncAgent,
    EnterpriseAuditEvent,
    SyncLogCategory,
)
from iic_booking.sync.services.logging import write_sync_log


def _is_online(last_heartbeat_at) -> bool:
    if last_heartbeat_at is None:
        return False
    cutoff = timezone.now() - timedelta(seconds=heartbeat_timeout_seconds())
    return last_heartbeat_at >= cutoff


class AgentRegistryService:
    """Registration, lifecycle, capability snapshots, department-scoped queries."""

    def scoped_agents(
        self,
        *,
        department_id=None,
        building_id=None,
        status: str | None = None,
        include_deleted: bool = False,
    ) -> QuerySet[DepartmentSyncAgent]:
        qs = DepartmentSyncAgent.objects.select_related(
            "department", "equipment", "building"
        ).all()
        if department_id:
            qs = qs.filter(department_id=department_id)
        if building_id:
            qs = qs.filter(building_id=building_id)
        if status:
            qs = qs.filter(status=status)
        if not include_deleted:
            qs = qs.exclude(status=AgentLifecycleStatus.DELETED)
        return qs

    def serialize_agent(self, agent: DepartmentSyncAgent) -> dict[str, Any]:
        return {
            "id": str(agent.id),
            "agent_uuid": str(agent.agent_uuid),
            "agent_name": agent.agent_name,
            "status": agent.status,
            "online": _is_online(agent.last_heartbeat_at),
            "department_id": str(agent.department_id) if agent.department_id else None,
            "department": agent.department.name if agent.department_id else "",
            "building_id": str(agent.building_id) if agent.building_id else None,
            "building": agent.building.name if agent.building_id else "",
            "equipment_id": agent.equipment_id,
            "equipment_code": agent.equipment.code if agent.equipment_id else "",
            "equipment_name": agent.equipment.name if agent.equipment_id else "",
            "machine_name": agent.machine_name,
            "operating_system": agent.operating_system,
            "version": agent.version,
            "update_channel": getattr(agent, "update_channel", None) or "PRODUCTION",
            "custom_tags": agent.custom_tags or [],
            "max_parallel_uploads": agent.max_parallel_uploads,
            "max_parallel_processing": agent.max_parallel_processing,
            "processing_capacity": agent.processing_capacity,
            "last_heartbeat_at": agent.last_heartbeat_at.isoformat()
            if agent.last_heartbeat_at
            else None,
            "device_id": str(agent.device_id) if agent.device_id else None,
            "security_version": agent.security_version,
        }

    def list_agents(self, **filters) -> list[dict[str, Any]]:
        return [self.serialize_agent(a) for a in self.scoped_agents(**filters)]

    def set_lifecycle(
        self,
        agent: DepartmentSyncAgent,
        *,
        new_status: str,
        user_name: str = "",
        correlation_id: uuid.UUID | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        previous = agent.status
        agent.status = new_status
        if new_status in {
            AgentLifecycleStatus.DISABLED,
            AgentLifecycleStatus.REVOKED,
            AgentLifecycleStatus.RETIRED,
            AgentLifecycleStatus.DELETED,
        }:
            agent.is_active = False
        elif new_status in {
            AgentLifecycleStatus.ACTIVE,
            AgentLifecycleStatus.ENROLLED,
            AgentLifecycleStatus.MAINTENANCE,
            AgentLifecycleStatus.DRAINING,
            AgentLifecycleStatus.RECOVERING,
        }:
            agent.is_active = True
        agent.save(update_fields=["status", "is_active", "updated_at"])

        EnterpriseAuditEvent.objects.create(
            event_code="ENT-1001",
            message=f"Lifecycle {previous} → {new_status}",
            department_id=str(agent.department_id or ""),
            building_id=str(agent.building_id or ""),
            agent_id=str(agent.id),
            correlation_id=correlation_id,
            user_name=user_name or "",
            details={"reason": reason, "from": previous, "to": new_status},
        )
        write_sync_log(
            event_code="ENT-1001",
            message=f"Lifecycle {previous} → {new_status}",
            category=SyncLogCategory.ENTERPRISE,
            sync_agent=agent,
            correlation_id=correlation_id,
            json_payload={"from": previous, "to": new_status, "reason": reason},
        )
        return self.serialize_agent(agent)

    def record_capabilities(
        self,
        agent: DepartmentSyncAgent,
        payload: dict[str, Any],
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        snap = AgentCapability.objects.create(
            sync_agent=agent,
            reported_at=timezone.now(),
            supported_plugins=payload.get("supported_plugins") or payload.get("plugin_inventory") or [],
            plugin_versions=payload.get("plugin_versions") or {},
            storage_free_bytes=payload.get("storage_free_bytes"),
            storage_total_bytes=payload.get("storage_total_bytes"),
            cpu_percent=payload.get("cpu_percent"),
            memory_percent=payload.get("memory_percent"),
            network_summary=str(payload.get("network_summary") or "")[:200],
            windows_version=str(payload.get("windows_version") or payload.get("windows_build") or "")[:100],
            schema_version=payload.get("schema_version"),
            recovery_version=payload.get("recovery_version"),
            security_version=payload.get("security_version") or agent.security_version,
            processing_capacity=payload.get("processing_capacity") or agent.processing_capacity,
            max_parallel_uploads=payload.get("max_parallel_uploads") or agent.max_parallel_uploads,
            max_parallel_processing=payload.get("max_parallel_processing")
            or agent.max_parallel_processing,
            capabilities=payload.get("capabilities") or {},
        )
        update_fields = []
        if payload.get("max_parallel_uploads"):
            agent.max_parallel_uploads = int(payload["max_parallel_uploads"])
            update_fields.append("max_parallel_uploads")
        if payload.get("max_parallel_processing"):
            agent.max_parallel_processing = int(payload["max_parallel_processing"])
            update_fields.append("max_parallel_processing")
        if payload.get("processing_capacity"):
            agent.processing_capacity = int(payload["processing_capacity"])
            update_fields.append("processing_capacity")
        if payload.get("custom_tags") is not None:
            agent.custom_tags = payload.get("custom_tags") or []
            update_fields.append("custom_tags")
        if update_fields:
            update_fields.append("updated_at")
            agent.save(update_fields=update_fields)

        EnterpriseAuditEvent.objects.create(
            event_code="ENT-1002",
            message="Capability snapshot recorded",
            department_id=str(agent.department_id or ""),
            building_id=str(agent.building_id or ""),
            agent_id=str(agent.id),
            correlation_id=correlation_id,
            details={"capability_id": snap.id},
        )
        return {"id": snap.id, "reported_at": snap.reported_at.isoformat()}
