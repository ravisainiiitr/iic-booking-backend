"""Undo automatic Booking Not Utilized applied before the equipment's Booking Not Utilize Window.

A booking qualifies when its latest change to Booking Not Utilized was made by the scheduled check
before ``last slot end + window`` (window as the corrected check applies it) and that time has still not
come, so the corrected check would not mark it today. Restoring sets the booking and its slots back to
Booked, removes the automatic Not Utilized sample-trace row, writes a system history entry and emails the
booking user and the wallet owner / supervisor once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from iic_booking.communication.email_branding import format_local_dt, user_display_name
from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import booking_display_id_for_email, get_frontend_absolute_url

from .booking_events import BookingEventType, create_booking_event
from .booking_not_utilized_service import auto_not_utilized_hours, not_utilized_window_hours
from .models import Booking, BookingEvent, BookingSampleTrace, BookingStatus, SampleTraceStatus, SlotStatus

logger = logging.getLogger(__name__)

AUTO_EVENT_COMMENT_PREFIX = "Automatically marked as Booking Not Utilized"
RESTORE_MARKER = "not_utilized_window_restore"
EMAILS_SENT_KEY = "restore_emails_sent_at"
USER_TEMPLATE = "booking_not_utilized_restored_email"
OWNER_TEMPLATE = "booking_not_utilized_restored_wallet_owner_email"


@dataclass
class RestoreCandidate:
    booking: Booking
    auto_event: BookingEvent
    window_hours: int
    last_slot_end: object
    allowed_from: Optional[object]


def _aware(dt):
    if dt is not None and timezone.is_naive(dt):
        return timezone.make_aware(dt)
    return dt


def _latest_not_utilized_event(booking) -> Optional[BookingEvent]:
    return (
        BookingEvent.objects.filter(booking=booking, new_status=BookingStatus.BOOKING_NOT_UTILIZED)
        .order_by("-created_at", "-event_id")
        .first()
    )


def evaluate(booking: Booking, now=None) -> Optional[RestoreCandidate]:
    """RestoreCandidate when the booking was wrongly marked by the scheduled check, else None."""
    now = now or timezone.now()
    if booking.status != BookingStatus.BOOKING_NOT_UTILIZED:
        return None
    event = _latest_not_utilized_event(booking)
    if event is None or event.created_by_id is not None:
        return None
    if not (event.comment or "").startswith(AUTO_EVENT_COMMENT_PREFIX):
        return None
    last_end = _aware(booking.daily_slots.aggregate(m=Max("end_datetime"))["m"])
    if last_end is None:
        return None
    statuses = set(booking.daily_slots.values_list("status", flat=True))
    if statuses != {SlotStatus.BOOKING_NOT_UTILIZED}:
        return None
    hours = auto_not_utilized_hours(booking.equipment)
    allowed_from = last_end + timedelta(hours=hours) if hours is not None else None
    if allowed_from is not None and (event.created_at >= allowed_from or now >= allowed_from):
        return None
    return RestoreCandidate(
        booking=booking,
        auto_event=event,
        window_hours=not_utilized_window_hours(booking.equipment),
        last_slot_end=last_end,
        allowed_from=allowed_from,
    )


def find_candidates(now=None) -> list[RestoreCandidate]:
    now = now or timezone.now()
    booking_ids = (
        BookingEvent.objects.filter(
            new_status=BookingStatus.BOOKING_NOT_UTILIZED,
            created_by__isnull=True,
            comment__startswith=AUTO_EVENT_COMMENT_PREFIX,
        )
        .values_list("booking_id", flat=True)
        .distinct()
    )
    out = []
    for booking in (
        Booking.objects.filter(pk__in=list(booking_ids), status=BookingStatus.BOOKING_NOT_UTILIZED)
        .select_related("equipment", "user")
        .order_by("pk")
    ):
        candidate = evaluate(booking, now)
        if candidate is not None:
            out.append(candidate)
    return out


def restore_event(booking) -> Optional[BookingEvent]:
    return (
        BookingEvent.objects.filter(booking=booking, metadata__has_key=RESTORE_MARKER)
        .order_by("-created_at", "-event_id")
        .first()
    )


def restore_booking(candidate: RestoreCandidate) -> Optional[BookingEvent]:
    """Restore one booking to Booked. Returns the history event, or None if its state changed meanwhile."""
    with transaction.atomic():
        locked = Booking.objects.select_for_update().select_related("equipment", "user").get(pk=candidate.booking.pk)
        if evaluate(locked) is None:
            return None
        auto_traces = list(
            BookingSampleTrace.objects.filter(
                booking=locked,
                status=SampleTraceStatus.NOT_UTILIZED,
                created_by__isnull=True,
                reason__startswith="Automatically marked as Booking Not Utilized",
            ).values_list("id", flat=True)
        )
        BookingSampleTrace.objects.filter(id__in=auto_traces).delete()
        restored_slots = locked.daily_slots.filter(status=SlotStatus.BOOKING_NOT_UTILIZED).update(
            status=SlotStatus.BOOKED
        )
        locked.status = BookingStatus.BOOKED
        locked.save(update_fields=["status"])
        window = candidate.window_hours
        return create_booking_event(
            booking=locked,
            event_type=BookingEventType.STATUS_CHANGED,
            previous_status=BookingStatus.BOOKING_NOT_UTILIZED,
            new_status=BookingStatus.BOOKED,
            comment=(
                f"Restored to Booked: automatic Not Utilized was applied before the equipment's {window}-hour "
                "window — system correction"
            ),
            created_by=None,
            system_actor=True,
            send_notification=False,
            metadata={
                RESTORE_MARKER: True,
                "auto_not_utilized_event_id": candidate.auto_event.event_id,
                "removed_sample_trace_ids": auto_traces,
                "restored_slot_count": restored_slots,
                "window_hours": window,
            },
        )


def _email_context(booking) -> dict:
    from .booking_events import apply_booking_party_to_context

    equipment = booking.equipment
    slot_parts = []
    for start, end, day in booking.daily_slots.order_by("start_datetime").values_list(
        "start_datetime", "end_datetime", "date"
    ):
        part = str(day)
        if start and end:
            part += f" {format_local_dt(start, '%H:%M')}-{format_local_dt(end, '%H:%M')}"
        slot_parts.append(part)
    ref = booking_display_id_for_email(booking)
    ctx = {
        "user_name": user_display_name(booking.user),
        "equipment_name": (getattr(equipment, "name", None) or getattr(equipment, "code", None) or "Equipment"),
        "slot_details": "; ".join(slot_parts),
        "booking_id": ref,
        "new_status": "Booked",
        "link": get_frontend_absolute_url(f"/my-bookings?booking={ref}"),
    }
    apply_booking_party_to_context(ctx, booking)
    return ctx


def _ensure_templates() -> None:
    from iic_booking.equipment.booking_lab_messages import ensure_email_template

    ensure_email_template(USER_TEMPLATE)
    ensure_email_template(OWNER_TEMPLATE)


def send_restore_emails(booking, event: BookingEvent) -> int:
    """Email user and wallet owner at most once each per restore event (marker kept on the event).

    Returns the number of emails sent by this call; a failed send is retried on the next call.
    """
    md = dict(event.metadata or {})
    sent_to = dict(md.get(EMAILS_SENT_KEY) or {})
    user = booking.user
    try:
        wallet = user.get_accessible_wallet()
    except Exception:
        wallet = None
    owner = getattr(wallet, "user", None) if wallet is not None else None
    jobs = [("user", user, USER_TEMPLATE, {})]
    if owner is not None and owner.pk != user.pk:
        jobs.append(
            (
                "wallet_owner",
                owner,
                OWNER_TEMPLATE,
                {"student_name": user_display_name(user, fallback="Student"), "wallet_owner_name": user_display_name(owner)},
            )
        )
    jobs = [j for j in jobs if j[0] not in sent_to]
    if not jobs:
        return 0
    _ensure_templates()
    ctx = _email_context(booking)
    sent = 0
    for role, recipient, template, extra in jobs:
        try:
            log = CommunicationService.send_email(
                recipient=recipient, template=template, template_context={**ctx, **extra}
            )
        except Exception:
            logger.exception("restore email failed booking_id=%s role=%s", booking.pk, role)
            continue
        if log is not None and log.status == log.CommunicationStatus.SENT:
            sent_to[role] = timezone.now().isoformat()
            sent += 1
    md[EMAILS_SENT_KEY] = sent_to
    event.metadata = md
    event.save(update_fields=["metadata"])
    return sent
