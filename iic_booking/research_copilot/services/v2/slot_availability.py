"""
Bookable-slot lookup with the same rules as the portal booking page.

The portal weekly grid (`equipment_daily_slots`) owns visibility, slot generation, the internal /
external slot windows, weekly time filters and multi-mode overlays. Copilot calls that view as the
requesting user and then applies the home-department filter used by the booking endpoint, so the
slots it offers are exactly the ones the user could pick on the booking page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

MAX_WINDOW_DAYS = 14


@dataclass
class SlotLookup:
    ok: bool
    equipment_id: int | None = None
    equipment_name: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    message: str = ""
    equipment_status: str | None = None
    bookable_equipment: bool = True
    window_start: str | None = None
    window_end: str | None = None
    slot_window_min_date: str | None = None
    slot_window_max_date: str | None = None


def _call_daily_slots_view(*, user, equipment_id: int, start: date, end: date):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from iic_booking.equipment.api_views import equipment_daily_slots

    factory = APIRequestFactory()
    request = factory.get(
        f"/api/equipments/{equipment_id}/daily-slots/",
        {"start_date": start.isoformat(), "end_date": end.isoformat()},
    )
    if user is not None and getattr(user, "is_authenticated", False):
        force_authenticate(request, user=user)
    response = equipment_daily_slots(request, equipment_id)
    data = getattr(response, "data", None)
    return int(getattr(response, "status_code", 500) or 500), (data if isinstance(data, dict) else {})


def _home_department_allowed_ids(*, user, equipment, slot_ids: list[int]) -> set[int]:
    """Same queryset filter `_book_equipment_impl` applies to non-admin slot_ids bookings."""
    from iic_booking.equipment.models import DailySlot, SlotStatus
    from iic_booking.equipment.slot_department_access import filter_queryset_for_home_department
    from iic_booking.users.models import UserType

    if not slot_ids:
        return set()
    qs = DailySlot.objects.filter(
        id__in=slot_ids,
        slot_master__equipment=equipment,
        status=SlotStatus.AVAILABLE,
        booking__isnull=True,
    )
    if user is None or not getattr(user, "is_authenticated", False):
        return set(qs.values_list("id", flat=True))
    user_type = getattr(user, "user_type", None)
    if user_type in UserType.get_admin_panel_codes():
        return set(qs.values_list("id", flat=True))
    qs = filter_queryset_for_home_department(
        qs,
        user=user,
        equipment=equipment,
        is_admin=False,
        is_external=bool(user_type and UserType.is_external_user(user_type)),
    )
    return set(qs.values_list("id", flat=True))


def find_bookable_slots(
    *,
    user,
    equipment_id: int,
    start_date: date,
    end_date: date,
    after_time: time | None = None,
    limit: int = 80,
) -> SlotLookup:
    from iic_booking.equipment.models import Equipment, EquipmentStatus

    eq = Equipment.objects.filter(pk=equipment_id).first()
    if eq is None:
        return SlotLookup(ok=False, error="EQUIPMENT_NOT_FOUND", message="Equipment not found.")

    today = timezone.localdate()
    start = max(start_date, today)
    end = max(end_date, start)
    if (end - start).days > MAX_WINDOW_DAYS - 1:
        end = start + timedelta(days=MAX_WINDOW_DAYS - 1)

    status_code, data = _call_daily_slots_view(user=user, equipment_id=eq.pk, start=start, end=end)
    if status_code == 403:
        return SlotLookup(
            ok=False,
            equipment_id=eq.pk,
            error="EQUIPMENT_NOT_VISIBLE",
            message="This equipment is not available to your account.",
        )
    if status_code >= 400:
        return SlotLookup(
            ok=False,
            equipment_id=eq.pk,
            equipment_name=eq.name,
            error="SLOTS_UNAVAILABLE",
            message="Slot availability could not be loaded. Open the equipment page for the live calendar.",
        )

    now = timezone.now()
    candidates: list[dict[str, Any]] = []
    for row in data.get("slots") or []:
        if str(row.get("status") or "") != "AVAILABLE":
            continue
        if row.get("booking") or row.get("real_booking_id"):
            continue
        if "available_for_external" in row and row.get("available_for_external") is False:
            continue
        start_dt = parse_datetime(str(row.get("start_datetime") or ""))
        end_dt = parse_datetime(str(row.get("end_datetime") or ""))
        if start_dt is None or start_dt <= now:
            continue
        if after_time and timezone.localtime(start_dt).time() < after_time:
            continue
        candidates.append({"row": row, "start": start_dt, "end": end_dt})

    allowed = _home_department_allowed_ids(
        user=user, equipment=eq, slot_ids=[int(c["row"]["id"]) for c in candidates if c["row"].get("id")]
    )
    rows: list[dict[str, Any]] = []
    for c in sorted(candidates, key=lambda x: x["start"]):
        sid = int(c["row"].get("id") or 0)
        if sid not in allowed:
            continue
        rows.append(
            {
                "slot_id": sid,
                "date": c["row"].get("date"),
                "start": c["start"].isoformat(),
                "end": c["end"].isoformat() if c["end"] else None,
                "status": "AVAILABLE",
                "label": c["row"].get("status_display") or "Available",
            }
        )
        if len(rows) >= limit:
            break

    bookable = (eq.status or "").strip() == EquipmentStatus.ACTIVE
    return SlotLookup(
        ok=True,
        equipment_id=eq.pk,
        equipment_name=eq.name,
        rows=rows if bookable else [],
        equipment_status=eq.get_status_display() if hasattr(eq, "get_status_display") else eq.status,
        bookable_equipment=bookable,
        window_start=data.get("start_date") or start.isoformat(),
        window_end=data.get("end_date") or end.isoformat(),
        slot_window_min_date=data.get("slot_window_min_date"),
        slot_window_max_date=data.get("slot_window_max_date"),
        message="" if bookable else f"{eq.name} is currently {eq.get_status_display()} and cannot be booked.",
    )


def slot_is_bookable_for_user(*, user, equipment_id: int, slot_id: int) -> bool:
    """Re-check a single slot against the portal rules (used before proposing and executing)."""
    from iic_booking.equipment.models import DailySlot

    slot = DailySlot.objects.filter(pk=slot_id, slot_master__equipment_id=equipment_id).only("date").first()
    if slot is None or slot.date is None:
        return False
    lookup = find_bookable_slots(
        user=user, equipment_id=equipment_id, start_date=slot.date, end_date=slot.date, limit=500
    )
    return lookup.ok and any(r["slot_id"] == int(slot_id) for r in lookup.rows)
