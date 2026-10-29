"""Reminders to external users, decided from local booking and tracking data only (never a provider call).

Kinds: arrival method not chosen; courier details need attention (the provider could not track what was entered);
shipment not progressing (no status change for ``stalled_after_days``); delivery problem (failed attempt / exception).
Each kind is sent at most ``reminder_max_count`` times per booking, at least ``reminder_interval_days`` apart, and
stops by itself once its condition no longer holds. Internal users never get courier reminders, MANUAL arrivals get
none, and bookings that are no longer waiting for a sample (cancelled, completed, sample received) are skipped.
Departments in test-users-only mode remind test users only. "Arrival method not chosen" goes only to confirmed
bookings made after the department's switch was turned on, for equipment that receives samples (not fabrication or
walk-in), and never when the booking already has a sample-trace event.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .constants import (
    ARRIVAL_OPEN_BOOKING_STATUSES,
    ARRIVAL_REMINDER_BOOKING_STATUSES,
    DETAIL_PROBLEM_OUTCOMES,
    ArrivalMode,
    ShipmentStatus,
)
from .eligibility import booking_is_external, department_tracking_enabled, tracking_enabled_since
from .models import CourierTrackingSettings, DeliveryStatus, ReminderKind, SampleArrival, ShipmentReminder

logger = logging.getLogger(__name__)

TEMPLATE_REMINDER = "courier_reminder_user"

MESSAGES = {
    ReminderKind.ARRIVAL_NOT_SELECTED: (
        "Please let the lab know how your sample will arrive: by courier (with its tracking number) or in person."
    ),
    ReminderKind.DETAILS_INCOMPLETE: (
        "We could not find tracking information for the courier and tracking number you entered. Please check them "
        "and update the shipment details, or switch to manual delivery."
    ),
    ReminderKind.NOT_PROGRESSING: (
        "The courier has not reported any progress on your sample shipment for a few days. Courier updates are "
        "sometimes delayed; you may wish to check with the courier."
    ),
    ReminderKind.DELIVERY_PROBLEM: (
        "The courier reports a delivery attempt that did not succeed or another delivery issue. You may wish to "
        "contact the courier. Courier updates can be delayed."
    ),
}

_RECEIVED_TRACE_STATUSES = ("HELD_AT_OFFICE", "FORWARDED_TO_LAB", "SAMPLE_ACCEPTED", "SAMPLE_REJECTED", "PROCESSING",
                            "COMPLETED")


def _sample_already_received(booking) -> bool:
    return booking.sample_trace_events.filter(status__in=_RECEIVED_TRACE_STATUSES).exists()


def _due(booking, kind: str, cfg: CourierTrackingSettings) -> int | None:
    """Next sequence number if this reminder may be sent now, else None."""
    sent = list(ShipmentReminder.objects.filter(booking=booking, kind=kind).order_by("-sequence")[:1])
    if not sent:
        return 1
    last = sent[0]
    if last.sequence >= cfg.reminder_max_count:
        return None
    if last.created_at > timezone.now() - timedelta(days=cfg.reminder_interval_days):
        return None
    return last.sequence + 1


def _arrival_reminder_applies(booking, enabled_since) -> bool:
    """Whether a booking with no declaration may be asked how its sample will arrive. Only confirmed bookings made
    after Courier Tracking was switched on for the department (so go-live does not remind every older booking), for
    equipment that receives samples, and not when the user already said "Sample Sent" through the older dialog."""
    from iic_booking.equipment.fabrication import is_fabrication_equipment
    from iic_booking.equipment.sample_lifecycle_policy import equipment_is_walk_in_sample

    if str(booking.status) not in ARRIVAL_REMINDER_BOOKING_STATUSES:
        return False
    if enabled_since is None or booking.created_at < enabled_since:
        return False
    if is_fabrication_equipment(booking.equipment) or equipment_is_walk_in_sample(booking.equipment):
        return False
    return not booking.sample_trace_events.exists()


def _condition(booking, arrival: SampleArrival | None, cfg: CourierTrackingSettings, enabled_since=None) -> str | None:
    if arrival is None:
        if not _arrival_reminder_applies(booking, enabled_since):
            return None
        if booking.created_at <= timezone.now() - timedelta(days=cfg.arrival_reminder_after_days):
            return ReminderKind.ARRIVAL_NOT_SELECTED
        return None
    if arrival.arrival_mode != ArrivalMode.COURIER:
        return None
    if arrival.last_problem in DETAIL_PROBLEM_OUTCOMES or arrival.status == ShipmentStatus.TRACKING_PROBLEM:
        return ReminderKind.DETAILS_INCOMPLETE
    if arrival.status in (ShipmentStatus.ATTEMPT_FAILED, ShipmentStatus.EXCEPTION):
        return ReminderKind.DELIVERY_PROBLEM
    stalled_since = timezone.now() - timedelta(days=cfg.stalled_after_days)
    if (
        arrival.status in (ShipmentStatus.INFO_RECEIVED, ShipmentStatus.PENDING, ShipmentStatus.IN_TRANSIT)
        and arrival.status_changed_at
        and arrival.status_changed_at < stalled_since
        and arrival.last_lookup_success_at  # we have looked recently enough to say "no progress"
    ):
        return ReminderKind.NOT_PROGRESSING
    return None


def run() -> dict:
    from iic_booking.equipment.models import Booking
    from iic_booking.users.models.user_type import UserType

    cfg = CourierTrackingSettings.load()
    counts: dict[str, int] = {}
    bookings = (
        Booking.objects.filter(status__in=ARRIVAL_OPEN_BOOKING_STATUSES, user_type_snapshot__in=UserType.get_external_user_codes())
        .select_related("user", "equipment")
        .order_by("booking_id")
    )
    enabled_since: dict[int, object] = {}
    for booking in bookings.iterator(chunk_size=200):
        # Same rule as registering a courier: department on, and in test-users-only mode only test users.
        if not booking_is_external(booking) or not department_tracking_enabled(booking):
            continue
        if _sample_already_received(booking):
            continue
        dept_id = booking.equipment.internal_department_id
        if dept_id not in enabled_since:
            enabled_since[dept_id] = tracking_enabled_since(dept_id)
        arrival = SampleArrival.objects.filter(booking=booking, is_active=True).first()
        kind = _condition(booking, arrival, cfg, enabled_since[dept_id])
        if kind is None:
            continue
        sequence = _due(booking, kind, cfg)
        if sequence is None:
            continue
        try:
            with transaction.atomic():
                reminder = ShipmentReminder.objects.create(
                    booking=booking, arrival=arrival, kind=kind, sequence=sequence, recipient=booking.user
                )
        except IntegrityError:
            continue  # another worker sent this one
        _send(reminder, booking, arrival)
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _send(reminder: ShipmentReminder, booking, arrival) -> None:
    from iic_booking.communication.service import CommunicationService
    from iic_booking.communication.utils import booking_display_id_for_email, get_frontend_absolute_url
    from iic_booking.users.display import get_user_display_name

    ref = booking_display_id_for_email(booking)
    context = {
        "user_name": get_user_display_name(booking.user) or "User",
        "booking_id": ref,
        "equipment_name": booking.equipment.name or booking.equipment.code,
        "message": MESSAGES[reminder.kind],
        "courier_name": getattr(arrival, "courier_name", "") or "",
        "tracking_number": getattr(arrival, "tracking_number", "") or "",
        "user_link": get_frontend_absolute_url(f"/courier-tracking?booking={ref}"),
    }
    try:
        log = CommunicationService.send_email(
            recipient=booking.user, template=TEMPLATE_REMINDER, template_context=context,
            metadata={"courier_reminder_id": reminder.pk, "kind": reminder.kind},
        )
        if log is not None and getattr(log, "status", "") == "failed":
            raise RuntimeError("email send failed")
        CommunicationService.send_push_notification(
            recipient=booking.user, title=f"Sample arrival - Booking {ref}", message=MESSAGES[reminder.kind],
            metadata={"notification_type": "info", "event": "courier_tracking.reminder",
                      "link": context["user_link"]},
        )
        ShipmentReminder.objects.filter(pk=reminder.pk).update(status=DeliveryStatus.SENT, sent_at=timezone.now())
    except Exception as exc:  # noqa: BLE001 - recorded; counts toward the limit so a broken mailbox is not spammed
        ShipmentReminder.objects.filter(pk=reminder.pk).update(
            status=DeliveryStatus.FAILED, last_error=f"{type(exc).__name__}"[:255]
        )
        logger.warning("Courier reminder %s failed", reminder.pk)
