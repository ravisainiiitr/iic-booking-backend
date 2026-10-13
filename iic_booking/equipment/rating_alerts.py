"""Close the loop on poor booking ratings: the instrument's OIC and operators hear about them straight away."""

from __future__ import annotations

LOW_RATING_MAX = 2


def is_low(rating) -> bool:
    return rating is not None and int(rating) <= LOW_RATING_MAX


def notify_low_rating(booking, *, previous_rating=None) -> None:
    """In-app (and browser push) alert to OIC and operators when a rating newly drops to 2/5 or below."""
    if not is_low(booking.rating) or is_low(previous_rating):
        return
    from iic_booking.communication.in_app import equipment_oic_users, notify_in_app, person_label

    from .models import EquipmentOperator

    equipment = booking.equipment
    operators = [
        row.operator
        for row in EquipmentOperator.objects.filter(equipment_id=equipment.pk).select_related("operator")
        if row.operator_id
    ]
    display_id = booking.virtual_booking_id or f"{equipment.code}-{booking.booking_id}"
    feedback = " ".join((booking.rating_feedback or "").split())
    if len(feedback) > 280:
        feedback = feedback[:279].rstrip() + "…"
    message = f"{person_label(booking.user)} rated {display_id} on {equipment.name} {booking.rating}/5."
    message += f' Comment: "{feedback}"' if feedback else " No comment was left."
    message += " Please review the booking and follow up with the user."
    notify_in_app(
        equipment_oic_users(equipment) + operators,
        title=f"Low rating ({booking.rating}/5) on booking {display_id}",
        message=message,
        link=f"/booking-management?expand={booking.booking_id}",
        notification_type="warning",
        event="booking_low_rating",
        extra={"notification_category": "staff", "booking_id": booking.booking_id},
    )
