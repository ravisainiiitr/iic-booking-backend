"""Enterprise agent assignment and scheduling (Milestone 14)."""

from __future__ import annotations

import uuid
from typing import Any

from django.db.models import Count, Q
from django.utils import timezone

from iic_booking.sync.models import (
    AgentAssignment,
    AgentLifecycleStatus,
    Building,
    DepartmentSyncAgent,
    EnterpriseAuditEvent,
    EquipmentSyncProfile,
    SyncAgentAssignment,
    SyncLogCategory,
)
from iic_booking.sync.services.logging import write_sync_log


class AssignmentService:
    """Automatic / manual assignment with extensible scheduling policies."""

    def assign(
        self,
        *,
        sync_agent: DepartmentSyncAgent,
        assignment_type: str = SyncAgentAssignment.AssignmentType.MANUAL,
        building_id=None,
        laboratory_id=None,
        equipment_id=None,
        group_id=None,
        priority: int = 100,
        notes: str = "",
        user_name: str = "",
        correlation_id: uuid.UUID | None = None,
        make_primary: bool = True,
    ) -> dict[str, Any]:
        if building_id:
            building = Building.objects.filter(
                pk=building_id, department_id=sync_agent.department_id
            ).first()
            if building is None:
                raise ValueError("Building not found in agent department.")
            sync_agent.building = building
            sync_agent.save(update_fields=["building", "updated_at"])

        record = SyncAgentAssignment.objects.create(
            sync_agent=sync_agent,
            department_id=sync_agent.department_id,
            building_id=building_id or sync_agent.building_id,
            laboratory_id=laboratory_id or sync_agent.laboratory_id,
            equipment_id=equipment_id,
            group_id=group_id,
            assignment_type=assignment_type,
            priority=priority,
            notes=notes,
            assigned_by=user_name or "",
            correlation_id=correlation_id,
            is_active=True,
        )

        if equipment_id and make_primary:
            profile = EquipmentSyncProfile.objects.filter(equipment_id=equipment_id).first()
            if profile is not None:
                # Soft-enforce department isolation via agent department.
                history = list(profile.ownership_history or [])
                history.append(
                    {
                        "at": timezone.now().isoformat(),
                        "primary_agent_id": str(sync_agent.id),
                        "by": user_name,
                        "assignment_id": str(record.id),
                    }
                )
                profile.primary_agent = sync_agent
                if building_id or sync_agent.building_id:
                    profile.building_id = building_id or sync_agent.building_id
                profile.ownership_history = history[-50:]
                profile.save(
                    update_fields=[
                        "primary_agent",
                        "building",
                        "ownership_history",
                        "updated_at",
                    ]
                )
                # Keep classic AgentAssignment in sync when possible.
                AgentAssignment.objects.filter(
                    sync_profile=profile, is_active=True
                ).exclude(sync_agent=sync_agent).update(
                    is_active=False, unassigned_at=timezone.now()
                )
                AgentAssignment.objects.update_or_create(
                    sync_agent=sync_agent,
                    sync_profile=profile,
                    defaults={"is_active": True, "unassigned_at": None, "notes": notes},
                )

        EnterpriseAuditEvent.objects.create(
            event_code="ENT-3001",
            message=f"Assignment {assignment_type}",
            department_id=str(sync_agent.department_id),
            building_id=str(record.building_id or ""),
            agent_id=str(sync_agent.id),
            correlation_id=correlation_id,
            user_name=user_name or "",
            details={"assignment_id": str(record.id), "equipment_id": equipment_id},
        )
        write_sync_log(
            event_code="ENT-3001",
            message=f"Assignment {assignment_type}",
            category=SyncLogCategory.ENTERPRISE,
            sync_agent=sync_agent,
            correlation_id=correlation_id,
            json_payload={"assignment_id": str(record.id)},
        )
        return {
            "id": str(record.id),
            "assignment_type": record.assignment_type,
            "agent_id": str(sync_agent.id),
            "building_id": str(record.building_id) if record.building_id else None,
            "equipment_id": equipment_id,
            "priority": record.priority,
        }

    def choose_agent(
        self,
        *,
        department_id,
        building_id=None,
        policy: str = "least_loaded",
        preferred_agent_id=None,
    ) -> DepartmentSyncAgent | None:
        """Extensible scheduling: preferred | least_loaded | department | building."""
        if preferred_agent_id and policy in {"preferred", "manual_override"}:
            agent = DepartmentSyncAgent.objects.filter(
                pk=preferred_agent_id,
                department_id=department_id,
                is_active=True,
            ).exclude(
                status__in=[
                    AgentLifecycleStatus.RETIRED,
                    AgentLifecycleStatus.DELETED,
                    AgentLifecycleStatus.DRAINING,
                    AgentLifecycleStatus.MAINTENANCE,
                    AgentLifecycleStatus.DISABLED,
                    AgentLifecycleStatus.REVOKED,
                ]
            ).first()
            if agent:
                return agent

        qs = DepartmentSyncAgent.objects.filter(
            department_id=department_id, is_active=True
        ).exclude(
            status__in=[
                AgentLifecycleStatus.RETIRED,
                AgentLifecycleStatus.DELETED,
                AgentLifecycleStatus.DRAINING,
                AgentLifecycleStatus.MAINTENANCE,
                AgentLifecycleStatus.DISABLED,
                AgentLifecycleStatus.REVOKED,
            ]
        )
        if building_id and policy in {"building", "least_loaded", "department"}:
            qs = qs.filter(Q(building_id=building_id) | Q(building_id__isnull=True))

        if policy == "least_loaded":
            return (
                qs.annotate(active_assignments=Count("assignments", filter=Q(assignments__is_active=True)))
                .order_by("active_assignments", "-processing_capacity", "agent_name")
                .first()
            )
        return qs.order_by("-processing_capacity", "agent_name").first()
