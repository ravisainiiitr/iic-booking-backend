"""Weekly batch to SRIC: GST invoice requests and bank refunds (09:30 IST, first working day of the week).

* Working days: Monday-Friday that are not an active ``Holiday`` (the calendar the results-deadline rules use). The
  batch day is the first working day of the ISO week (31 Oct 2026 committee: weekly instead of daily).
* ``run_batch`` is scheduled at 09:30 and every 30 minutes until 17:00 on weekdays. The first run on the batch day
  opens that week's batch (unique per date) and takes every invoice request and bank refund queued before it; later
  runs, that day or later in the week, only retry. Requests are never lost because they live in the database.
* Bank refunds of the batch go to the same SRIC address in one email (``sric_refunds``).
* Each request is claimed by compare-and-swap (BATCHED/FAILED -> SENDING) before its email is sent, and the email
  is sent outside any transaction. Success -> SENT (accepted by the mail server, which is not "invoice issued").
  Failure -> FAILED with exponential backoff, at most ``MAX_ATTEMPTS`` tries, then GAVE_UP and the Accounts In
  Charge are alerted. A worker that dies after claiming leaves SENDING; after ``STALE_SENDING`` it becomes
  UNCERTAIN and is never resent automatically (it may have gone out); staff resolve it explicitly.
* Mode ``off`` (default) sends nothing; ``test`` sends to the configured test address with a [TEST] subject.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape

from . import notifications
from .models import (
    ExternalServicesSettings,
    InvoiceRequestStatus,
    SricDispatchAttempt,
    SricInvoiceBatch,
    SricInvoiceRequest,
)

logger = logging.getLogger(__name__)

BATCH_TIME = time(9, 30)
MAX_ATTEMPTS = 5
STALE_SENDING = timedelta(minutes=30)
BACKOFF_BASE = timedelta(minutes=30)


class DispatchError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code

    def payload(self) -> dict:
        return {"error": self.message, "code": self.code}


def is_working_day(d) -> bool:
    from iic_booking.equipment.models import Holiday

    if d.weekday() >= 5:
        return False
    return not Holiday.objects.filter(date=d, is_active=True).exists()


def is_batch_day(d) -> bool:
    """The first working day of ``d``'s week (Monday, or the next working day after holidays)."""
    if not is_working_day(d):
        return False
    monday = d - timedelta(days=d.weekday())
    return not any(is_working_day(monday + timedelta(days=i)) for i in range(d.weekday()))


def next_batch_at(now=None) -> datetime:
    """The next weekly batch, 09:30 IST on the first working day of a week (for the user-facing status)."""
    local = timezone.localtime(now or timezone.now())
    d = local.date()
    for _ in range(60):
        candidate = timezone.make_aware(datetime.combine(d, BATCH_TIME), timezone.get_current_timezone())
        if candidate > local and is_batch_day(d):
            return candidate
        d += timedelta(days=1)
    return timezone.make_aware(datetime.combine(d, BATCH_TIME), timezone.get_current_timezone())


def recipient(s: ExternalServicesSettings) -> str:
    if s.sric_dispatch_mode == "live":
        return s.sric_recipient_email
    if s.sric_dispatch_mode == "test":
        return s.sric_test_recipient_email
    return ""


def _money(v) -> str:
    return f"Rs. {v}" if v not in (None, "") else "-"


