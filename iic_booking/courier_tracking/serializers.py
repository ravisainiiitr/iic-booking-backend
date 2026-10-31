"""Response shapes and request validation for sample arrivals (courier details are entered by hand)."""

from __future__ import annotations

from rest_framework import serializers

from .constants import ArrivalMode
from .models import SampleArrival


def arrival_dict(arrival: SampleArrival, *, staff: bool = False, include_history: bool = True) -> dict:
    from iic_booking.communication.utils import booking_display_id_for_email
    from iic_booking.users.display import get_user_display_name

    booking = arrival.booking
    out = {
        "id": arrival.pk,
        "booking_id": booking.pk,
        "booking_reference": booking_display_id_for_email(booking),
        "booking_status": booking.status,
        "equipment": {"id": arrival.equipment_id, "name": arrival.equipment.name, "code": arrival.equipment.code},
        "department": (
            {"id": arrival.department_id, "name": arrival.department.name} if arrival.department_id else None
        ),
        "arrival_mode": arrival.arrival_mode,
        "is_active": arrival.is_active,
        "superseded_at": arrival.superseded_at.isoformat() if arrival.superseded_at else None,
        "sample_identifiers": arrival.sample_identifiers,
        "dispatch_date": arrival.dispatch_date.isoformat() if arrival.dispatch_date else None,
        "expected_arrival_date": arrival.expected_arrival_date.isoformat() if arrival.expected_arrival_date else None,
        "notes": arrival.notes,
        "created_at": arrival.created_at.isoformat(),
    }
    if arrival.arrival_mode == ArrivalMode.COURIER:
        out.update(
            {
                "courier": {"slug": arrival.courier_slug, "name": arrival.courier_name},
                "tracking_number": arrival.tracking_number,
                "receipt_note": "Staff confirm on the portal when the lab receives the sample.",
            }
        )
    if staff:
        out["submitted_by"] = {
            "id": booking.user_id,
            "name": get_user_display_name(booking.user) or "",
            "email": getattr(booking.user, "email", "") or "",
        }
    return out


class ArrivalRequestSerializer(serializers.Serializer):
    arrival_mode = serializers.ChoiceField(choices=ArrivalMode.choices)
    courier_slug = serializers.CharField(required=False, allow_blank=True, max_length=64)
    tracking_number = serializers.CharField(required=False, allow_blank=True, max_length=64)
    sample_identifiers = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    dispatch_date = serializers.DateField(required=False, allow_null=True)
    expected_arrival_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=2000)
