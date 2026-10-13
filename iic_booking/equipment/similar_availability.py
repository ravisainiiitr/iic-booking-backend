"""'Next available on similar instruments': the earliest open slot on equivalent equipment.

Equivalent means the same administrator-defined equipment group when the instrument has one, otherwise the same
category.

Shown on the booking page so a user facing a full week does not have to check every other XRD or SEM by hand.
Only slots inside each instrument's booking window for this viewer are considered; the booking page still applies
every booking rule when the user picks a slot.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

MIN_LEAD = timedelta(hours=1)
CANDIDATE_LIMIT = 12
RESULT_LIMIT = 4


def earliest_open_slot(equipment, user, *, now=None) -> dict[str, Any] | None:
    """Earliest AVAILABLE slot this viewer could pick inside the instrument's current booking window."""
    from iic_booking.users.models.user_type import UserType

    from .equipment_group_service import slot_window_bounds
    from .models import DailySlot, SlotStatus
    from .slot_department_access import filter_queryset_for_home_department

    now = now or timezone.now()
    is_admin = getattr(user, "user_type", None) == UserType.ADMIN or getattr(user, "is_superuser", False)
    window_start, window_end = slot_window_bounds(equipment, getattr(user, "user_type", None), is_admin)
    qs = DailySlot.objects.filter(
        slot_master__equipment=equipment,
        slot_master__is_active=True,
        status=SlotStatus.AVAILABLE,
        booking__isnull=True,
        start_datetime__gte=now + MIN_LEAD,
        date__gte=window_start,
        date__lte=window_end,
    ).order_by("start_datetime")
    if not is_admin:
        qs = filter_queryset_for_home_department(
            qs,
            user=user,
            equipment=equipment,
            is_admin=False,
            is_external=UserType.is_external_user(getattr(user, "user_type", None)),
        )
    time_from = getattr(equipment, "weekly_view_time_from", None)
    time_to = getattr(equipment, "weekly_view_time_to", None)
    for slot in qs[:40]:
        start, end = timezone.localtime(slot.start_datetime), timezone.localtime(slot.end_datetime)
        if time_from is not None and start.time() < time_from:
            continue
        if time_to is not None and end.time() > time_to and end.date() == start.date():
            continue
        return {
            "slot_id": slot.pk,
            "date": slot.date.isoformat(),
            "start": slot.start_datetime.isoformat(),
            "end": slot.end_datetime.isoformat(),
        }
    return None


def _family_ids(equipment) -> set[int]:
    """The instrument, its base instrument and every operating mode of either (they share hardware)."""
    from .models import Equipment

    base_id = equipment.parent_equipment_id or equipment.pk
    ids = set(
        Equipment.objects.filter(Q(pk=base_id) | Q(parent_equipment_id=base_id)).values_list("pk", flat=True)
    )
    ids.add(equipment.pk)
    return ids


def _row(eq, earliest) -> dict[str, Any]:
    return {
        "id": eq.equipment_id,
        "name": eq.name,
        "code": eq.code,
        "category": eq.category.name if eq.category_id else "",
        "department": eq.internal_department.name if eq.internal_department_id else "",
        "location": eq.location or "",
        "earliest": earliest,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def similar_availability(request, equipment_id: int):
    """GET: this instrument's earliest open slot and up to four similar instruments with sooner or any slots."""
    from .api_views import get_visible_equipment_queryset
    from .maintenance_policy import is_equipment_operational_status

    user = request.user
    visible = get_visible_equipment_queryset(user, catalog_scope="all").select_related(
        "category", "internal_department"
    )
    source = visible.filter(pk=equipment_id).first()
    if source is None:
        return Response({"error": "Equipment not found."}, status=404)
    now = timezone.now()
    own = earliest_open_slot(source, user, now=now) if is_equipment_operational_status(source.status) else None
    result: dict[str, Any] = {"equipment": _row(source, own), "alternatives": [], "basis": None}
    if source.equipment_group_id:
        peers, result["basis"] = visible.filter(equipment_group_id=source.equipment_group_id), "group"
    elif source.category_id:
        peers, result["basis"] = visible.filter(category_id=source.category_id), "category"
    else:
        return Response(result)

    candidates = (
        peers.exclude(pk__in=_family_ids(source))
        .order_by("name")
        .distinct()[:CANDIDATE_LIMIT]
    )
    rows = []
    for eq in candidates:
        if not is_equipment_operational_status(eq.status):
            continue
        earliest = earliest_open_slot(eq, user, now=now)
        if earliest:
            rows.append(_row(eq, earliest))
    rows.sort(key=lambda r: r["earliest"]["start"])
    result["alternatives"] = rows[:RESULT_LIMIT]
    return Response(result)