def compose(req: SricInvoiceRequest, *, mode: str) -> tuple[str, str, str]:
    snap = req.snapshot or {}
    billing = snap.get("billing") or {}
    fbr = snap.get("fbr_number") or "FBR not available"
    ref = snap.get("booking_reference") or req.booking_id
    subject = f"GST Invoice Request — {fbr} — {ref}"
    if mode == "test":
        subject = f"[TEST] {subject}"
    early = req.early_requested
    ack = snap.get("early_acknowledgement") or {}
    lines = [
        "Dear SRIC Office,",
        "",
        "Please issue the GST tax invoice for the following booking at the Institute Instrumentation Centre.",
        "",
        f"Booking reference: {ref}",
        f"FBR number: {fbr}",
        f"Facility: {snap.get('facility') or '-'}",
        f"Equipment: {(snap.get('equipment') or {}).get('name', '-')}",
        "",
        f"Billing name: {billing.get('billing_name', '-')}",
        f"GSTIN: {billing.get('gstin') or 'Not provided'}",
        "Billing address: " + ", ".join(
            [*billing.get("address_lines", []), billing.get("city", ""), billing.get("state", ""),
             billing.get("pincode", ""), billing.get("country", "")]
        ).strip(", "),
        "",
        f"Total charge: {_money((snap.get('charges') or {}).get('total'))}",
    ]
    for item in (snap.get("charges") or {}).get("breakdown") or []:
        lines.append(f"  - {item.get('description', '')}: Rs. {item.get('amount', '')}")
    payments = snap.get("payments") or []
    if payments:
        lines.append("Payments:")
        lines.extend(f"  - {p.get('method')}: {p.get('reference')} ({_money(p.get('amount'))})" for p in payments)
    lines += [
        f"Payment realised: {snap.get('payment_settled_at') or 'Paid from the portal wallet'}",
        "",
        f"Invoice timing: {'Early request before analysis' if early else 'After analysis'}",
        f"Request type: {'Explicit early request by the user' if early else 'Raised when the analysis was completed'}",
    ]
    if early and ack:
        lines.append(
            f"User acknowledgement: disclosure version {ack.get('policy_version')} accepted "
            f"{ack.get('acknowledged_at')}"
        )
    lines += ["", f"Analysis status: {snap.get('booking_status')}", "", "Regards,", "IIC Booking Portal"]
    text = "\n".join(lines)
    html = "<p>" + "<br/>".join(escape(line) for line in lines) + "</p>"
    return subject, text, html


def _send(req: SricInvoiceRequest, to: str, mode: str) -> None:
    subject, text, html = compose(req, mode=mode)
    msg = EmailMultiAlternatives(subject=subject, body=text, from_email=settings.DEFAULT_FROM_EMAIL, to=[to])
    msg.attach_alternative(html, "text/html")
    if msg.send(fail_silently=False) != 1:
        raise RuntimeError("mail server did not accept the message")


def compose_consolidated(reqs: list[SricInvoiceRequest], *, mode: str) -> tuple[str, str, str]:
    """One request for a single GST invoice covering several bookings of the same user."""
    first = reqs[0].snapshot or {}
    billing = first.get("billing") or {}
    total = sum((Decimal(str((r.snapshot.get("charges") or {}).get("total") or 0)) for r in reqs), Decimal("0"))
    subject = f"Consolidated GST Invoice Request — {billing.get('billing_name', '-')} — {len(reqs)} bookings"
    if mode == "test":
        subject = f"[TEST] {subject}"
    lines = [
        "Dear SRIC Office,",
        "",
        "Please issue ONE consolidated GST tax invoice for the following bookings at the Institute Instrumentation "
        "Centre, as the user has asked.",
        "",
        f"Billing name: {billing.get('billing_name', '-')}",
        f"GSTIN: {billing.get('gstin') or 'Not provided'}",
        "Billing address: " + ", ".join(
            [*billing.get("address_lines", []), billing.get("city", ""), billing.get("state", ""),
             billing.get("pincode", ""), billing.get("country", "")]
        ).strip(", "),
        "",
        "Bookings:",
    ]
    for r in reqs:
        snap = r.snapshot or {}
        lines.append(
            f"  - {snap.get('booking_reference') or r.booking_id} | FBR {snap.get('fbr_number') or 'not available'} | "
            f"{(snap.get('equipment') or {}).get('name', '-')} | {_money((snap.get('charges') or {}).get('total'))}"
        )
    lines += ["", f"Total of all bookings: {_money(total)}",
              f"Payment realised: {first.get('payment_settled_at') or 'Paid from the portal wallet'}",
              "", "Regards,", "IIC Booking Portal"]
    text = "\n".join(lines)
    return subject, text, "<p>" + "<br/>".join(escape(line) for line in lines) + "</p>"


