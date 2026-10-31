"""Bank refunds sent to SRIC with the weekly batch (first working day of the week, 09:30 IST).

Every bank refund queued before the week's batch opened goes out in one email to the SRIC desk (the same address
and off / test / live mode as the invoice requests). A failed send is retried by the later runs of the batch; the
refund stays visible to the Accounts In Charge, who close it once the money has reached the user's account.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone
from django.utils.html import escape

from .models import ExternalRefund, RefundDestination, RefundStatus

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


def compose(refunds: list[ExternalRefund], *, mode: str) -> tuple[str, str, str]:
    from iic_booking.users.display import get_user_display_name

    prefix = "[TEST] " if mode == "test" else ""
    total = sum((r.refund_amount for r in refunds), start=refunds[0].refund_amount * 0)
    subject = f"{prefix}IIC refunds to bank accounts: {len(refunds)} request(s), ₹{total:,.2f}"
    head = ("Booking", "Payee", "Bank", "Account", "IFSC", "Amount (₹)", "GST included", "Reason")
    rows = []
    for r in refunds:
        b = r.bank_snapshot or {}
        rows.append((
            getattr(r.booking, "virtual_booking_id", "") or str(r.booking_id),
            f"{b.get('account_holder_name', '')} ({get_user_display_name(r.user)}, {r.user.email})",
            f"{b.get('bank_name', '')} {b.get('branch', '')}".strip(),
            b.get("account_number", ""), b.get("ifsc", ""), f"{r.refund_amount:,.2f}",
            "Yes" if r.gst_refunded else "No (GST invoice already requested)",
            "Lab disruption, no penalty" if r.origin == "disruption" else f"Cancellation, {r.penalty_percent}% penalty",
        ))
    intro = ("Dear SRIC team,\n\nPlease refund the amounts below to the bank accounts given by the users. Each amount "
             "was received through the IIC booking portal; the return shipping charge is not refunded. Please share "
             "the bank reference (UTR) of each payment with the IIC Accounts In Charge.\n\n")
    text = intro + "\n".join(" | ".join(map(str, row)) for row in (head, *rows)) + f"\n\nTotal: ₹{total:,.2f}\n\nIIC Booking Portal\n"
    cells = "".join(f"<th style='border:1px solid #999;padding:4px 8px;text-align:left'>{escape(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td style='border:1px solid #999;padding:4px 8px'>{escape(str(c))}</td>"
                                    for c in row) + "</tr>" for row in rows)
    html = (f"<p>Dear SRIC team,</p><p>{escape(intro.split(chr(10)*2, 1)[1].strip())}</p>"
            f"<table style='border-collapse:collapse'><thead><tr>{cells}</tr></thead><tbody>{body}</tbody></table>"
            f"<p><b>Total: ₹{total:,.2f}</b></p><p>IIC Booking Portal</p>")
    return subject, text, html


def queue_into_batch(batch, cutoff) -> int:
    ids = list(ExternalRefund.objects.select_for_update(skip_locked=True).filter(
        destination=RefundDestination.BANK, status=RefundStatus.QUEUED, batch__isnull=True, created_at__lte=cutoff,
    ).values_list("pk", flat=True))
    ExternalRefund.objects.filter(pk__in=ids).update(batch=batch)
    return len(ids)


def send_due(to: str, mode: str) -> dict:
    """Send every batched refund not yet sent (one email). Returns counts."""
    due = list(ExternalRefund.objects.select_related("booking", "user").filter(
        destination=RefundDestination.BANK, batch__isnull=False,
        status__in=[RefundStatus.QUEUED, RefundStatus.FAILED], attempts__lt=MAX_ATTEMPTS,
    ).order_by("pk")[:300])
    if not due:
        return {"refunds_sent": 0}
    subject, text, html = compose(due, mode=mode)
    pks = [r.pk for r in due]
    try:
        msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [to])
        msg.attach_alternative(html, "text/html")
        msg.send()
    except Exception as exc:  # noqa: BLE001 - retried by the next run; staff see FAILED in the tracker
        for r in due:
            ExternalRefund.objects.filter(pk=r.pk).update(status=RefundStatus.FAILED, attempts=r.attempts + 1,
                                                          last_error=type(exc).__name__[:255])
        logger.warning("refund batch email failed: %s", type(exc).__name__)
        return {"refunds_failed": len(pks)}
    now = timezone.now()
    for r in due:
        ExternalRefund.objects.filter(pk=r.pk).update(status=RefundStatus.SENT, attempts=r.attempts + 1, sent_at=now,
                                                      sent_to=to, sent_in_mode=mode, last_error="")
    return {"refunds_sent": len(pks)}
