"""Payment deadline, settlement and expiry for special-request allocations.

* Deadline (server-side only): ``min(approved_at + payment_window_hours, first allocated slot start)``.
* A payment counts only when the existing settlement paths confirm it (Razorpay signature/webhook settlement or
  Finance processing an offline receipt); starting a payment or a browser redirect never does.
* ``settlement_hook`` is called by those paths with the booking row locked. Payment realised by the deadline marks
  the allocation PAID. A gateway confirmation up to ``GATEWAY_GRACE`` after the deadline still counts (the order
  could only be opened before the deadline). Later money never revives the booking: it is recorded as a late payment
  for Finance (refund under existing rules; no automatic refund here).
* ``expire_overdue`` runs every few minutes. Each allocation is processed in its own transaction with the booking
  and allocation rows locked; payment is re-checked inside that transaction, so a confirmed payment always wins and
  a repeated or concurrent run does nothing twice.
* Released slots go back to the status they had before allocation (weekend / holiday / blocked rows stay closed),
  except that an equipment now under maintenance gets UNDER_MAINTENANCE and an AVAILABLE slot on a newly declared
  holiday becomes NOT_AVAILABLE.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from django.db import transaction
from django.utils import timezone

from . import notifications
from .models import AllocationState, ExternalServicesSettings, SpecialAllocation, SpecialRequestAction

logger = logging.getLogger(__name__)

GATEWAY_GRACE = timedelta(minutes=15)


def payment_deadline(approved_at, first_slot_start, *, window_hours: Optional[int] = None):
    hours = ExternalServicesSettings.load().payment_window_hours if window_hours is None else window_hours
    return min(approved_at + timedelta(hours=int(hours)), first_slot_start)


def allocation_payload(a: SpecialAllocation) -> dict:
    def iso(v):
        return timezone.localtime(v).isoformat() if v else None

    booking = a.booking
    return {
        "booking_id": booking.pk,
        "booking_reference": getattr(booking, "virtual_booking_id", "") or str(booking.pk),
        "booking_status": booking.status,
        "state": a.state,
        "state_label": a.get_state_display(),
        "approved_at": iso(a.approved_at),
        "slot_start": iso(a.slot_start),
        "slots": [
            {"start": iso(s.start_datetime), "end": iso(s.end_datetime)}
            for s in booking.daily_slots.order_by("start_datetime")
        ] if a.state in (AllocationState.AWAITING_PAYMENT, AllocationState.PAID) else [],
        "payment_deadline_at": iso(a.payment_deadline_at),
        "amount": f"{booking.total_charge:.2f}" if booking.total_charge is not None else None,
        "amount_due": f"{booking.amount_due:.2f}" if getattr(booking, "amount_due", None) is not None else None,
        "short_notice": a.short_notice,
        "short_notice_acknowledged_at": iso(a.short_notice_ack_at),
        "paid_at": iso(a.paid_at),
        "expired_at": iso(a.expired_at),
        "expiry_reason": a.expiry_reason,
        "late_payment_at": iso(a.late_payment_at),
    }


def _allocation_for(booking, *, lock: bool = False) -> Optional[SpecialAllocation]:
    """The booking's special allocation, if any. Read in a savepoint: before this app's migration has run (code is
    deployed before migrations) there can be no allocation, and ordinary payments must not fail."""
    from django.db import DatabaseError

    try:
        with transaction.atomic():
            qs = SpecialAllocation.objects.filter(booking_id=booking.pk)
            return (qs.select_for_update() if lock else qs).first()
    except DatabaseError:
        logger.warning("special allocations unavailable; treating booking %s as ordinary", booking.pk, exc_info=True)
        return None


# --- payment start ---------------------------------------------------------------------------------------------------

def payment_refusal(booking, *, at=None) -> Optional[dict]:
    """Error payload when the user may not start paying for this booking now, else None (and None for every
    booking that is not a special allocation, so ordinary payments are unaffected)."""
    a = _allocation_for(booking)
    if a is None:
        return None
    at = at or timezone.now()
    if a.state != AllocationState.AWAITING_PAYMENT:
        return {"error": "This booking is no longer awaiting payment.", "code": "SPECIAL_NOT_AWAITING_PAYMENT"}
    if at >= a.payment_deadline_at:
        return {
            "error": "The payment deadline for this booking has passed.",
            "code": "SPECIAL_PAYMENT_DEADLINE_PASSED",
            "payment_deadline_at": timezone.localtime(a.payment_deadline_at).isoformat(),
        }
    if a.short_notice and a.short_notice_ack_at is None:
        from . import policy
        from .models import PolicyKey

        return {
            "error": "Please accept the short-notice booking conditions before paying.",
            "code": "SHORT_NOTICE_ACK_REQUIRED",
            "policy": policy.policy_dict(policy.current(PolicyKey.SHORT_NOTICE)),
        }
    return None


# --- settlement ------------------------------------------------------------------------------------------------------

def settlement_hook(booking, *, realised_at=None, source: str = "gateway") -> bool:
    """Called by the settlement paths with ``booking`` locked, before they confirm it.

    Returns True when the booking may be confirmed as usual, False when it must not change status (the money is
    still recorded by the caller as received). ``realised_at``: when the payment was realised (gateway: now;
    offline receipt: when the user submitted it).
    """
    a = _allocation_for(booking, lock=True)
    if a is None:
        return True
    now = timezone.now()
    realised_at = realised_at or now
    if a.state == AllocationState.PAID:
        return True
    on_time = realised_at <= a.payment_deadline_at + (GATEWAY_GRACE if source == "gateway" else timedelta(0))
    if a.state == AllocationState.AWAITING_PAYMENT and on_time:
        a.state, a.paid_at = AllocationState.PAID, now
        a.save(update_fields=["state", "paid_at", "updated_at"])
        _event(a, SpecialRequestAction.PAID, details={"source": source, "realised_at": realised_at.isoformat()})
        notifications.notify(
            f"special:{a.request_id}:paid", [booking.user],
            title=f"Payment received for booking {_ref(booking)}",
            message=f"Your payment for the special booking on {booking.equipment.name} has been received. "
                    "The booking is confirmed.",
            link=f"/external-special-requests?request={a.request_id}",
        )
        return True
    if a.state == AllocationState.AWAITING_PAYMENT:
        # Paid too late: expire now (slots released) and keep the money on record for Finance.
        _expire_locked(a, booking, reason="Payment realised after the deadline.", now=now)
    _record_late_payment(a, booking, source=source, realised_at=realised_at)
    return False


def _record_late_payment(a, booking, *, source, realised_at) -> None:
    a.late_payment_at = timezone.now()
    a.late_payment_note = f"{source} payment realised {timezone.localtime(realised_at):%d %b %Y %H:%M} IST"[:255]
    a.save(update_fields=["late_payment_at", "late_payment_note", "updated_at"])
    _event(a, SpecialRequestAction.LATE_PAYMENT, details={"source": source, "realised_at": realised_at.isoformat()})
    from .special_requests import staff_for

    notifications.notify(
        f"special:{a.request_id}:late_payment", staff_for(booking.equipment) + _finance_for(booking),
        title=f"Late payment on cancelled booking {_ref(booking)}",
        message=(
            f"A payment for special booking {_ref(booking)} ({booking.equipment.name}) was realised after the "
            "payment deadline. The booking stays cancelled and its slots may have been given to someone else. "
            "Review the payment under the existing refund rules."
        ),
        link=f"/external-special-requests/manage?request={a.request_id}",
    )
    notifications.notify(
        f"special:{a.request_id}:late_payment:user", [booking.user],
        title=f"Payment received after the deadline for {_ref(booking)}",
        message=(
            "We received your payment after the payment deadline, so the booking could not be confirmed. "
            "The accounts office will contact you about the payment."
        ),
        link=f"/external-special-requests?request={a.request_id}",
    )


def _finance_for(booking) -> list:
    """Accounts In Charge of the equipment's department (FINANCE users of that department)."""
    from django.contrib.auth import get_user_model

    from iic_booking.users.models.user_type import UserType

    dept = getattr(booking.equipment, "internal_department_id", None)
    qs = get_user_model().objects.filter(user_type=UserType.FINANCE, is_active=True)
    if dept:
        qs = qs.filter(department_id=dept)
    return list(qs[:10])


