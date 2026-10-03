"""
Read-only "my booking quota" summary for the booking page.

Numbers come from the same QuotaService helpers that booking enforcement uses, so the
remaining minutes shown to the user match what a booking attempt would be checked against.
No row locks: this is a display-only read.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user import User
from iic_booking.users.models.user_type import UserType

from .models import Booking, Equipment
from .quota_utils import (
    QuotaService,
    booking_counts_toward_quota,
    booking_effective_quota_minutes,
    booking_quota_reference_datetime,
    booking_quota_should_skip,
    quota_limit_is_effectively_unlimited,
)


def _period_item(*, quota_type: str, scope: str, shared: bool, limit_minutes: int, used_minutes: int, reference_dt: datetime) -> dict:
    start, end = QuotaService._get_quota_period(quota_type, reference_dt)
    return {
        "period": quota_type,
        "scope": scope,
        "shared": shared,
        "limit_minutes": int(limit_minutes),
        "used_minutes": int(used_minutes),
        "remaining_minutes": max(0, int(limit_minutes) - int(used_minutes)),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
    }


def _dimension_periods(dims, reference_dt: datetime) -> list[dict]:
    """Usage of each limit with zero requested minutes, through the same path as enforcement."""
    periods: list[dict] = []
    for dim in dims:
        if dim.scope != "pool" and dim.limit_minutes <= 0:
            continue
        result = QuotaService.evaluate_dimension(dim, booking_date=reference_dt)
        periods.append(
            _period_item(
                quota_type=dim.quota_type,
                scope=dim.scope_label,
                shared=dim.scope == "group",
                limit_minutes=result.limit_minutes,
                used_minutes=result.used_minutes,
                reference_dt=reference_dt,
            )
        )
    return periods


def _group_periods(user: User, equipment, reference_dt: datetime) -> list[dict]:
    return _dimension_periods(QuotaService.group_quota_dimensions(user, equipment.equipment_group), reference_dt)


def _legacy_periods(user: User, equipment, reference_dt: datetime) -> list[dict]:
    """Equipment-level HOURS quotas (limit_value is in minutes)."""
    return _dimension_periods(QuotaService.legacy_quota_dimensions(user, equipment), reference_dt)


def build_booking_quota_summary(user: User, equipment, reference_day: date) -> dict:
    """Remaining weekly/monthly booking minutes for ``user`` on ``equipment`` around ``reference_day``."""
    reference_dt = timezone.make_aware(
        datetime.combine(reference_day, time(12, 0)), timezone.get_current_timezone()
    )
    group = getattr(equipment, "equipment_group", None)
    summary = {
        "equipment_id": equipment.pk,
        "equipment_name": equipment.name,
        "equipment_group_name": group.name if group else None,
        "reference_date": reference_day.isoformat(),
        "applies": False,
        "reason": None,
        "periods": [],
        "remaining_minutes": None,
        "binding": None,
    }

    if getattr(user, "user_type", None) in UserType.get_admin_panel_codes():
        summary["reason"] = "staff"
        return summary
    if booking_quota_should_skip(equipment):
        summary["reason"] = "skipped"
        return summary

    if group is not None:
        periods = _group_periods(user, equipment, reference_dt)
    else:
        periods = _legacy_periods(user, equipment, reference_dt)
    periods = [p for p in periods if not quota_limit_is_effectively_unlimited(p["period"], p["limit_minutes"])]

    if not periods:
        summary["reason"] = "no_limits"
        return summary

    binding = min(periods, key=lambda p: p["remaining_minutes"])
    summary.update(
        applies=True,
        periods=periods,
        remaining_minutes=binding["remaining_minutes"],
        binding=binding,
    )
    return summary


def _resolve_target_user(request, equipment) -> tuple[Optional[User], Optional[Response]]:
    user_id_raw = request.query_params.get("user_id")
    if user_id_raw in (None, ""):
        return request.user, None
    actor_type = str(getattr(request.user, "user_type", None) or "").strip().lower()
    if actor_type not in {UserType.ADMIN, UserType.MANAGER, UserType.DEPT_ADMIN}:
        return None, Response(
            {"error": "Only Admin, OIC, or Department Administrator can query another user's quota."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if actor_type == UserType.DEPT_ADMIN:
        dept_id = getattr(request.user, "department_id", None)
        if not dept_id or getattr(equipment, "internal_department_id", None) != dept_id:
            return None, Response(
                {"error": "You can only query quotas for equipment in your assigned department."},
                status=status.HTTP_403_FORBIDDEN,
            )
    try:
        return User.objects.get(pk=int(user_id_raw)), None
    except (TypeError, ValueError, User.DoesNotExist):
        return None, Response({"error": "Invalid user_id."}, status=status.HTTP_400_BAD_REQUEST)


def _booking_quota_response(request, equipment, booking_id_raw: str) -> Response:
    try:
        booking = (
            Booking.objects.select_related("user")
            .prefetch_related("daily_slots")
            .get(pk=int(booking_id_raw), equipment_id=equipment.pk)
        )
    except (TypeError, ValueError, Booking.DoesNotExist):
        return Response({"error": "Invalid booking_id."}, status=status.HTTP_400_BAD_REQUEST)
    if booking.user_id != request.user.pk:
        actor_type = str(getattr(request.user, "user_type", None) or "").strip().lower()
        dept_ok = actor_type != UserType.DEPT_ADMIN or (
            getattr(request.user, "department_id", None)
            and getattr(equipment, "internal_department_id", None) == request.user.department_id
        )
        if actor_type not in {UserType.ADMIN, UserType.MANAGER, UserType.DEPT_ADMIN} or not dept_ok:
            return Response(
                {"error": "You can only view the quota of your own bookings."},
                status=status.HTTP_403_FORBIDDEN,
            )
    reference_dt = booking_quota_reference_datetime(booking) or timezone.now()
    summary = build_booking_quota_summary(booking.user, equipment, timezone.localtime(reference_dt).date())
    counts = booking_counts_toward_quota(booking)
    summary["booking"] = {
        "id": booking.pk,
        "counts_toward_quota": counts,
        "minutes": booking_effective_quota_minutes(booking) if counts else 0,
    }
    return Response(summary, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_my_booking_quota(request, pk):
    """
    Remaining weekly/monthly booking minutes for the current user (or ``user_id`` for staff).

    Query params: ``date`` (YYYY-MM-DD, any day in the week being viewed; default today).
    ``booking_id``: summary for that booking's owner and quota period instead, plus the
    minutes the booking itself counts (used by the Edit inputs dialog).
    """
    equipment = get_object_or_404(
        Equipment.objects.select_related("equipment_group"),
        pk=pk,
    )

    booking_id_raw = (request.query_params.get("booking_id") or "").strip()
    if booking_id_raw:
        return _booking_quota_response(request, equipment, booking_id_raw)

    date_raw = (request.query_params.get("date") or "").strip()
    if date_raw:
        try:
            reference_day = datetime.strptime(date_raw, "%Y-%m-%d").date()
        except ValueError:
            return Response(
                {"error": "Invalid date. Use YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        reference_day = timezone.localdate()

    target_user, error = _resolve_target_user(request, equipment)
    if error is not None:
        return error

    return Response(build_booking_quota_summary(target_user, equipment, reference_day), status=status.HTTP_200_OK)
