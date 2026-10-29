"""Response shapes and request validation. No provider wording or secrets ever pass through here."""

from __future__ import annotations

from rest_framework import serializers

from .constants import TERMINAL_STATUSES, ArrivalMode
from .models import SampleArrival

REFUSAL_MESSAGES = {
    "too_soon": "This shipment was checked recently. Please try again later.",
    "daily_limit": "You have reached today's refresh limit. The shipment is still checked on a schedule.",
    "terminal": "This shipment has finished; no further checks are needed.",
    "department_disabled": "Courier tracking is switched off for this equipment's department.",
    "not_courier": "This is not an active courier shipment.",
    "not_external": "Courier tracking is available only for bookings made by external users.",
    "details_need_attention": "The courier could not track these details. Please check the courier and number.",
    "lookup_limit": "This shipment has reached its check limit; the lab can still check it.",
    "quota": "The monthly tracking limit has been reached. The last known status is shown.",
    "reserve": "Tracking checks are limited this month. The shipment is still checked on a schedule.",
    "in_progress": "A check of this shipment is already in progress.",
    "shared_recent_result": "Updated from a recent check of the same shipment.",
    "not_due": "No check is due yet.",
    "not_configured": "Courier tracking checks are not switched on yet. The shipment is registered.",
    "booking_not_confirmed": "The shipment is checked once the booking is confirmed. The shipment is registered.",
}


def refusal_message(code: str) -> str:
    if code.startswith("blocked:"):
        return "Tracking checks are paused at the moment. The last known status is shown."
    return REFUSAL_MESSAGES.get(code, "The shipment could not be checked now.")


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
                "email_updates": arrival.email_updates,
                "status": arrival.status,
                "status_label": arrival.get_status_display() if arrival.status else "",
                "provider_state": arrival.provider_state,
                "provider_status_label": arrival.provider_status_label,
                "is_terminal": arrival.status in TERMINAL_STATUSES,
                "delivery_reported": bool(arrival.delivery_reported_at),
                "delivery_reported_at": arrival.delivery_reported_at.isoformat() if arrival.delivery_reported_at else None,
                "delivery_event_at": arrival.delivery_event_at,
                "latest_checkpoint": arrival.latest_checkpoint or None,
                "checkpoints": arrival.checkpoints if include_history else None,
                "last_synced_at": arrival.last_lookup_success_at.isoformat() if arrival.last_lookup_success_at else None,
                "last_webhook_at": arrival.last_webhook_at.isoformat() if arrival.last_webhook_at else None,
                "next_check_at": arrival.next_lookup_at.isoformat() if arrival.next_lookup_at and arrival.is_active else None,
                "needs_attention": bool(arrival.last_problem) and arrival.status not in TERMINAL_STATUSES,
                "problem": arrival.last_problem if staff else ("details" if arrival.last_problem else ""),
                "receipt_note": "A courier-reported delivery is not confirmation that the lab has received the sample.",
            }
        )
    if staff:
        out["submitted_by"] = {
            "id": booking.user_id,
            "name": get_user_display_name(booking.user) or "",
            "email": getattr(booking.user, "email", "") or "",
        }
        out["lookup_count"] = arrival.lookup_count
    return out


class ArrivalRequestSerializer(serializers.Serializer):
    arrival_mode = serializers.ChoiceField(choices=ArrivalMode.choices)
    courier_slug = serializers.CharField(required=False, allow_blank=True, max_length=64)
    tracking_number = serializers.CharField(required=False, allow_blank=True, max_length=64)
    sample_identifiers = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    dispatch_date = serializers.DateField(required=False, allow_null=True)
    expected_arrival_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    email_updates = serializers.BooleanField(required=False, default=False)


class SettingsSerializer(serializers.Serializer):
    monthly_quota_fallback = serializers.IntegerField(required=False, min_value=1, max_value=1_000_000)
    safety_reserve = serializers.IntegerField(required=False, min_value=0, max_value=100_000)
    alert_threshold_percent = serializers.IntegerField(required=False, min_value=1, max_value=100)
    scheduled_interval_hours = serializers.IntegerField(required=False, min_value=6, max_value=24 * 30)
    priority_interval_hours = serializers.IntegerField(required=False, min_value=3, max_value=24 * 30)
    user_refresh_interval_hours = serializers.IntegerField(required=False, min_value=1, max_value=24 * 30)
    user_refresh_daily_limit = serializers.IntegerField(required=False, min_value=0, max_value=50)
    max_lookups_per_shipment = serializers.IntegerField(required=False, min_value=1, max_value=200)
    scheduled_lookups_enabled = serializers.BooleanField(required=False)
    arrival_reminder_after_days = serializers.IntegerField(required=False, min_value=1, max_value=60)
    stalled_after_days = serializers.IntegerField(required=False, min_value=1, max_value=60)
    reminder_interval_days = serializers.IntegerField(required=False, min_value=1, max_value=60)
    reminder_max_count = serializers.IntegerField(required=False, min_value=0, max_value=20)
    user_status_emails_enabled = serializers.BooleanField(required=False)
    staff_delivery_in_app = serializers.BooleanField(required=False)
    reason = serializers.CharField(min_length=5, max_length=500)


SETTINGS_FIELDS = [name for name in SettingsSerializer().fields if name != "reason"]