def _ref(booking) -> str:
    return getattr(booking, "virtual_booking_id", "") or f"#{booking.pk}"


def _event(a, action, actor=None, reason: str = "", details=None):
    from .special_requests import record

    return record(a.request, action, actor, reason, **(details or {}))


# --- short-notice acknowledgement for the allocated slot -------------------------------------------------------------

def acknowledge_short_notice(user, request_id, version) -> SpecialAllocation:
    from . import policy
    from .models import PolicyKey
    from .special_requests import SpecialRequestError, _owned

    urg = _owned(user, request_id)
    with transaction.atomic():
        a = SpecialAllocation.objects.select_for_update().filter(request=urg).first()
        if a is None or a.state != AllocationState.AWAITING_PAYMENT:
            raise SpecialRequestError("There is no allocation awaiting payment.", code="invalid_state")
        if not a.short_notice:
            raise SpecialRequestError("This allocation does not need the short-notice acknowledgement.",
                                      code="not_needed")
        if a.short_notice_ack_at is None:
            try:
                p = policy.require_acknowledgement(PolicyKey.SHORT_NOTICE, version)
            except policy.PolicyError as exc:
                raise SpecialRequestError(exc.message, code=exc.code, field=exc.field, extra=exc.extra)
            a.short_notice_ack_at, a.short_notice_policy = timezone.now(), p
            a.save(update_fields=["short_notice_ack_at", "short_notice_policy", "updated_at"])
            _event(a, SpecialRequestAction.SHORT_NOTICE_ACKNOWLEDGED, user,
                   details={"policy_version": p.version, "stage": "allocation",
                            "text_sha256": policy.text_sha256(p.text)})
    return a


