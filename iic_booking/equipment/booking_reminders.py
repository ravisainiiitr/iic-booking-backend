"""Booking reminder notifications (e.g. same-day reminder at 8:30 AM)."""

import logging
from typing import TYPE_CHECKING, Optional

from iic_booking.communication.email_branding import format_local_dt
from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import get_frontend_absolute_url, booking_display_id_for_email
from iic_booking.equipment.booking_events import (
    apply_booking_party_to_context,
    apply_equipment_booking_email_extra_to_context,
    apply_user_sample_preparation_notice_to_context,
)
from iic_booking.users.display import get_user_display_name

if TYPE_CHECKING:
    from .models import Booking

logger = logging.getLogger(__name__)


def build_reminder_context(booking: "Booking") -> Optional[dict]:
    """
    Template context for booking_reminder_email (user_name, booking_id, equipment_name, start_time,
    end_time, total_hours, total_charge, link, ...). Slot times are shown in local time (IST).
    Returns None for an invalid booking. Has no side effects.
    """
    if not booking or not booking.user or not booking.equipment:
        return None

    user = booking.user
    equipment = booking.equipment

    daily_slots = list(booking.daily_slots.order_by("start_datetime"))
    from iic_booking.users.models.user_type import UserType
    recipient_is_admin_oic = getattr(user, "user_type", None) in UserType.get_admin_panel_codes()
    hide_time_display = (
        getattr(equipment, "weekly_view_display", None) == "SLOT_ID"
        and not recipient_is_admin_oic
    )

    if hide_time_display and daily_slots:
        start_time = format_local_dt(daily_slots[0].start_datetime, "%Y-%m-%d")
        end_time = ""
        total_hours = ""
    else:
        start_time = format_local_dt(daily_slots[0].start_datetime, "%Y-%m-%d %H:%M:%S") if daily_slots else ""
        end_time = format_local_dt(daily_slots[-1].end_datetime, "%Y-%m-%d %H:%M:%S") if daily_slots else ""
        total_hours = str(round(booking.total_time_minutes / 60, 2)) if booking.total_time_minutes else "0"

    display_booking_ref = booking_display_id_for_email(booking)
    booking_link = get_frontend_absolute_url(f"/my-bookings?booking={display_booking_ref}")

    template_context = {
        "user_name": get_user_display_name(user),
        "user_email": user.email,
        "booking_id": display_booking_ref,
        "equipment_name": equipment.name,
        "equipment_code": equipment.code,
        "start_time": start_time,
        "end_time": end_time,
        "total_charge": str(booking.total_charge),
        "total_hours": total_hours,
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
    apply_booking_party_to_context(template_context, booking)
    return template_context


def send_reminder_for_booking(booking: "Booking") -> None:
    """Send a reminder email for a single booking (e.g. "Your booking is today")."""
    template_context = build_reminder_context(booking)
    if template_context is None:
        logger.warning("Invalid booking, user, or equipment for reminder")
        return

    display_booking_ref = booking_display_id_for_email(booking)
    booking_link = get_frontend_absolute_url(f"/my-bookings?booking={display_booking_ref}")
    metadata = {
        "booking_id": display_booking_ref,
        "real_booking_id": booking.booking_id,
        "notification_type": "reminder",
        "link": booking_link,
    }

    CommunicationService.send_email(
        recipient=booking.user,
        template="booking_reminder_email",
        template_context=template_context,
        metadata=metadata,
    )
    logger.info("Booking reminder email sent to %s for booking_id=%s", booking.user.email, booking.booking_id)
