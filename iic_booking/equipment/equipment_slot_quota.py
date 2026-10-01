"""
Per-user weekly / monthly slot limits saved on Equipment.

Equipment.internal_weekly_quota / internal_monthly_quota apply to internal users and
external_weekly_quota / external_monthly_quota to external users. Each is the number of slots one
user may hold on that equipment in an IST calendar week (Monday-Sunday) or calendar month.
A value of 0 or less means no limit.

Enforced only when settings.ENFORCE_EQUIPMENT_SLOT_QUOTA is True, and skipped by the same switches
as the group quota (SKIP_BOOKING_QUOTA_CHECK, Equipment.skip_quota_check). Staff acting from the
admin panel (Admin, Department Admin, OIC, Operator, ...) and urgent / hold bookings are exempt,
as they are for the group quota.

Usage follows the group quota's period rules (QuotaService): a booking belongs to a period through
quota_period_anchor_at when set, otherwise through any of its slots starting in the period; repeat
sample bookings never count. Every slot attached to a counted booking counts, so all sample sets'
slots are included. Bookings that are cancelled, refunded, waitlisted, on hold, disrupted or
unused do not count.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from iic_booking.users.models.user_type import UserType

from .models import Booking, BookingStatus, DailySlot, QuotaType
from .quota_utils import QuotaService, booking_quota_should_skip

SLOT_LIMIT_ERROR_CODE = "EQUIPMENT_SLOT_LIMIT"

SLOT_LIMIT_COUNTING_STATUSES = (
    BookingStatus.PENDING,
    BookingStatus.PENDING_PAYMENT,
    BookingStatus.BOOKED,
    BookingStatus.PROCESSING,
    BookingStatus.COMPLETED,
)

_PERIODS = (
    (QuotaType.WEEKLY, "weekly"),
    (QuotaType.MONTHLY, "monthly"),
)


@dataclass(frozen=True)
class SlotLimitUsage:
    quota_type: str
    limit: int
    used: int
    period_start: datetime
    period_end: datetime

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def as_dict(self) -> dict:
        return {
            "period": "weekly" if self.quota_type == QuotaType.WEEKLY else "monthly",
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
        }


def enforcement_enabled() -> bool:
    return bool(getattr(settings, "ENFORCE_EQUIPMENT_SLOT_QUOTA", False))


def is_exempt_actor(actor) -> bool:
    return getattr(actor, "user_type", None) in UserType.get_admin_panel_codes()


def configured_limits(equipment, user) -> dict[str, int]:
    """{QuotaType: limit} for the user's audience (internal / external); limits <= 0 are left out."""
    prefix = "external" if UserType.is_external_user(getattr(user, "user_type", None) or "") else "internal"
    out = {}
    for quota_type, name in _PERIODS:
        value = getattr(equipment, f"{prefix}_{name}_quota", None)
        if value is not None and int(value) > 0:
            out[quota_type] = int(value)
    return out


def limits_apply(equipment, user, *, actor=None, bypass: bool = False) -> dict[str, int]:
    """Limits to enforce for this request; empty when switched off, skipped or exempt."""
    if bypass or user is None or not enforcement_enabled() or booking_quota_should_skip(equipment):
        return {}
    if is_exempt_actor(actor if actor is not None else user):
        return {}
    return configured_limits(equipment, user)


def slots_used(user, equipment, quota_type: str, reference: datetime, *, exclude_booking_id=None) -> int:
    start, end = QuotaService._get_quota_period(quota_type, reference)
    has_slot_in_period = Booking.objects.filter(
        pk=OuterRef("pk"),
        daily_slots__start_datetime__gte=start,
        daily_slots__start_datetime__lte=end,
    )
    bookings = Booking.objects.filter(
        user=user,
        equipment=equipment,
        status__in=SLOT_LIMIT_COUNTING_STATUSES,
        source_booking__isnull=True,
    ).filter(
        Q(
            quota_period_anchor_at__isnull=False,
            quota_period_anchor_at__gte=start,
            quota_period_anchor_at__lte=end,
        )
        | (Q(quota_period_anchor_at__isnull=True) & Exists(has_slot_in_period))
    )
    if exclude_booking_id is not None:
        bookings = bookings.exclude(pk=exclude_booking_id)
    return DailySlot.objects.filter(booking__in=bookings).count()


def usage_summary(equipment, user, reference: Optional[datetime] = None) -> list[SlotLimitUsage]:
    reference = reference or timezone.now()
    out = []
    for quota_type, limit in configured_limits(equipment, user).items():
        start, end = QuotaService._get_quota_period(quota_type, reference)
        out.append(
            SlotLimitUsage(
                quota_type=quota_type,
                limit=limit,
                used=slots_used(user, equipment, quota_type, reference),
                period_start=start,
                period_end=end,
            )
        )
    return out


def _period_phrase(quota_type: str, start: datetime) -> str:
    now = timezone.localtime(timezone.now())
    current_start, _ = QuotaService._get_quota_period(quota_type, now)
    if quota_type == QuotaType.WEEKLY:
        return "this week" if start == current_start else f"in the week of {start.day} {start:%b %Y}"
    return "this month" if start == current_start else f"in {start:%B %Y}"


def slot_limit_error(
    user,
    equipment,
    *,
    slots_requested: int,
    reference: Optional[datetime],
    actor=None,
    bypass: bool = False,
    exclude_booking_id=None,
    action: str = "booking",
    lock: bool = False,
) -> Optional[str]:
    """
    Error message when the request would take the user past a weekly or monthly slot limit, else None.

    reference is the datetime that picks the period (first slot start, or the quota anchor for a
    disruption reschedule). For a move or edit pass exclude_booking_id: the booking's new slots
    replace its current ones, and the change is refused only if it leaves the user over the limit
    with more slots in that period than before, so bookings already over a newly set limit can
    still be moved within their period.

    lock=True takes the user row lock (inside the caller's transaction) so two concurrent bookings
    by the same user cannot both pass.
    """
    limits = limits_apply(equipment, user, actor=actor, bypass=bypass)
    if not limits or slots_requested <= 0:
        return None
    if lock:
        get_user_model().objects.select_for_update().filter(pk=user.pk).exists()
    reference = reference or timezone.now()
    for quota_type, name in _PERIODS:
        limit = limits.get(quota_type)
        if not limit:
            continue
        used = slots_used(user, equipment, quota_type, reference, exclude_booking_id=exclude_booking_id)
        projected = used + int(slots_requested)
        if projected <= limit:
            continue
        if exclude_booking_id is not None:
            before = slots_used(user, equipment, quota_type, reference)
            if projected <= before:
                continue
        start, _ = QuotaService._get_quota_period(quota_type, reference)
        return (
            f"{name.capitalize()} slot limit for {equipment.name} reached: you have booked {used} of "
            f"{limit} slots {_period_phrase(quota_type, start)}; this {action} needs {int(slots_requested)}."
        )
    return None