# --- expiry ----------------------------------------------------------------------------------------------------------

def _restore_status(slot, previous: Optional[str], equipment) -> str:
    from iic_booking.equipment.maintenance_policy import released_slot_status_after_booking_freed
    from iic_booking.equipment.models import Holiday, SlotStatus

    freed = released_slot_status_after_booking_freed(equipment)  # UNDER_MAINTENANCE while under maintenance
    if freed != SlotStatus.AVAILABLE:
        return freed
    previous = previous or SlotStatus.AVAILABLE
    if previous in (SlotStatus.BOOKED, SlotStatus.AVAILABLE):
        if slot.date and Holiday.objects.filter(date=slot.date, is_active=True).exists():
            return SlotStatus.NOT_AVAILABLE  # a holiday declared after the allocation
        return SlotStatus.AVAILABLE
    return previous


def _expire_locked(a: SpecialAllocation, booking, *, reason: str, now) -> list[int]:
    """Release the slots and cancel the booking. Caller holds the allocation and booking locks."""
    from iic_booking.equipment.booking_events import create_booking_event
    from iic_booking.equipment.cancellation_models import CancellationReason
    from iic_booking.equipment.models import BookingEventType, BookingStatus, DailySlot, UrgentBookingRequestStatus

    released = []
    for slot in DailySlot.objects.select_for_update().filter(booking=booking):
        new_status = _restore_status(slot, (a.previous_slot_statuses or {}).get(str(slot.pk)), booking.equipment)
        DailySlot.objects.filter(pk=slot.pk).update(booking=None, status=new_status)
        if new_status == "AVAILABLE":
            released.append(slot.pk)
    previous_status = booking.status
    booking.status = BookingStatus.CANCELLED
    booking.save(update_fields=["status", "updated_at"])
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.STATUS_CHANGED,
        created_by=None,
        system_actor=True,
        comment=f"Special booking cancelled: {reason}",
        previous_status=previous_status,
        new_status=BookingStatus.CANCELLED,
        metadata={"external_special_expired": True, "special_request_id": a.request_id},
        send_notification=False,
        cancellation={
            "actor": None,
            "system": True,
            "reason": CancellationReason.PAYMENT_DEADLINE_EXPIRED,
            "refund_amount": 0,
            "released_slot_ids": released,
        },
    )
    a.state, a.expired_at, a.expiry_reason = AllocationState.EXPIRED, now, reason[:255]
    a.save(update_fields=["state", "expired_at", "expiry_reason", "updated_at"])
    urg = a.request
    urg.status = UrgentBookingRequestStatus.EXPIRED
    urg.save(update_fields=["status"])
    _event(a, SpecialRequestAction.EXPIRED, reason=reason, details={"released_slot_ids": released})
    notifications.notify(
        f"special:{a.request_id}:expired", [booking.user],
        title=f"Special booking {_ref(booking)} cancelled: not paid in time",
        message=(
            f"The payment for your special booking on {booking.equipment.name} was not received by the deadline "
            f"({timezone.localtime(a.payment_deadline_at):%d %b %Y %I:%M %p} IST), so the slot has been released."
        ),
        link=f"/external-special-requests?request={a.request_id}",
    )
    from .special_requests import staff_for

    notifications.notify(
        f"special:{a.request_id}:expired:staff", staff_for(booking.equipment),
        title=f"Special booking {_ref(booking)} released (unpaid)",
        message=f"The special booking on {booking.equipment.name} was not paid by the deadline; its slots are free.",
        link=f"/external-special-requests/manage?request={a.request_id}",
        email=False,
    )
    if released:
        transaction.on_commit(lambda: _notify_waitlist(booking.equipment, released))
    return released


