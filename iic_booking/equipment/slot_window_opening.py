"""
Read-only: when the next week of slots opens for users.

Uses the same rule as the slots API: the equipment's own weekday + time when both are set,
otherwise the global InternalUserSlotWindowSetting. Internal and external users share that
instant (externals get the week after next at the same moment). Staff calendars show it as
the time slots open for users.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import Equipment


def next_slot_window_opening(ref_weekday, ref_time, at: datetime) -> Optional[datetime]:
    """First opening strictly after ``at`` (an opening exactly at ``at`` has already happened)."""
    if ref_weekday is None or ref_time is None:
        return None
    tz = timezone.get_current_timezone()
    local = timezone.localtime(at, tz)
    week_monday = local.date() - timedelta(days=local.weekday())
    day = week_monday + timedelta(days=int(ref_weekday))
    opening = timezone.make_aware(datetime.combine(day, ref_time), tz)
    if opening <= local:
        opening = timezone.make_aware(datetime.combine(day + timedelta(days=7), ref_time), tz)
    return opening


def slot_window_opening_payload(equipment: Optional[Equipment] = None, at: Optional[datetime] = None) -> dict:
    from .api_views import get_equipment_slot_window_reference_config, get_internal_slot_window_setting

    at = timezone.now() if at is None else at
    if timezone.is_naive(at):
        at = timezone.make_aware(at, timezone.get_current_timezone())
    local = timezone.localtime(at)

    if equipment is not None:
        ref_weekday, ref_time = get_equipment_slot_window_reference_config(equipment)
        own = equipment.slot_window_reference_weekday is not None and equipment.slot_window_reference_time is not None
        source = "equipment" if own else "global"
    else:
        setting = get_internal_slot_window_setting()
        ref_weekday = setting.reference_weekday if setting else None
        ref_time = setting.reference_time if setting else None
        source = "global"

    opening = next_slot_window_opening(ref_weekday, ref_time, at)
    applies = opening is not None
    return {
        "equipment_id": equipment.equipment_id if equipment is not None else None,
        "applies": applies,
        "weekday": int(ref_weekday) if applies else None,
        "time": ref_time.strftime("%H:%M") if applies else None,
        "source": source if applies else None,
        "next_opens_at": opening.isoformat() if applies else None,
        "server_time": at.isoformat(),
        "utc_offset_minutes": int(local.utcoffset().total_seconds() // 60),
    }


@api_view(["GET"])
@permission_classes([AllowAny])
def slot_window_opening(request):
    """
    GET /api/slot-window/opening/?equipment_id=<id>

    Without equipment_id: the global rule. With it: that equipment's effective rule, subject to the
    same visibility check as its slots calendar.
    """
    from .api_views import (
        equipment_visibility_denied_response,
        user_can_see_equipment,
        user_can_view_equipment_in_catalog,
    )

    raw = request.query_params.get("equipment_id")
    equipment = None
    if raw not in (None, ""):
        try:
            pk = int(raw)
        except (TypeError, ValueError):
            return Response({"error": "equipment_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
        equipment = Equipment.objects.filter(pk=pk).first()
        if equipment is None:
            return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
        user = request.user if request.user.is_authenticated else None
        if not user_can_see_equipment(user, equipment) and not user_can_view_equipment_in_catalog(user, equipment):
            return equipment_visibility_denied_response(user)

    response = Response(slot_window_opening_payload(equipment))
    response["Cache-Control"] = "no-store"
    return response
