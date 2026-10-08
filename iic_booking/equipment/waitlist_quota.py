"""Weekly / monthly quota for waitlist joins and waitlist confirmations.

Automatic paths enforce the same limits as a normal booking, in the week / month of the slots being
booked: joining the queue after a failed booking, FCFS auto-confirmation into slots freed by a
cancellation or reschedule, and the pre-reference sweep. An over-quota entry is skipped and stays
waitlisted; the freed slot goes to the next entry. Officer In Charge / Admin manual confirmation is
never blocked by these limits; it only shows a warning.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, time
from decimal import Decimal

from django.utils import timezone

from .quota_utils import QUOTA_ALLOWED, QuotaDecision, QuotaService, booking_quota_should_skip

logger = logging.getLogger(__name__)

STAFF_SKIP_LIMITS_NOTE = "Staff bookings skip limits."


class WaitlistQuotaExceeded(str):
    """Error text returned by the waitlist auto-booker when only the user's quota stood in the way."""


_join_request: ContextVar[dict | None] = ContextVar("waitlist_join_request", default=None)


@contextmanager
def waitlist_join_request_scope():
    """One booking request: lets the waitlist join step see the request's inputs and slots."""
    token = _join_request.set({})
    try:
        yield
    finally:
        _join_request.reset(token)


def note_waitlist_join_request(**facts) -> None:
    store = _join_request.get()
    if store is not None:
        store.update(facts)


def requested_minutes_for(equipment, user, input_values: dict | None, *, slot_count: int = 0) -> int:
    """Instrument time a waitlisted request needs (charge profile time formula, else whole slots)."""
    from .calculators import TimeCalculationEngine, build_safe_input_values_for_charge_calculation
    from .waitlist_booking import _resolve_charge_profile_for_user

    slot_duration = int(getattr(equipment, "slot_duration_minutes", None) or 60) or 60
    charge_profile, _user_type, _external = _resolve_charge_profile_for_user(equipment, user)
    if charge_profile:
        try:
            minutes = int(
                TimeCalculationEngine.calculate_time(
                    charge_profile,
                    build_safe_input_values_for_charge_calculation(dict(input_values or {}), equipment=equipment),
                    slot_duration_minutes=slot_duration,
                )
                or 0
            )
            if minutes > 0:
                return minutes
        except Exception:
            logger.debug("Waitlist quota: time formula failed for equipment %s", equipment.pk, exc_info=True)
    return max(0, int(slot_count or 0)) * slot_duration


def quota_reference_datetime(equipment, *, slot_ids=None, week_start=None):
    """The week / month a waitlisted request counts in: its first requested slot, else the viewed week."""
    from .models import DailySlot

    ids = [int(x) for x in (slot_ids or []) if x is not None]
    if ids:
        first = (
            DailySlot.objects.filter(id__in=ids, slot_master__equipment=equipment)
            .order_by("start_datetime")
            .values_list("start_datetime", flat=True)
            .first()
        )
        if first:
            return first
    if week_start is not None:
        if isinstance(week_start, str):
            from django.utils.dateparse import parse_date

            week_start = parse_date(week_start)
        if week_start is not None:
            return timezone.make_aware(datetime.combine(week_start, time(12, 0)))
    return timezone.now()


def evaluate_waitlist_quota(
    equipment, user, *, minutes: int, booking_date, charge: Decimal | None = None
) -> QuotaDecision:
    """Normal-booking quota pipeline (faculty / individual, monthly then weekly) for one more booking."""
    if booking_quota_should_skip(equipment) or int(minutes or 0) <= 0:
        return QUOTA_ALLOWED
    return QuotaService.evaluate_booking_quota(
        user,
        equipment,
        additional_time_minutes=int(minutes),
        additional_bookings=1,
        additional_charge=charge if charge is not None else Decimal("0.00"),
        booking_date=booking_date,
    )


def waitlist_join_quota_error(equipment, user) -> str | None:
    """Quota error that keeps a failed booking off the waitlist, or None.

    Only inside a booking request scope, and never for staff booking on someone's behalf or urgent
    holds (those skip limits).
    """
    facts = _join_request.get()
    if not facts or facts.get("skip_limits"):
        return None
    slot_ids = facts.get("slot_ids") or []
    minutes = requested_minutes_for(equipment, user, facts.get("input_values"), slot_count=len(slot_ids))
    if minutes <= 0:
        return None
    try:
        decision = evaluate_waitlist_quota(
            equipment,
            user,
            minutes=minutes,
            booking_date=quota_reference_datetime(
                equipment, slot_ids=slot_ids, week_start=facts.get("week_start")
            ),
        )
    except Exception:
        logger.exception("Waitlist join quota check failed for equipment %s user %s", equipment.pk, user.pk)
        return None
    return None if decision.allowed else (decision.error or "Your booking limit for this equipment is used up.")


def quota_warning_text(decision: QuotaDecision) -> str | None:
    """Warning for a manual confirmation that goes over a limit (None when within quota)."""
    if decision.allowed:
        return None
    failure = decision.failure
    if failure is None:
        return f"{decision.error or 'This booking is over the user’s booking limit.'} {STAFF_SKIP_LIMITS_NOTE}"
    period = {"WEEKLY": "weekly", "MONTHLY": "monthly"}.get(str(failure.quota_type or "").upper(), "")
    whose = "the faculty group’s" if failure.scope_kind == "group" else "the user’s"
    return (
        f"This will exceed {whose} {period} quota for this equipment "
        f"({int(failure.used_minutes)}/{int(failure.limit_minutes)} min used; this booking adds "
        f"{int(failure.requested_minutes)} min). {STAFF_SKIP_LIMITS_NOTE}"
    ).replace("  ", " ")


def manual_confirm_quota_warning(equipment, user, *, minutes: int, booking_date) -> dict:
    """``quota_warning`` (text or None) and ``quota_failure`` (breakdown) for the manual-confirm dialog."""
    try:
        decision = evaluate_waitlist_quota(equipment, user, minutes=minutes, booking_date=booking_date)
    except Exception:
        logger.exception("Manual waitlist confirm quota preview failed for equipment %s", equipment.pk)
        return {"quota_warning": None, "quota_failure": None}
    return {
        "quota_warning": quota_warning_text(decision),
        "quota_failure": decision.failure.payload() if decision.failure is not None else None,
    }
