"""What happens to a slot a booking gave back (``DailySlot.released_by_booking_at``), by the time left until it starts.

- More than 48 hours: the waitlist gets it automatically, in queue order.
- 48 hours down to just over 1 hour: nothing is allocated; active waitlist candidates are told it is free.
- 1 hour or less: closed to users, who see Not Available. Staff can still allocate it or confirm a waitlist entry.
- Once it has started: an ordinary past Available slot, which calendars show as No booking.

The closed period is worked out from the clock, so nothing has to run when it starts or ends. Slots that were never
booked, and slots staff set to Available (which clears the marker), stay open to users as before.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone

AUTO_ALLOCATE_LEAD = timedelta(hours=48)
USER_LOCK_LEAD = timedelta(hours=1)

USERS_SEE_LABEL = "Not Available"
STAFF_REASON = (
    "A booking gave this slot back within an hour of its start, so users cannot book it. "
    "You can still allocate it to a user or confirm a waitlist entry."
)


def _aware(dt: datetime) -> datetime:
    return timezone.make_aware(dt, timezone.get_current_timezone()) if timezone.is_naive(dt) else dt


def freed_slot_locked_for_users(slot, now: datetime | None = None) -> bool:
    from .models import SlotStatus

    start = getattr(slot, "start_datetime", None)
    if (
        start is None
        or slot.status != SlotStatus.AVAILABLE
        or slot.booking_id is not None
        or slot.released_by_booking_at is None
    ):
        return False
    now = now or timezone.now()
    return now < _aware(start) <= now + USER_LOCK_LEAD


def exclude_freed_slots_locked_for_users(queryset, now: datetime | None = None):
    now = now or timezone.now()
    return queryset.exclude(
        released_by_booking_at__isnull=False,
        start_datetime__gt=now,
        start_datetime__lte=now + USER_LOCK_LEAD,
    )


def apply_freed_slot_lock_to_slot_payloads(slots, rows: list[dict], *, staff: bool, now: datetime | None = None) -> None:
    """Users get the slot as Not Available. Staff keep the real status, with what users see and why."""
    now = now or timezone.now()
    locked = {s.pk for s in slots if freed_slot_locked_for_users(s, now)}
    if not locked:
        return
    for row in rows:
        if row.get("id") not in locked:
            continue
        if staff:
            row["users_blocked_label"] = USERS_SEE_LABEL
            row["users_blocked_reason"] = STAFF_REASON
        else:
            row.update(
                status="NOT_AVAILABLE",
                display_status="NOT_AVAILABLE",
                status_display=USERS_SEE_LABEL,
                available_for_external=False,
            )