def _notify_waitlist(equipment, slot_ids) -> None:
    try:
        from iic_booking.equipment.waitlist import notify_waitlist_slots_available

        notify_waitlist_slots_available(equipment, preferred_slot_ids=slot_ids)
    except Exception:  # noqa: BLE001 - the waitlist mail is best effort, as for released urgent holds
        logger.warning("Waitlist notification failed after special expiry (equipment %s)", equipment.pk)


def _paid_by_gateway(booking) -> bool:
    from iic_booking.payments.models import PaymentOrderStatus

    return booking.razorpay_payment_orders.filter(status=PaymentOrderStatus.PAID).exists()


def expire_one(allocation_id: int, *, now=None) -> str:
    """Process one allocation; returns 'expired', 'paid', 'not_due' or 'skipped'."""
    from iic_booking.equipment.models import Booking

    now = now or timezone.now()
    booking_id = SpecialAllocation.objects.filter(pk=allocation_id).values_list("booking_id", flat=True).first()
    if booking_id is None:
        return "skipped"
    with transaction.atomic():
        # Same lock order as the settlement paths (booking, then allocation), so the two can never deadlock. A booking
        # locked by a settlement in progress is skipped; the next run sees the outcome.
        booking = Booking.objects.select_for_update(skip_locked=True).filter(pk=booking_id).first()
        if booking is None:
            return "skipped"
        a = (
            SpecialAllocation.objects.select_for_update(skip_locked=True)
            .select_related("request")
            .filter(pk=allocation_id, state=AllocationState.AWAITING_PAYMENT)
            .first()
        )
        if a is None:
            return "skipped"  # done already, or another worker holds it
        if now < a.payment_deadline_at + GATEWAY_GRACE:
            return "not_due"
        if booking.payment_settled_at or _paid_by_gateway(booking):
            # Paid: the settlement already confirmed it (or is about to); never cancel a paid booking.
            a.state, a.paid_at = AllocationState.PAID, a.paid_at or now
            a.save(update_fields=["state", "paid_at", "updated_at"])
            return "paid"
        _expire_locked(a, booking, reason="Payment not realised by the deadline.", now=now)
        return "expired"


def expire_overdue(now=None) -> dict:
    now = now or timezone.now()
    ids = list(
        SpecialAllocation.objects.filter(
            state=AllocationState.AWAITING_PAYMENT, payment_deadline_at__lte=now - GATEWAY_GRACE
        ).values_list("pk", flat=True)[:500]
    )
    counts: dict[str, int] = {}
    for pk in ids:
        try:
            outcome = expire_one(pk, now=now)
        except Exception:  # noqa: BLE001 - one bad row must not stop the others; logged for the admin
            logger.exception("Special allocation %s expiry failed", pk)
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts
