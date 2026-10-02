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

from .models import Equipment, ExternalUserQuota, QuotaLimitType, QuotaType, UserTypeQuota
from .quota_utils import QuotaService, booking_quota_should_skip, quota_limit_is_effectively_unlimited


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


def _group_periods(user: User, equipment, reference_dt: datetime) -> list[dict]:
    """Mirror of QuotaService._validate_group_quotas with zero requested minutes."""
    equipment_group = equipment.equipment_group
    group_equipment_ids = list(equipment_group.equipment.values_list("equipment_id", flat=True))
    is_internal = UserType.is_internal_user(user.user_type)
    is_faculty = bool(user.is_faculty())
    wallet = user.get_accessible_wallet()
    is_using_faculty_wallet = bool(
        wallet
        and wallet.user.user_type == UserType.FACULTY
        and wallet.user_id != user.pk
    )
    use_faculty_quota = is_faculty or is_using_faculty_wallet

    monthly = QuotaService._get_group_quota(equipment_group, QuotaType.MONTHLY)
    weekly = QuotaService._get_group_quota(equipment_group, QuotaType.WEEKLY)

    periods: list[dict] = []
    if use_faculty_quota:
        for quota_obj, scope in ((monthly, "Faculty Monthly"), (weekly, "Faculty Weekly")):
            if quota_obj is None:
                continue
            limit = (
                quota_obj.internal_faculty_quota_minutes
                if is_internal
                else quota_obj.external_faculty_quota_minutes
            )
            if not limit or limit <= 0:
                continue
            result = QuotaService._evaluate_faculty_minutes(
                user=user,
                group_equipment_ids=group_equipment_ids,
                limit_minutes=limit,
                quota_type=quota_obj.quota_type,
                booking_date=reference_dt,
                additional_time_minutes=0,
                scope_label=scope,
                exclude_booking_id=None,
            )
            periods.append(
                _period_item(
                    quota_type=quota_obj.quota_type,
                    scope=scope,
                    shared=True,
                    limit_minutes=result.limit_minutes,
                    used_minutes=result.used_minutes,
                    reference_dt=reference_dt,
                )
            )

    if not is_faculty:
        for quota_obj, scope in ((monthly, "Individual Monthly"), (weekly, "Individual Weekly")):
            if quota_obj is None:
                continue
            limit = (
                quota_obj.internal_individual_quota_minutes
                if is_internal
                else quota_obj.external_individual_quota_minutes
            )
            if not limit or limit <= 0:
                continue
            result = QuotaService._evaluate_individual_minutes(
                user=user,
                group_equipment_ids=group_equipment_ids,
                limit_minutes=limit,
                quota_type=quota_obj.quota_type,
                booking_date=reference_dt,
                additional_time_minutes=0,
                scope_label=scope,
                exclude_booking_id=None,
            )
            periods.append(
                _period_item(
                    quota_type=quota_obj.quota_type,
                    scope=scope,
                    shared=False,
                    limit_minutes=result.limit_minutes,
                    used_minutes=result.used_minutes,
                    reference_dt=reference_dt,
                )
            )
    return periods


def _legacy_periods(user: User, equipment, reference_dt: datetime) -> list[dict]:
    """Equipment-level HOURS quotas (limit_value is in minutes), same querysets as enforcement."""
    is_external = bool(user.is_external())
    prefix = "External" if is_external else "Individual"
    periods: list[dict] = []
    for quota_type in (QuotaType.MONTHLY, QuotaType.WEEKLY):
        if is_external:
            quotas = ExternalUserQuota.objects.filter(
                equipment=equipment, quota_type=quota_type, is_enforced=True
            )
            snapshot_filter = QuotaService.LEGACY_EXTERNAL_SNAPSHOT_FILTER
        else:
            quotas = UserTypeQuota.objects.filter(
                equipment=equipment,
                user_type=user.user_type,
                quota_type=quota_type,
                is_enforced=True,
            )
            snapshot_filter = {"user_type_snapshot": user.user_type}
        hour_quotas = [q for q in quotas if q.limit_type == QuotaLimitType.HOURS]
        if not hour_quotas:
            continue
        start, end = QuotaService._get_quota_period(quota_type, reference_dt)
        used = QuotaService._sum_booking_quota_minutes(
            QuotaService._legacy_bookings_in_period(
                equipment=equipment,
                snapshot_filter=snapshot_filter,
                start_date=start,
                end_date=end,
            )
        )
        label = "Monthly" if quota_type == QuotaType.MONTHLY else "Weekly"
        for quota in hour_quotas:
            periods.append(
                _period_item(
                    quota_type=quota_type,
                    scope=f"{prefix} {label}",
                    shared=False,
                    limit_minutes=int(quota.limit_value or 0),
                    used_minutes=used,
                    reference_dt=reference_dt,
                )
            )
    return periods


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


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_my_booking_quota(request, pk):
    """
    Remaining weekly/monthly booking minutes for the current user (or ``user_id`` for staff).

    Query params: ``date`` (YYYY-MM-DD, any day in the week being viewed; default today).
    """
    equipment = get_object_or_404(
        Equipment.objects.select_related("equipment_group"),
        pk=pk,
    )

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