def _send_consolidated(reqs: list[SricInvoiceRequest], to: str, mode: str) -> None:
    subject, text, html = compose_consolidated(reqs, mode=mode)
    msg = EmailMultiAlternatives(subject=subject, body=text, from_email=settings.DEFAULT_FROM_EMAIL, to=[to])
    msg.attach_alternative(html, "text/html")
    if msg.send(fail_silently=False) != 1:
        raise RuntimeError("mail server did not accept the message")


def _claim(pk, now) -> bool:
    return bool(
        SricInvoiceRequest.objects.filter(
            pk=pk, status__in=[InvoiceRequestStatus.BATCHED, InvoiceRequestStatus.FAILED], attempts__lt=MAX_ATTEMPTS
        ).filter(next_attempt_at__isnull=True).update(status=InvoiceRequestStatus.SENDING, claimed_at=now)
        or SricInvoiceRequest.objects.filter(
            pk=pk, status__in=[InvoiceRequestStatus.BATCHED, InvoiceRequestStatus.FAILED], attempts__lt=MAX_ATTEMPTS,
            next_attempt_at__lte=now,
        ).update(status=InvoiceRequestStatus.SENDING, claimed_at=now)
    )


def _alert_accounts(req: SricInvoiceRequest) -> None:
    from .invoices import _staff_recipients

    notifications.notify(
        f"invoice:{req.pk}:gave_up", _staff_recipients(req.booking),
        title=f"GST invoice request {req.snapshot.get('booking_reference')} could not be sent",
        message=(
            f"The request to SRIC failed {req.attempts} times ({req.last_error or 'mail error'}). Check the address "
            "and resend it from the invoice requests page."
        ),
        link="/accounts/invoice-requests",
    )


