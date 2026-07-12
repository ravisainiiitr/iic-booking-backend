"""Booking reminder notifications (e.g. same-day reminder at 8:30 AM)."""

import logging
from typing import TYPE_CHECKING

from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import get_frontend_absolute_url, booking_display_id_for_email
from iic_booking.equipment.booking_events import (
    apply_equipment_booking_email_extra_to_context,
    apply_user_sample_preparation_notice_to_context,
)

if TYPE_CHECKING:
    from .models import Booking

logger = logging.getLogger(__name__)


def send_reminder_for_booking(booking: "Booking") -> None:
    """
    Send a reminder email for a single booking (e.g. "Your booking is today").
    Uses template booking_reminder_email with context: user_name, booking_id, equipment_name,
    start_time, end_time, total_hours, total_charge, link.
    """
    if not booking or not booking.user or not booking.equipment:
        logger.warning("Invalid booking, user, or equipment for reminder")
        return

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
        first_slot = daily_slots[0]
        start_time = first_slot.start_datetime.strftime("%Y-%m-%d") if first_slot.start_datetime else ""
        end_time = ""
        total_hours = ""
    else:
        start_time = daily_slots[0].start_datetime.strftime("%Y-%m-%d %H:%M:%S") if daily_slots else ""
        end_time = daily_slots[-1].end_datetime.strftime("%Y-%m-%d %H:%M:%S") if daily_slots else ""
        total_hours = str(round(booking.total_time_minutes / 60, 2)) if booking.total_time_minutes else "0"

    display_booking_ref = booking_display_id_for_email(booking)
    booking_link = get_frontend_absolute_url(f"/my-bookings?booking={display_booking_ref}")

    template_context = {
        "user_name": user.name or user.email,
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

    metadata = {
        "booking_id": display_booking_ref,
        "real_booking_id": booking.booking_id,
        "notification_type": "reminder",
        "link": booking_link,
    }

    CommunicationService.send_email(
        recipient=user,
        template="booking_reminder_email",
        template_context=template_context,
        metadata=metadata,
    )
    logger.info("Booking reminder email sent to %s for booking_id=%s", user.email, booking.booking_id)
