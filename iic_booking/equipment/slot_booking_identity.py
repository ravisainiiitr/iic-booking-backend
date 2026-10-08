"""Who may see who booked a slot (name, department, contact, booking reference) on slot calendars.

Staff of the slot's equipment (Main Admin; Department Administrator of its department; Officer In-charge
incl. active temporary OIC; its Lab Operators) see every booker. Anyone else sees only the status, plus
the booker details of their own bookings.
"""

from __future__ import annotations

from iic_booking.users.models.user_type import UserType

SLOT_BOOKING_IDENTITY_FIELDS = (
    "booking",
    "booking_id",
    "real_booking_id",
    "booking_user_name",
    "booking_user_department_code",
    "booking_user_department_name",
    "booking_user_email",
    "booking_user_phone",
    "booking_user_type",
    "booking_sample_status_display",
)


def booking_identity_equipment_ids(user) -> set[int] | None:
    """Equipment whose bookers ``user`` may see; ``None`` means every equipment."""
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    user_type = getattr(user, "user_type", None)
    if user_type == UserType.ADMIN:
        return None
    if user_type in (UserType.DEPT_ADMIN, UserType.MANAGER, UserType.OPERATOR):
        from .api_views import _get_equipment_ids_for_log_access

        return set(_get_equipment_ids_for_log_access(user) or [])
    return set()


class SlotBookingIdentityPolicy:
    def __init__(self, user):
        authenticated = user is not None and getattr(user, "is_authenticated", False)
        self.user_id = user.pk if authenticated else None
        self.equipment_ids = booking_identity_equipment_ids(user)

    def allows(self, slot) -> bool:
        if not getattr(slot, "booking_id", None):
            return True
        if self.equipment_ids is None:
            return True
        slot_master = getattr(slot, "slot_master", None)
        equipment_id = getattr(slot_master, "equipment_id", None)
        booking = getattr(slot, "booking", None)
        if equipment_id is None:
            equipment_id = getattr(booking, "equipment_id", None)
        if equipment_id is not None and equipment_id in self.equipment_ids:
            return True
        return self.user_id is not None and getattr(booking, "user_id", None) == self.user_id


def mask_slot_booking_identity(data: dict) -> dict:
    for key in SLOT_BOOKING_IDENTITY_FIELDS:
        if key in data:
            data[key] = None
    if "booking_is_external" in data:
        data["booking_is_external"] = False
    return data
