"""Shipment messages, through the portal's existing email (CommunicationService) and in-app notifications.

Every message is a ``ShipmentNotification`` row keyed by (arrival, kind, state, recipient, channel), created before it
is sent, so a repeated event or a retried webhook cannot send it twice, and a failed send is retried by the sweep.

* Users: in-app for each meaningful status once; email only if they ticked "Email me when the courier tracking status
  changes" (and status emails are enabled). Never for MANUAL arrivals.
* Staff: when a delivery is reported, the equipment's Officer(s) In Charge (incl. temporary OIC), Lab Operators (lab
  in-charge) and the department's Accounts In Charge are emailed (always, whatever the user chose) and, if enabled,
  notified in the portal. A missing role is recorded as an administrative warning. A reported delivery never marks
  the sample as received.
"""

from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .alerts import raise_alert
from .constants import ShipmentStatus
from .models import (
    AlertKind,
    CourierTrackingSettings,
    DeliveryStatus,
    NotificationChannel,
    SampleArrival,
    ShipmentNotification,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5

TEMPLATE_REGISTERED = "courier_shipment_registered_user"
TEMPLATE_STATUS = "courier_status_update_user"
TEMPLATE_STAFF_DELIVERED = "courier_delivery_reported_staff"

KIND_REGISTERED = "user_registered"
KIND_STATUS = "user_status"
KIND_STAFF_DELIVERED = "staff_delivery_reported"

# Statuses worth telling the user about, with the in-app wording.
USER_STATUS_MESSAGES = {
    ShipmentStatus.IN_TRANSIT: "Your sample shipment is in transit.",
    ShipmentStatus.OUT_FOR_DELIVERY: "Your sample shipment is out for delivery.",
    ShipmentStatus.DELIVERY_REPORTED: (
        "The courier reports your sample shipment as delivered. The lab will confirm once it has the sample."
    ),
    ShipmentStatus.ATTEMPT_FAILED: "The courier reports a delivery attempt that did not succeed.",
    ShipmentStatus.EXCEPTION: "The courier reports a problem with your sample shipment.",
    ShipmentStatus.RETURNED: "The courier reports your sample shipment as returned to the sender.",
    ShipmentStatus.CANCELLED: "The courier reports your sample shipment as cancelled.",
}


# -- context ---------------------------------------------------------------------------------------------------------

def _context(arrival: SampleArrival) -> dict:
    from iic_booking.communication.utils import booking_display_id_for_email, get_frontend_absolute_url
    from iic_booking.users.display import get_user_display_name

    booking = arrival.booking
    ref = booking_display_id_for_email(booking)
    latest = arrival.latest_checkpoint or {}
    return {
        "booking_id": ref,
        "user_name": get_user_display_name(booking.user) or "User",
        "equipment_name": getattr(arrival.equipment, "name", "") or getattr(arrival.equipment, "code", ""),
        "department_name": getattr(arrival.department, "name", "") or "",
        "courier_name": arrival.courier_name,
        "tracking_number": arrival.tracking_number,
        "status_label": arrival.get_status_display() if arrival.status else "",
        "provider_status": arrival.provider_status_label,
        "event_time": " ".join(x for x in (latest.get("date", ""), latest.get("time", "")) if x),
        "event_location": latest.get("location", ""),
        "sample_identifiers": arrival.sample_identifiers,
        "submitted_by": get_user_display_name(booking.user) or "",
        "submitted_by_email": getattr(booking.user, "email", "") or "",
        "user_link": get_frontend_absolute_url(f"/courier-tracking?booking={ref}"),
        "staff_link": get_frontend_absolute_url(f"/booking-management?booking={ref}"),
        "delay_note": "Courier updates can take several hours to appear.",
    }


# -- recipients ------------------------------------------------------------------------------------------------------

def staff_recipients(arrival: SampleArrival) -> tuple[list, list[str]]:
    """(users, missing role labels) for a reported delivery. Users are deduplicated across roles."""
    from iic_booking.equipment.models import EquipmentManager, EquipmentOperator, EquipmentTemporaryOIC
    from iic_booking.users.models import User
    from iic_booking.users.models.user_type import UserType

    equipment = arrival.equipment
    now = timezone.now()
    oic_ids = set(EquipmentManager.objects.filter(equipment=equipment).values_list("manager_id", flat=True))
    oic_ids |= set(
        EquipmentTemporaryOIC.objects.active(now).filter(equipment=equipment).values_list("temporary_oic_id", flat=True)
    )
    lab_ids = set(EquipmentOperator.objects.filter(equipment=equipment).values_list("operator_id", flat=True))
    finance = User.objects.filter(is_active=True, user_type=UserType.FINANCE)
    accounts_ids = set(finance.filter(department_id=arrival.department_id).values_list("id", flat=True)) if arrival.department_id else set()
    if not accounts_ids:
        # Same fallback as the booking page's "Accounts In Charge": any finance user.
        first = finance.order_by("id").values_list("id", flat=True).first()
        accounts_ids = {first} if first else set()
    missing = []
    active = set(User.objects.filter(id__in=oic_ids | lab_ids | accounts_ids, is_active=True).values_list("id", flat=True))
    if not (oic_ids & active):
        missing.append("Officer In Charge")
    if not (lab_ids & active):
        missing.append("Lab in-charge (Lab Operator)")
    if not (accounts_ids & active):
        missing.append("Accounts In Charge")
    users = list(User.objects.filter(id__in=active).order_by("id"))
    return users, missing


# -- queueing --------------------------------------------------------------------------------------------------------

def _queue(arrival: SampleArrival, kind: str, state_key: str, recipients, channels, context: dict) -> list[int]:
    ids: list[int] = []
    for user in recipients:
        for channel in channels:
            try:
                with transaction.atomic():
                    row, created = ShipmentNotification.objects.get_or_create(
                        arrival=arrival, kind=kind, state_key=state_key[:64], recipient=user, channel=channel,
                        defaults={"context": context},
                    )
            except IntegrityError:
                continue
            if created:
                ids.append(row.pk)
    if ids:
        transaction.on_commit(lambda: dispatch(ids))
    return ids


def dispatch(ids: list[int]) -> None:
    """Hand to Celery; if the broker is unreachable the rows stay PENDING and the sweep sends them."""
    try:
        from .tasks import send_notifications_task

        send_notifications_task.delay(ids)
    except Exception:  # noqa: BLE001
        logger.warning("Courier notification queue unavailable; %s message(s) left for the sweep", len(ids))


def on_registered(arrival: SampleArrival) -> None:
    if not arrival.is_courier:
        return
    cfg = CourierTrackingSettings.load()
    channels = [NotificationChannel.IN_APP]
    if arrival.email_updates and cfg.user_status_emails_enabled:
        channels.append(NotificationChannel.EMAIL)
    _queue(arrival, KIND_REGISTERED, "registered", [arrival.booking.user], channels, _context(arrival))


def on_status_change(arrival: SampleArrival, old_status: str) -> None:
    """Queue the messages for a status the arrival just reached (no-op for unchanged or minor states)."""
    if not arrival.is_courier or arrival.status == old_status:
        return
    cfg = CourierTrackingSettings.load()
    context = _context(arrival)
    if arrival.status in USER_STATUS_MESSAGES:
        channels = [NotificationChannel.IN_APP]
        if arrival.email_updates and cfg.user_status_emails_enabled:
            channels.append(NotificationChannel.EMAIL)
        _queue(arrival, KIND_STATUS, str(arrival.status), [arrival.booking.user], channels,
               {**context, "message": USER_STATUS_MESSAGES[arrival.status]})
    if arrival.status == ShipmentStatus.DELIVERY_REPORTED:
        users, missing = staff_recipients(arrival)
        if missing:
            raise_alert(
                AlertKind.RECIPIENT_MISSING,
                f"recipient:{arrival.pk}:{'|'.join(missing)}",
                f"Delivery reported for booking {context['booking_id']} but no {', '.join(missing)} could be found "
                f"for {context['equipment_name']}. Check the equipment's staff assignments.",
                arrival=arrival,
            )
        channels = [NotificationChannel.EMAIL] + ([NotificationChannel.IN_APP] if cfg.staff_delivery_in_app else [])
        _queue(arrival, KIND_STAFF_DELIVERED, "delivered", users, channels, context)


# -- sending ---------------------------------------------------------------------------------------------------------

def _template_for(kind: str) -> str:
    return {KIND_REGISTERED: TEMPLATE_REGISTERED, KIND_STATUS: TEMPLATE_STATUS,
            KIND_STAFF_DELIVERED: TEMPLATE_STAFF_DELIVERED}[kind]


def _in_app_text(row: ShipmentNotification) -> tuple[str, str, str]:
    ctx = row.context or {}
    if row.kind == KIND_STAFF_DELIVERED:
        return (
            f"Shipment reported delivered - Booking {ctx.get('booking_id', '')}",
            f"{ctx.get('courier_name', '')} {ctx.get('tracking_number', '')} for {ctx.get('equipment_name', '')} is "
            "reported delivered by the courier. Verify physical receipt and update the sample status.",
            ctx.get("staff_link", ""),
        )
    if row.kind == KIND_REGISTERED:
        return (
            f"Courier shipment registered - Booking {ctx.get('booking_id', '')}",
            f"We will follow {ctx.get('courier_name', '')} {ctx.get('tracking_number', '')}. {ctx.get('delay_note', '')}",
            ctx.get("user_link", ""),
        )
    return (f"Shipment update - Booking {ctx.get('booking_id', '')}", ctx.get("message", ""), ctx.get("user_link", ""))


def send_one(pk: int) -> str:
    """Send one queued message. Safe to call repeatedly: a sent row is never sent again."""
    with transaction.atomic():
        row = (
            ShipmentNotification.objects.select_for_update(skip_locked=True)
            .select_related("recipient")
            .filter(pk=pk, status__in=[DeliveryStatus.PENDING, DeliveryStatus.FAILED])
            .first()
        )
        if row is None:
            return "skipped"
        row.attempts += 1
        try:
            if row.channel == NotificationChannel.EMAIL:
                from iic_booking.communication.service import CommunicationService

                if not (row.recipient.email or "").strip():
                    row.status = DeliveryStatus.SKIPPED
                    row.last_error = "recipient has no email address"
                    row.save(update_fields=["status", "attempts", "last_error"])
                    return "skipped"
                log = CommunicationService.send_email(
                    recipient=row.recipient,
                    template=_template_for(row.kind),
                    template_context=row.context,
                    metadata={"courier_notification_id": row.pk, "arrival_id": row.arrival_id, "kind": row.kind},
                )
                if log is None:
                    # The recipient's own notification settings declined it (courier_* templates are in no category,
                    # so this only happens if that changes).
                    row.status = DeliveryStatus.SKIPPED
                    row.last_error = "declined by recipient notification settings"
                    row.save(update_fields=["status", "attempts", "last_error"])
                    return "skipped"
                if getattr(log, "status", "") == "failed":
                    raise RuntimeError(f"email send failed: {(getattr(log, 'error_message', '') or '')[:150]}")
            else:
                from iic_booking.communication.service import CommunicationService

                title, message, link = _in_app_text(row)
                CommunicationService.send_push_notification(
                    recipient=row.recipient, title=title[:250], message=message,
                    metadata={"notification_type": "info", "event": f"courier_tracking.{row.kind}", "link": link},
                )
        except Exception as exc:  # noqa: BLE001 - recorded and retried
            row.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            row.status = DeliveryStatus.GAVE_UP if row.attempts >= MAX_ATTEMPTS else DeliveryStatus.FAILED
            row.save(update_fields=["status", "attempts", "last_error"])
            if row.status == DeliveryStatus.GAVE_UP:
                raise_alert(AlertKind.NOTIFICATION_FAILED, f"notification:{row.pk}",
                            f"A courier tracking {row.get_channel_display().lower()} to user #{row.recipient_id} "
                            f"failed {row.attempts} times ({row.kind}).", arrival=row.arrival)
            logger.warning("Courier notification %s failed (attempt %s)", row.pk, row.attempts)
            return "failed"
        row.status = DeliveryStatus.SENT
        row.sent_at = timezone.now()
        row.last_error = ""
        row.save(update_fields=["status", "attempts", "last_error", "sent_at"])
    return "sent"


def sweep(limit: int = 200) -> dict:
    """Send messages left PENDING (queue down) or FAILED earlier. Called periodically."""
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(minutes=2)
    ids = list(
        ShipmentNotification.objects.filter(status__in=[DeliveryStatus.PENDING, DeliveryStatus.FAILED], created_at__lt=cutoff)
        .order_by("created_at")
        .values_list("pk", flat=True)[:limit]
    )
    counts: dict[str, int] = {}
    for pk in ids:
        result = send_one(pk)
        counts[result] = counts.get(result, 0) + 1
    return counts