def run_batch(now=None) -> dict:
    now = now or timezone.now()
    local = timezone.localtime(now)
    s = ExternalServicesSettings.load()
    if not (s.invoice_requests_enabled or s.cancellation_policy_enabled) or s.sric_dispatch_mode == "off":
        return {"skipped": "off"}
    if not is_working_day(local.date()):
        return {"skipped": "not_working_day"}
    if local.time() < BATCH_TIME:
        return {"skipped": "before_0930"}
    to = recipient(s)
    if not to:
        return {"skipped": "no_recipient"}
    result = {"batched": 0, "sent": 0, "failed": 0, "uncertain": 0}
    # Requests a crashed worker left mid-send: never resent automatically.
    stale = list(SricInvoiceRequest.objects.filter(status=InvoiceRequestStatus.SENDING,
                                                   claimed_at__lt=now - STALE_SENDING).values_list("pk", flat=True))
    for pk in stale:
        if SricInvoiceRequest.objects.filter(pk=pk, status=InvoiceRequestStatus.SENDING).update(
                status=InvoiceRequestStatus.UNCERTAIN):
            SricDispatchAttempt.objects.create(request_id=pk, outcome="uncertain", detail="worker stopped mid-send")
            result["uncertain"] += 1
    if is_batch_day(local.date()):
        with transaction.atomic():
            batch, created = SricInvoiceBatch.objects.get_or_create(batch_date=local.date(), defaults={"mode": s.sric_dispatch_mode})
            if created:
                from .sric_refunds import queue_into_batch

                cutoff = batch.opened_at
                ids = []
                if s.invoice_requests_enabled:
                    ids = list(SricInvoiceRequest.objects.select_for_update(skip_locked=True)
                               .filter(status=InvoiceRequestStatus.QUEUED, created_at__lte=cutoff).values_list("pk", flat=True))
                    SricInvoiceRequest.objects.filter(pk__in=ids).update(status=InvoiceRequestStatus.BATCHED, batch=batch)
                SricInvoiceBatch.objects.filter(pk=batch.pk).update(requests_count=len(ids))
                result["batched"] = len(ids)
                result["refunds_batched"] = queue_into_batch(batch, cutoff) if s.cancellation_policy_enabled else 0
    from .sric_refunds import send_due

    result.update(send_due(to, s.sric_dispatch_mode))
    due = list(
        SricInvoiceRequest.objects.filter(
            status__in=[InvoiceRequestStatus.BATCHED, InvoiceRequestStatus.FAILED], attempts__lt=MAX_ATTEMPTS
        ).values_list("pk", "consolidation_group")[:200]
    )
    # A consolidation group is one email (one consolidated invoice request); every other request is its own email.
    units, seen = [], set()
    for pk, group in due:
        if not group:
            units.append([pk])
        elif group not in seen:
            seen.add(group)
            units.append([p for p, g in due if g == group])
    for pks in units:
        claimed = [pk for pk in pks if _claim(pk, now)]
        if not claimed:
            continue
        reqs = list(SricInvoiceRequest.objects.select_related("booking__equipment", "booking__user")
                    .filter(pk__in=claimed).order_by("pk"))
        try:
            if len(reqs) == 1:
                _send(reqs[0], to, s.sric_dispatch_mode)
            else:
                _send_consolidated(reqs, to, s.sric_dispatch_mode)
        except Exception as exc:  # noqa: BLE001 - recorded and retried with backoff
            for req in reqs:
                attempts = req.attempts + 1
                gave_up = attempts >= MAX_ATTEMPTS
                SricInvoiceRequest.objects.filter(pk=req.pk).update(
                    status=InvoiceRequestStatus.GAVE_UP if gave_up else InvoiceRequestStatus.FAILED,
                    attempts=attempts, last_error=type(exc).__name__[:255],
                    next_attempt_at=now + BACKOFF_BASE * (2 ** (attempts - 1)),
                )
                SricDispatchAttempt.objects.create(request_id=req.pk, batch=req.batch, outcome="failed",
                                                   detail=type(exc).__name__[:255])
                result["failed"] += 1
                if gave_up:
                    req.refresh_from_db()
                    _alert_accounts(req)
            continue
        for req in reqs:
            SricInvoiceRequest.objects.filter(pk=req.pk).update(
                status=InvoiceRequestStatus.SENT, attempts=req.attempts + 1, sent_at=timezone.now(), sent_to=to,
                sent_in_mode=s.sric_dispatch_mode, last_error="", next_attempt_at=None,
            )
            SricDispatchAttempt.objects.create(request_id=req.pk, batch=req.batch, outcome="sent",
                                               detail=s.sric_dispatch_mode)
            result["sent"] += 1
            if s.sric_dispatch_mode == "live":  # test sends are not news for the user
                notifications.notify(
                    f"invoice:{req.pk}:sent", [req.booking.user],
                    title=f"GST invoice request sent to SRIC for {req.snapshot.get('booking_reference')}",
                    message="Your GST invoice request has been sent to the SRIC office. SRIC issues the invoice; it "
                            "will appear in the portal once IIC receives it.",
                    link=f"/my-bookings?booking={req.booking_id}",
                )
    logger.info("sric invoice batch: %s", result)
    return result


def resolve(user, request_id, action: str, note: str) -> SricInvoiceRequest:
    """Staff resolution of an UNCERTAIN or GAVE_UP request: ``confirm_sent`` (SRIC confirmed receipt) or
    ``resend`` (requeue for the next run). Audited in ``SricDispatchAttempt``."""
    from .invoices import InvoiceError, _staff_request

    _staff_request(user, request_id)  # authorise before validating the caller's input
    if len((note or "").strip()) < 10:
        raise InvoiceError("Give a note of at least 10 characters.", field="note")
    with transaction.atomic():
        req = _staff_request(user, request_id, lock=True)
        if req.status not in (InvoiceRequestStatus.UNCERTAIN, InvoiceRequestStatus.GAVE_UP):
            raise InvoiceError("Only an uncertain or failed request can be resolved.", code="invalid_state")
        if action == "confirm_sent":
            req.status, req.sent_at = InvoiceRequestStatus.SENT, req.sent_at or timezone.now()
        elif action == "resend":
            req.status, req.attempts, req.next_attempt_at = InvoiceRequestStatus.FAILED, 0, None
        else:
            raise InvoiceError("Choose confirm_sent or resend.", field="action")
        req.save(update_fields=["status", "sent_at", "attempts", "next_attempt_at", "updated_at"])
        SricDispatchAttempt.objects.create(request=req, outcome="resolved", actor=user,
                                           detail=f"{action}: {note.strip()}"[:255])
    return req
