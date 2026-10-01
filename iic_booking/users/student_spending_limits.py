"""Weekly / monthly spending limits a supervising faculty sets for a student on their wallet.

The limits live on the approved ``WalletJoinRequest`` (the supervisor-student link) and only apply to
charges the student puts on that supervisor's wallet.

Spend in a period is the sum over the student's bookings created in the period (IST; weeks run
Monday-Sunday, months are calendar months), counting only bookings created after the student joined
this supervisor's wallet. Bookings that were never charged or were fully refunded (cancelled, refunded,
operator unavailable, waitlisted, awaiting external payment) are left out. Every other booking counts at
its current ``total_charge``, which already reflects partial cancellations and charge recalculations,
minus any input-edit extra that has not been paid yet. A partial refund therefore lowers the spend of the
period the booking was created in, not the period the refund happened in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional
from zoneinfo import ZoneInfo

from django.db.models import Q, Sum
from django.utils import timezone

IST = ZoneInfo("Asia/Kolkata")
ZERO = Decimal("0.00")
MAX_LIMIT_INR = Decimal("9999999999.99")
SPENDING_LIMIT_ERROR_CODE = "STUDENT_SPENDING_LIMIT_EXCEEDED"


def _not_charged_statuses():
    from iic_booking.equipment.models import BookingStatus

    return (
        BookingStatus.CANCELLED,
        BookingStatus.REFUNDED,
        BookingStatus.ABSENT,
        BookingStatus.WAITLISTED,
        BookingStatus.PENDING_PAYMENT,
    )


def _money(value) -> Decimal:
    return Decimal(value or 0).quantize(Decimal("0.01"))


def format_inr(value) -> str:
    return f"₹{_money(value):,.2f}"


def week_bounds(at: Optional[datetime] = None) -> tuple[datetime, datetime]:
    local = (at or timezone.now()).astimezone(IST)
    start = datetime.combine(local.date() - timedelta(days=local.weekday()), time.min, tzinfo=IST)
    return start, start + timedelta(days=7)


def month_bounds(at: Optional[datetime] = None) -> tuple[datetime, datetime]:
    local = (at or timezone.now()).astimezone(IST)
    start = datetime(local.year, local.month, 1, tzinfo=IST)
    if local.month == 12:
        end = datetime(local.year + 1, 1, 1, tzinfo=IST)
    else:
        end = datetime(local.year, local.month + 1, 1, tzinfo=IST)
    return start, end


def supervisor_link_for(student, wallet_target, *, lock: bool = False):
    """Approved link between ``student`` and the owner of ``wallet_target`` (Wallet or SubWallet), if any."""
    from iic_booking.users.models.wallet import SubWallet, WalletJoinRequest, WalletJoinRequestStatus

    if student is None or wallet_target is None:
        return None
    wallet = wallet_target.wallet if isinstance(wallet_target, SubWallet) else wallet_target
    if wallet is None or wallet.user_id == student.pk:
        return None
    qs = WalletJoinRequest.objects.filter(
        Q(wallet_id=wallet.pk) | Q(wallet__isnull=True, faculty_id=wallet.user_id),
        student_id=student.pk,
        status=WalletJoinRequestStatus.APPROVED,
    ).order_by("-id")
    if lock:
        qs = qs.select_for_update()
    return qs.first()


def student_spend(link, start: datetime, end: datetime) -> Decimal:
    from iic_booking.equipment.models import Booking

    since = max(start, link.responded_at) if link.responded_at else start
    if since >= end:
        return ZERO
    totals = (
        Booking.objects.filter(user_id=link.student_id, created_at__gte=since, created_at__lt=end)
        .exclude(status__in=_not_charged_statuses())
        .aggregate(
            charged=Sum("total_charge"),
            unpaid=Sum(
                "charge_recalculation_pending_amount",
                filter=Q(charge_recalculation_pending_amount__gt=0),
            ),
        )
    )
    spent = _money(totals["charged"]) - _money(totals["unpaid"])
    return spent if spent > ZERO else ZERO


@dataclass(frozen=True)
class _PeriodUsage:
    kind: str
    label: str
    limit: Optional[Decimal]
    start: datetime
    end: datetime
    spent: Decimal

    @property
    def remaining(self) -> Optional[Decimal]:
        if self.limit is None:
            return None
        left = _money(self.limit) - self.spent
        return left if left > ZERO else ZERO


def _usage(link, at: datetime) -> list[_PeriodUsage]:
    w_start, w_end = week_bounds(at)
    m_start, m_end = month_bounds(at)
    return [
        _PeriodUsage("weekly", "week", link.weekly_limit_inr, w_start, w_end, student_spend(link, w_start, w_end)),
        _PeriodUsage("monthly", "month", link.monthly_limit_inr, m_start, m_end, student_spend(link, m_start, m_end)),
    ]


def _amount_or_none(value) -> Optional[str]:
    return None if value is None else str(_money(value))


def limit_summary(link, at: Optional[datetime] = None) -> dict:
    """Limits and current week / month spend for one supervisor-student link (API payload)."""
    now = at or timezone.now()
    week, month = _usage(link, now)
    return {
        "join_request_id": link.pk,
        "student": link.student_id,
        "faculty": link.faculty_id,
        "spending_limit_enabled": bool(link.spending_limit_enabled),
        "weekly_limit_inr": _amount_or_none(link.weekly_limit_inr),
        "monthly_limit_inr": _amount_or_none(link.monthly_limit_inr),
        "week_start": week.start.date().isoformat(),
        "week_end": (week.end - timedelta(days=1)).date().isoformat(),
        "month_start": month.start.date().isoformat(),
        "month_end": (month.end - timedelta(days=1)).date().isoformat(),
        "week_spent_inr": str(week.spent),
        "month_spent_inr": str(month.spent),
        "weekly_remaining_inr": _amount_or_none(week.remaining),
        "monthly_remaining_inr": _amount_or_none(month.remaining),
        "spending_limit_updated_at": (
            link.spending_limit_updated_at.isoformat() if link.spending_limit_updated_at else None
        ),
    }


def spending_limit_error(
    student,
    wallet_target,
    amount,
    *,
    attribution_at: Optional[datetime] = None,
    charge_label: str = "booking",
    lock: bool = False,
    at: Optional[datetime] = None,
) -> Optional[str]:
    """Message when charging ``amount`` to ``wallet_target`` would push the student past a supervisor limit.

    ``attribution_at`` is the creation time of the booking the charge belongs to (defaults to now). Limits
    are only enforced for periods that are still running, so an extra charge on a booking from a week or
    month that has already ended is not blocked by that closed period. Call with ``lock=True`` inside the
    transaction that debits the wallet so concurrent bookings by the same student are serialised.
    """
    try:
        amount = _money(amount)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if amount <= ZERO:
        return None
    link = supervisor_link_for(student, wallet_target, lock=lock)
    if link is None or not link.spending_limit_enabled:
        return None
    now = at or timezone.now()
    for usage in _usage(link, attribution_at or now):
        if usage.limit is None or usage.end <= now:
            continue
        if usage.spent + amount > _money(usage.limit):
            return (
                f"This {charge_label} exceeds the {usage.kind} spending limit ({format_inr(usage.limit)}) "
                f"set by your supervisor. Remaining this {usage.label}: {format_inr(usage.remaining)}."
            )
    return None


class SpendingLimitValidationError(ValueError):
    pass


def _parse_limit(raw, field_label: str) -> Optional[Decimal]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        value = Decimal(str(raw).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        raise SpendingLimitValidationError(f"{field_label} must be a number.")
    if not value.is_finite():
        raise SpendingLimitValidationError(f"{field_label} must be a number.")
    if value < 0:
        raise SpendingLimitValidationError(f"{field_label} cannot be negative.")
    if value > MAX_LIMIT_INR:
        raise SpendingLimitValidationError(f"{field_label} is too large.")
    return _money(value)


def apply_spending_limit_update(link, data) -> None:
    """Validate and save the toggle / amounts sent by the supervisor. Raises SpendingLimitValidationError."""
    raw_enabled = data.get("spending_limit_enabled", link.spending_limit_enabled)
    if isinstance(raw_enabled, str):
        enabled = raw_enabled.strip().lower() in ("1", "true", "yes", "on")
    else:
        enabled = bool(raw_enabled)
    weekly = (
        _parse_limit(data.get("weekly_limit_inr"), "Weekly limit")
        if "weekly_limit_inr" in data
        else link.weekly_limit_inr
    )
    monthly = (
        _parse_limit(data.get("monthly_limit_inr"), "Monthly limit")
        if "monthly_limit_inr" in data
        else link.monthly_limit_inr
    )
    if enabled and weekly is None and monthly is None:
        raise SpendingLimitValidationError("Enter a weekly limit, a monthly limit, or both.")
    link.spending_limit_enabled = enabled
    link.weekly_limit_inr = weekly
    link.monthly_limit_inr = monthly
    link.spending_limit_updated_at = timezone.now()
    link.save()
