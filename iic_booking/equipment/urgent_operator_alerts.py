"""Email the equipment's Lab Operator(s) when an urgent request is allocated (its hold becomes a booking)."""

from __future__ import annotations

import logging

from django.utils import timezone

from iic_booking.communication.email_branding import absolute_http_url, slot_end_on_start_day, strftime_slot_end
from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import booking_display_id_for_email
from iic_booking.communication.utils import get_frontend_absolute_url
from iic_booking.users.display import get_user_display_name

logger = logging.getLogger(__name__)

EMAIL_TEMPLATE_CODE = "urgent_booking_allocated_operator_email"


def _duration_text(minutes) -> str:
    if not minutes:
        return ""
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    return f"{hours} h" if hours else f"{mins} min"


def _slot_lines(booking) -> str:
    lines = []
    for s in booking.daily_slots.order_by("start_datetime"):
        if not (s.start_datetime and s.end_datetime):
            continue
        start = timezone.localtime(s.start_datetime)
        end = timezone.localtime(s.end_datetime)
        end_text = strftime_slot_end(end, "%H:%M") if slot_end_on_start_day(start, end) else f"{end:%a %d %b %Y, %H:%M}"
        lines.append(f"{start:%a %d %b %Y, %H:%M} – {end_text}")
    return "\n".join(lines)


def _sample_lines(booking) -> str:
    from .input_display import booking_input_fields
    from .input_display import input_summary_lines

    try:
        rows = input_summary_lines(booking.input_values or {}, booking_input_fields(booking))
    except Exception:
        logger.exception("Urgent operator email: sample details unavailable booking_id=%s", booking.pk)
        return ""
    return "\n".join(f"{label}: {value}" for label, value in rows)


def send_urgent_allocation_operator_emails(booking, *, exclude_ids=()) -> set[int]:
    """Email each active Lab Operator of the equipment once. Returns the ids of the operators emailed."""
    from .booking_lab_messages import ensure_email_template
    from .models import UrgentBookingRequest
    from .reports import get_equipment_lab_incharge_users

    skip = set(exclude_ids or ())
    operators = [
        u for u in get_equipment_lab_incharge_users(booking.equipment) if u.id not in skip and (u.email or "").strip()
    ]
    if not operators:
        return set()
    try:
        ensure_email_template(EMAIL_TEMPLATE_CODE)
    except Exception:
        logger.exception("Urgent operator email template unavailable booking_id=%s", booking.pk)
        return set()

    urg = UrgentBookingRequest.objects.filter(hold_booking=booking).only("id", "admin_notes", "preferred_schedule").first()
    requester = booking.user
    equipment = booking.equipment
    path = f"/booking-management?expand={booking.booking_id}"
    context = {
        "booking_id": booking_display_id_for_email(booking) or str(booking.booking_id),
        "equipment_name": equipment.name,
        "equipment_code": equipment.code,
        "request_id": urg.id if urg else "",
        "requester_name": get_user_display_name(requester),
        "requester_category": str(requester.get_user_type_display_label() or ""),
        "required_time": _duration_text(booking.total_time_minutes),
        "allocated_slots": _slot_lines(booking),
        "sample_details": _sample_lines(booking),
        "oic_note": (getattr(urg, "admin_notes", "") or "").strip(),
        "link": absolute_http_url(get_frontend_absolute_url(path) or path),
    }
    metadata = {"booking_id": booking.booking_id, "urgent_booking_request_id": getattr(urg, "id", None)}
    sent: set[int] = set()
    for operator in operators:
        try:
            CommunicationService.send_email(
                recipient=operator,
                template=EMAIL_TEMPLATE_CODE,
                template_context={**context, "user_name": get_user_display_name(operator)},
                metadata=metadata,
            )
            sent.add(operator.id)
        except Exception:
            logger.exception("Failed urgent operator email booking_id=%s user_id=%s", booking.pk, operator.id)
    return sent
