"""Sample submission deadline advance reminders (email + in-app notification)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from django.db.models import Exists, OuterRef, Prefetch
from django.utils import timezone

from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import booking_display_id_for_email, get_frontend_absolute_url
from iic_booking.equipment.booking_events import (
    apply_equipment_booking_email_extra_to_context,
    apply_user_sample_preparation_notice_to_context,
)

if TYPE_CHECKING:
    from .models import Booking

logger = logging.getLogger(__name__)

# Notify this many hours before the sample submission deadline.
SAMPLE_SUBMISSION_DEADLINE_ADVANCE_HOURS = 12


def compute_sample_submission_deadline(booking: "Booking") -> Optional[datetime]:
    """
    Deadline by which the user should submit the sample:
      slot_start − sample_submission_lead_hours
    or slot_start when atmosphere-sensitive / lead hours is 0.
    Returns None if slots or equipment are missing.
    """
    from .serializers import _booking_slot_bounds

    equipment = getattr(booking, "equipment", None)
    if not equipment:
        return None
    start_dt, _end_dt = _booking_slot_bounds(booking)
    if start_dt is None:
        return None
    atmosphere = bool(getattr(booking, "atmosphere_sensitive_sample", False))
    lead_hours = int(getattr(equipment, "sample_submission_lead_hours", 0) or 0)
    if atmosphere or lead_hours <= 0:
        return start_dt
    return start_dt - timedelta(hours=lead_hours)


def sample_submission_already_accepted(booking: "Booking") -> bool:
    from .models import SampleTraceStatus
    from .serializers import _booking_sample_trace_events

    events = _booking_sample_trace_events(booking)
    return any(getattr(e, "status", None) == SampleTraceStatus.SAMPLE_ACCEPTED for e in events)


def is_within_sample_submission_advance_window(
    booking: "Booking",
    *,
    now: Optional[datetime] = None,
) -> tuple[bool, Optional[datetime], int]:
    """
    True when the sample submission deadline is approaching:
    notify_at (deadline − 12h) <= now < deadline, and sample not yet accepted.
    Returns (in_window, deadline, remaining_seconds).
    """
    now = now or timezone.now()
    if sample_submission_already_accepted(booking):
        return False, None, 0
    deadline = compute_sample_submission_deadline(booking)
    if deadline is None:
        return False, None, 0
    if timezone.is_naive(deadline):
        deadline = timezone.make_aware(deadline)
    remaining = int((deadline - now).total_seconds())
    if remaining <= 0:
        return False, deadline, remaining
    notify_at = deadline - timedelta(hours=SAMPLE_SUBMISSION_DEADLINE_ADVANCE_HOURS)
    if now < notify_at:
        return False, deadline, remaining
    return True, deadline, remaining


def send_sample_submission_deadline_reminder(booking: "Booking") -> bool:
    """
    Send email + in-app push once for an approaching sample submission deadline.
    Returns True if at least one channel was attempted without hard failure.
    """
    if not booking or not booking.user or not booking.equipment:
        logger.warning("Invalid booking for sample submission deadline reminder")
        return False

    if getattr(booking, "sample_submission_deadline_reminder_sent_at", None):
        return False

    in_window, deadline, remaining = is_within_sample_submission_advance_window(booking)
    if not in_window or deadline is None:
        return False

    user = booking.user
    equipment = booking.equipment
    display_booking_ref = booking_display_id_for_email(booking)
    booking_link = get_frontend_absolute_url(f"/my-bookings?booking={display_booking_ref}")

    from iic_booking.users.models.user_type import UserType

    daily_slots = list(booking.daily_slots.order_by("start_datetime"))
    recipient_is_admin_oic = getattr(user, "user_type", None) in UserType.get_admin_panel_codes()
    hide_time_display = (
        getattr(equipment, "weekly_view_display", None) == "SLOT_ID"
        and not recipient_is_admin_oic
    )
    if hide_time_display and daily_slots:
        start_time = daily_slots[0].start_datetime.strftime("%Y-%m-%d") if daily_slots[0].start_datetime else ""
        end_time = ""
    else:
        start_time = (
            daily_slots[0].start_datetime.strftime("%Y-%m-%d %H:%M:%S") if daily_slots else ""
        )
        end_time = (
            daily_slots[-1].end_datetime.strftime("%Y-%m-%d %H:%M:%S") if daily_slots else ""
        )

    lead_hours = int(getattr(equipment, "sample_submission_lead_hours", 0) or 0)
    deadline_display = deadline.strftime("%Y-%m-%d %H:%M:%S")
    remaining_hours = max(0, remaining // 3600)
    remaining_mins = max(0, (remaining % 3600) // 60)
    remaining_label = (
        f"{remaining_hours}h {remaining_mins}m"
        if remaining_hours
        else f"{remaining_mins} minute(s)"
    )

    template_context = {
        "user_name": user.name or user.email,
        "user_email": user.email,
        "booking_id": display_booking_ref,
        "equipment_name": equipment.name,
        "equipment_code": equipment.code,
        "start_time": start_time,
        "end_time": end_time,
        "submission_deadline": deadline_display,
        "lead_hours": str(lead_hours),
        "advance_hours": str(SAMPLE_SUBMISSION_DEADLINE_ADVANCE_HOURS),
        "remaining_label": remaining_label,
        "link": booking_link,
        "user_sample_preparation_notice": "",
        "user_sample_preparation_notice_html": "",
        "equipment_booking_email_extra": "",
        "equipment_booking_email_extra_html": "",
    }
    apply_equipment_booking_email_extra_to_context(
        template_context, equipment, also_append_to_comment=False
    )
    apply_user_sample_preparation_notice_to_context(
        template_context, user, equipment, also_append_to_comment=False
    )

    metadata = {
        "booking_id": display_booking_ref,
        "real_booking_id": booking.booking_id,
        "notification_type": "warning",
        "kind": "sample_submission_deadline",
        "link": booking_link,
        "submission_deadline": deadline.isoformat(),
    }

    email_ok = False
    push_ok = False
    try:
        CommunicationService.send_email(
            recipient=user,
            template="sample_submission_deadline_reminder_email",
            template_context=template_context,
            metadata=metadata,
        )
        email_ok = True
    except Exception:
        logger.exception(
            "Failed sample submission deadline email for booking_id=%s",
            booking.booking_id,
        )

    try:
        CommunicationService.send_push_notification(
            recipient=user,
            title="Sample submission deadline approaching",
            message=(
                f"Booking #{display_booking_ref} ({equipment.name}): please submit your sample "
                f"by {deadline_display} ({remaining_label} remaining)."
            ),
            metadata=metadata,
        )
        push_ok = True
    except Exception:
        logger.exception(
            "Failed sample submission deadline push for booking_id=%s",
            booking.booking_id,
        )

    if email_ok or push_ok:
        booking.sample_submission_deadline_reminder_sent_at = timezone.now()
        booking.save(update_fields=["sample_submission_deadline_reminder_sent_at", "updated_at"])
        logger.info(
            "Sample submission deadline reminder sent booking_id=%s email=%s push=%s",
            booking.booking_id,
            email_ok,
            push_ok,
        )
        return True
    return False


def iter_bookings_for_sample_submission_deadline_reminders():
    """BOOKED bookings that have not yet received the advance reminder."""
    from .models import Booking, BookingSampleTrace, BookingStatus, DailySlot, SampleTraceStatus

    now = timezone.now()
    # Bound scan: deadlines only apply for upcoming (or just-started) slots.
    horizon_end = now + timedelta(days=30)
    accepted = BookingSampleTrace.objects.filter(
        booking_id=OuterRef("pk"),
        status=SampleTraceStatus.SAMPLE_ACCEPTED,
    )
    return (
        Booking.objects.filter(
            status=BookingStatus.BOOKED,
            sample_submission_deadline_reminder_sent_at__isnull=True,
            daily_slots__start_datetime__gte=now - timedelta(hours=1),
            daily_slots__start_datetime__lte=horizon_end,
        )
        .exclude(Exists(accepted))
        .select_related("user", "equipment")
        .prefetch_related(
            Prefetch("daily_slots", queryset=DailySlot.objects.order_by("start_datetime")),
            "sample_trace_events",
        )
        .distinct()
    )


def list_approaching_sample_submission_for_user(user) -> list[dict]:
    """Payload for login / dashboard alerts for the given user."""
    from .models import Booking, BookingSampleTrace, BookingStatus, DailySlot, SampleTraceStatus

    accepted = BookingSampleTrace.objects.filter(
        booking_id=OuterRef("pk"),
        status=SampleTraceStatus.SAMPLE_ACCEPTED,
    )
    qs = (
        Booking.objects.filter(user=user, status=BookingStatus.BOOKED)
        .exclude(Exists(accepted))
        .select_related("equipment")
        .prefetch_related(
            Prefetch("daily_slots", queryset=DailySlot.objects.order_by("start_datetime")),
            "sample_trace_events",
        )
    )
    now = timezone.now()
    items = []
    for booking in qs:
        in_window, deadline, remaining = is_within_sample_submission_advance_window(
            booking, now=now
        )
        if not in_window or deadline is None:
            continue
        equipment = booking.equipment
        display_id = booking_display_id_for_email(booking)
        items.append(
            {
                "booking_id": booking.booking_id,
                "virtual_booking_id": getattr(booking, "virtual_booking_id", None) or display_id,
                "equipment_id": equipment.equipment_id if equipment else None,
                "equipment_name": equipment.name if equipment else "",
                "equipment_code": equipment.code if equipment else "",
                "deadline_at": deadline.isoformat(),
                "remaining_seconds": remaining,
                "lead_hours": int(getattr(equipment, "sample_submission_lead_hours", 0) or 0),
                "advance_hours": SAMPLE_SUBMISSION_DEADLINE_ADVANCE_HOURS,
                "link": f"/my-bookings?booking={display_id}",
            }
        )
    items.sort(key=lambda x: x["remaining_seconds"])
    return items
