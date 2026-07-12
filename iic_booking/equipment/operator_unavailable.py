"""Shared logic for marking a booking as Operator Unavailable (ABSENT) with full refund."""

import logging

from django.db import transaction

from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import booking_display_id_for_email
from iic_booking.users.repositories.wallet_repository import WalletRepository

from .booking_events import create_booking_event
from .maintenance_policy import released_slot_status_after_booking_freed
from .models import BookingEventType, BookingSampleTrace, BookingStatus, SampleTraceStatus
from .waitlist import notify_waitlist_slots_available
from .waitlist_booking import _student_booking_description_suffix

logger = logging.getLogger(__name__)


def apply_operator_unavailable_booking(booking, *, notes: str = "", actor):
    """
    Mark booking ABSENT, free slots, full wallet refund, notify user (operator unavailable email).

    Args:
        booking: Booking with user and equipment loaded (select_related recommended).
        notes: Optional operator notes (appended to booking.notes).
        actor: User who triggered the action, or None for system jobs.

    Raises:
        ValueError: If booking cannot be processed (wrong status, no wallet, etc.).
    """
    if booking.status not in [BookingStatus.PENDING, BookingStatus.BOOKED]:
        raise ValueError(
            f"Cannot mark booking as operator unavailable with status '{booking.status}'. "
            "Only PENDING or BOOKED bookings can be marked."
        )

    refund_target, _ = WalletRepository.get_booking_wallet_target(
        booking.user, getattr(booking.equipment, "internal_department", None)
    )
    if not refund_target:
        raise ValueError("User does not have a wallet. Cannot process refund for operator unavailable.")

    absent_notes = notes or ""
    if absent_notes:
        booking.notes = f"{booking.notes or ''}\n[Operator Unavailable]: {absent_notes}".strip()
        booking.save(update_fields=["notes"])

    slots_ordered = list(booking.daily_slots.order_by("start_datetime"))
    start_dt = slots_ordered[0].start_datetime if slots_ordered else None
    end_dt = slots_ordered[-1].end_datetime if slots_ordered else None
    start_time_str = start_dt.strftime("%Y-%m-%d %H:%M") if start_dt else ""
    end_time_str = end_dt.strftime("%Y-%m-%d %H:%M") if end_dt else ""

    refund_description = f"Refund (Operator Unavailable) for Booking #{booking.booking_id} - {booking.equipment.code}"
    if absent_notes:
        refund_description += f" - {absent_notes}"
    refund_description += _student_booking_description_suffix(refund_target, booking.user)

    released_slot_ids = list(booking.daily_slots.values_list("id", flat=True))
    previous_status = booking.status
    with transaction.atomic():
        booking.daily_slots.update(
            booking=None,
            status=released_slot_status_after_booking_freed(booking.equipment),
        )
        refund_transaction = refund_target.credit(
            amount=booking.total_charge,
            description=refund_description,
            related_user=booking.user,
        )
        booking.status = BookingStatus.ABSENT
        booking.save(update_fields=["status"])

        create_booking_event(
            booking=booking,
            event_type=BookingEventType.ABSENT,
            previous_status=previous_status,
            new_status=BookingStatus.ABSENT,
            comment=absent_notes or "Operator unavailable. Full refund issued.",
            created_by=actor,
            metadata={"refund_amount": str(booking.total_charge)},
            send_notification=False,
        )

        trace_reason = (absent_notes or "").strip() or "Operator unavailable. Full refund issued."
        BookingSampleTrace.objects.create(
            booking=booking,
            status=SampleTraceStatus.OP_UNAVAILABLE,
            reason=trace_reason,
            created_by=actor,
        )

        if refund_transaction:
            from iic_booking.communication.wallet_notifications import send_sub_wallet_transaction_notifications

            try:
                send_sub_wallet_transaction_notifications(
                    transaction=refund_transaction,
                    booking=booking,
                )
            except Exception as e:
                logger.error("Failed to send wallet transaction notification: %s", e, exc_info=True)

    try:
        notify_waitlist_slots_available(
            booking.equipment,
            preferred_slot_ids=released_slot_ids,
            respect_reschedule_threshold=True,
        )
    except Exception as e:
        logger.warning(
            "Failed to notify waitlist after operator unavailable for equipment %s: %s",
            booking.equipment.code,
            e,
        )

    equipment_name = getattr(booking.equipment, "name", None) or getattr(booking.equipment, "code", None) or "Equipment"
    user = booking.user
    ctx = {
        "user_name": getattr(user, "name", None) or getattr(user, "email", None) or "User",
        "user_email": getattr(user, "email", "") or "",
        "booking_id": booking_display_id_for_email(booking),
        "equipment_name": equipment_name,
        "equipment_code": getattr(booking.equipment, "code", "") or "",
        "start_time": start_time_str,
        "end_time": end_time_str,
        "refund_amount": str(booking.total_charge),
        "comment": absent_notes or "Operator was unavailable. A full refund has been issued to your wallet.",
    }
    try:
        CommunicationService.send_email(
            recipient=user,
            template="operator_unavailable_email",
            template_context=ctx,
            created_by=actor,
        )
    except Exception as e:
        logger.warning("Failed to send operator unavailable email to %s: %s", getattr(booking.user, "email", ""), e)
