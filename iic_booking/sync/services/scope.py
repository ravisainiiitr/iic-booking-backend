"""Helpers for scoping data to an authenticated Department Sync Agent."""

from __future__ import annotations

from django.db.models import Prefetch, QuerySet

from iic_booking.equipment.models import Booking, Equipment
from iic_booking.sync.models import AgentAssignment, DepartmentSyncAgent, EquipmentSyncProfile


def assigned_profile_queryset(agent: DepartmentSyncAgent) -> QuerySet[EquipmentSyncProfile]:
    return (
        EquipmentSyncProfile.objects.filter(
            assignments__sync_agent=agent,
            assignments__is_active=True,
        )
        .select_related(
            "equipment",
            "equipment__internal_department",
        )
        .prefetch_related(
            Prefetch(
                "assignments",
                queryset=AgentAssignment.objects.filter(
                    sync_agent=agent,
                    is_active=True,
                ).select_related("sync_agent", "sync_agent__equipment"),
            )
        )
        .distinct()
    )


def assigned_equipment_ids(agent: DepartmentSyncAgent) -> list[int]:
    return list(
        AgentAssignment.objects.filter(sync_agent=agent, is_active=True).values_list(
            "sync_profile__equipment_id",
            flat=True,
        )
    )


def assigned_equipment_queryset(agent: DepartmentSyncAgent) -> QuerySet[Equipment]:
    ids = assigned_equipment_ids(agent)
    return Equipment.objects.filter(equipment_id__in=ids).select_related("internal_department")


def agent_may_access_equipment(agent: DepartmentSyncAgent, equipment_id: int) -> bool:
    return AgentAssignment.objects.filter(
        sync_agent=agent,
        is_active=True,
        sync_profile__equipment_id=equipment_id,
    ).exists()


def agent_may_access_booking(agent: DepartmentSyncAgent, booking: Booking) -> bool:
    return agent_may_access_equipment(agent, booking.equipment_id)


def bookings_for_agent(agent: DepartmentSyncAgent) -> QuerySet[Booking]:
    ids = assigned_equipment_ids(agent)
    return (
        Booking.objects.filter(equipment_id__in=ids)
        .select_related(
            "equipment",
            "equipment__internal_department",
            "user",
            "user__department",
        )
        .prefetch_related("daily_slots", "sample_trace_events")
    )
