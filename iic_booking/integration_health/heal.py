"""Safe automatic fixes run with the health checks. Each one only repeats what the portal itself would do, and only
where repeating cannot create a duplicate:

* I-STEM: run the sync (queue newly paid / completed / cancelled bookings and process due jobs), and re-send a Create
  Booking that I-STEM definitely refused because the hour was taken (Create now moves to the next free hour).
* Payments: close "authorised but not captured" issues whose payment was in fact captured and recorded.
* WhatsApp: send again messages stuck in the queue, and, once Meta reports the number can send again, messages Meta
  refused for a reason that has since cleared (e.g. 131037 display name not yet approved). Only messages from the last
  48 hours, and each message at most once by this route.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

RESEND_WINDOW = timedelta(hours=48)
# Meta refusals that clear on Meta's side (account or rate state), not because of the message or the recipient.
RESENDABLE_CODES = {"131037", "130429", "131016", "131000", "131048", "131056", "2", "4", "80007"}
RESENT_KEY = "integration_health:whatsapp_resent:{pk}"


def heal_istem() -> dict:
    from iic_booking.istem import conf
    from iic_booking.istem.tasks import run_istem_sync

    if not conf.automatic_fbr_enabled() or conf.missing_live_settings():
        return {"skipped": True}
    retried = retry_slot_refused_creates()
    summary = run_istem_sync()
    return {"retried_creates": retried, **(summary or {})}


def retry_slot_refused_creates(limit: int = 50) -> int:
    """CREATE_FAILED links whose latest Create Booking was a definite "slot already booked" refusal and that hold no
    I-STEM request_id: back to CREATE_READY, so Create asks for the next free hour."""
    from iic_booking.istem import contract
    from iic_booking.istem.choices import AttemptOutcome, Operation, SyncState
    from iic_booking.istem.models import IstemApiAttempt, IstemBookingLink
    from iic_booking.istem.outbox import add_job, record_event

    done = 0
    links = IstemBookingLink.objects.filter(sync_state=SyncState.CREATE_FAILED, request_id__isnull=True,
                                            booking__created_at__gte=timezone.now() - timedelta(days=30))[:limit]
    for link in links:
        last = IstemApiAttempt.objects.filter(booking_link=link, operation=Operation.CREATE_BOOKING).order_by("-pk").first()
        # The refusal text is kept on the link ("I-STEM refused the booking: This slot is already booked.").
        if last is None or last.outcome != AttemptOutcome.DEFINITE_FAILURE or not contract.slot_occupied(link.review_reason or ""):
            continue
        with transaction.atomic():
            locked = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
            if locked.sync_state != SyncState.CREATE_FAILED or locked.request_id:
                continue
            record_event(booking_link=locked, from_state=locked.sync_state, to_state=SyncState.CREATE_READY,
                         reason="integration health: retry after I-STEM said the hour was taken")
            locked.sync_state = SyncState.CREATE_READY
            locked.needs_review, locked.review_reason = False, ""
            locked.save(update_fields=["sync_state", "needs_review", "review_reason", "updated_at"])
            add_job(Operation.CREATE_BOOKING, dedupe_key=f"CREATE_BOOKING:{locked.booking_id}",
                    trigger="integration health retry", booking_link=locked)
        done += 1
    return done


def heal_whatsapp(sender_state: dict | None) -> dict:
    from iic_booking.whatsapp import conf
    from iic_booking.whatsapp.models import WhatsAppMessage
    from iic_booking.whatsapp.service import _enqueue

    from .checks import WHATSAPP_QUEUE_GRACE, meta_code

    if not conf.enabled():
        return {"skipped": True}
    now = timezone.now()
    requeued = 0
    # Stuck in the queue: the worker missed them. deliver() is a no-op for anything no longer QUEUED.
    for pk in WhatsAppMessage.objects.filter(status=WhatsAppMessage.Status.QUEUED, created_at__lt=now - WHATSAPP_QUEUE_GRACE,
                                             created_at__gte=now - RESEND_WINDOW).values_list("pk", flat=True)[:100]:
        _enqueue(pk)
        requeued += 1
    resent = 0
    if (sender_state or {}).get("can_send") == "AVAILABLE":
        refused = WhatsAppMessage.objects.filter(status=WhatsAppMessage.Status.FAILED, created_at__gte=now - RESEND_WINDOW)
        for msg in refused.order_by("created_at")[:200]:
            code = meta_code(msg.error)
            if code not in RESENDABLE_CODES and not msg.error.startswith("gave up after retries"):
                continue
            key = RESENT_KEY.format(pk=msg.pk)
            if not cache.add(key, 1, timeout=int(RESEND_WINDOW.total_seconds()) * 2):
                continue  # already resent once by this route
            updated = WhatsAppMessage.objects.filter(pk=msg.pk, status=WhatsAppMessage.Status.FAILED).update(
                status=WhatsAppMessage.Status.QUEUED, error=f"resent after: {msg.error}"[:2000], updated_at=now)
            if updated:
                transaction.on_commit(lambda pk=msg.pk: _enqueue(pk))
                resent += 1
    return {"requeued": requeued, "resent": resent, "sender": (sender_state or {}).get("can_send", "")}


def close_captured_payment_issues() -> int:
    """Open "authorised payment could not be captured" issues whose payment Razorpay did capture and the portal
    recorded on a paid order: closed with the same note the payment flow uses since #234 (issues raised before that
    fix stayed open). Nothing else is touched."""
    from iic_booking.payments.models import (
        Payment, PaymentIssue, PaymentIssueKind, PaymentIssueStatus, PaymentOrderStatus, PaymentStatus,
    )

    captured = Payment.objects.filter(status__in=[PaymentStatus.CAPTURED, PaymentStatus.REFUNDED,
                                                  PaymentStatus.PARTIALLY_REFUNDED],
                                      payment_order__status=PaymentOrderStatus.PAID).values_list("razorpay_payment_id", flat=True)
    return PaymentIssue.objects.filter(kind=PaymentIssueKind.NOT_CAPTURED, status=PaymentIssueStatus.OPEN,
                                       razorpay_payment_id__in=list(captured)).update(
        status=PaymentIssueStatus.RESOLVED, resolved_at=timezone.now(),
        resolution_note="Closed automatically: Razorpay captured the payment and it was recorded.")


def heal_all(sender_state: dict | None) -> dict:
    out = {}
    for name, fn in (("istem", heal_istem), ("whatsapp", lambda: heal_whatsapp(sender_state)),
                     ("payments", lambda: {"closed_capture_issues": close_captured_payment_issues()})):
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 - one failing fix must not stop the others
            logger.exception("integration health: %s fix failed", name)
            out[name] = {"error": type(exc).__name__}
    return out
