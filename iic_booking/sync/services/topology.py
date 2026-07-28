"""Department topology discovery and caching (Milestone 14)."""

from __future__ import annotations

import uuid
from typing import Any

from django.utils import timezone

from iic_booking.sync.models import (
    AgentAssignment,
    Building,
    DepartmentSyncAgent,
    DepartmentTopology,
    EnterpriseAuditEvent,
    EquipmentSyncProfile,
    Laboratory,
)
from iic_booking.users.models import Department


class TopologyService:
    def list_departments(self, *, department_id=None) -> list[dict[str, Any]]:
        qs = Department.objects.all().order_by("name")
        if department_id:
            qs = qs.filter(pk=department_id)
        rows = []
        for dept in qs:
            agents = DepartmentSyncAgent.objects.filter(department=dept).exclude(status="DELETED")
            buildings = Building.objects.filter(department=dept, is_active=True)
            rows.append(
                {
                    "id": str(dept.id),
                    "name": dept.name,
                    "code": getattr(dept, "code", "") or "",
                    "building_count": buildings.count(),
                    "agent_count": agents.count(),
                    "laboratory_count": Laboratory.objects.filter(
                        department=dept, is_active=True
                    ).count(),
                }
            )
        return rows

    def list_buildings(self, *, department_id=None) -> list[dict[str, Any]]:
        qs = Building.objects.select_related("department").filter(is_active=True)
        if department_id:
            qs = qs.filter(department_id=department_id)
        return [
            {
                "id": str(b.id),
                "name": b.name,
                "code": b.code,
                "campus": b.campus,
                "address": b.address,
                "department_id": str(b.department_id),
                "department": b.department.name,
                "agent_count": b.sync_agents.exclude(status="DELETED").count(),
            }
            for b in qs.order_by("department__name", "name")
        ]

    def build_topology(
        self,
        department: Department,
        *,
        user_name: str = "",
        correlation_id: uuid.UUID | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        buildings = list(Building.objects.filter(department=department, is_active=True))
        labs = list(Laboratory.objects.filter(department=department, is_active=True))
        agents = list(
            DepartmentSyncAgent.objects.filter(department=department)
            .exclude(status="DELETED")
            .select_related("building", "equipment")
        )
        profile_count = EquipmentSyncProfile.objects.filter(
            primary_agent__department=department
        ).count()
        assignment_count = (
            AgentAssignment.objects.filter(
                is_active=True, sync_agent__department=department
            )
            .values("sync_profile_id")
            .distinct()
            .count()
        )
        equipment_count = max(profile_count, assignment_count)

        snapshot = {
            "department": {"id": str(department.id), "name": department.name},
            "buildings": [
                {
                    "id": str(b.id),
                    "name": b.name,
                    "code": b.code,
                    "campus": b.campus,
                    "agents": [
                        {"id": str(a.id), "name": a.agent_name, "status": a.status}
                        for a in agents
                        if a.building_id == b.id
                    ],
                }
                for b in buildings
            ],
            "laboratories": [
                {
                    "id": str(lab.id),
                    "name": lab.name,
                    "code": lab.code,
                    "location": lab.location,
                }
                for lab in labs
            ],
            "agents_unassigned_building": [
                {"id": str(a.id), "name": a.agent_name, "status": a.status}
                for a in agents
                if a.building_id is None
            ],
            "generated_at": timezone.now().isoformat(),
        }

        if persist:
            topo = DepartmentTopology.objects.create(
                department=department,
                version=(
                    DepartmentTopology.objects.filter(department=department).count() + 1
                ),
                snapshot=snapshot,
                building_count=len(buildings),
                agent_count=len(agents),
                equipment_count=equipment_count,
            )
            EnterpriseAuditEvent.objects.create(
                event_code="ENT-2001",
                message="Topology snapshot generated",
                department_id=str(department.id),
                correlation_id=correlation_id,
                user_name=user_name or "",
                details={"topology_id": str(topo.id)},
            )

        return {
            "snapshot": snapshot,
            "building_count": len(buildings),
            "agent_count": len(agents),
            "equipment_count": equipment_count,
        }
