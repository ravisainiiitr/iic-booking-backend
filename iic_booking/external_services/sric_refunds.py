"""Bank refunds in the SRIC batch runs (``sric_dispatch.run_batch``: 09:30 and every 30 minutes until 17:00 on
working days).

Each refund is its own email, built and sent by ``bank_refunds`` (the Main Administrator's template, the full details,
the refund addresses). The timing is the Main Administrator's choice:

* ``immediate``: a refund is sent as soon as the user confirms the account; these runs only retry failures and pick up
  anything that could not be queued at once.
* ``weekly``: the week's first run takes every refund confirmed before it into the batch and sends them.

A refund is never sent before the user has confirmed the account, and never resent automatically after an interrupted
send (``bank_refunds.mark_stale``).
"""

from __future__ import annotations

import logging

from .models import ExternalRefund, ExternalServicesSettings, RefundDestination, RefundStatus

logger = logging.getLogger(__name__)


def queue_into_batch(batch, cutoff) -> int:
    """Weekly timing: attach the refunds confirmed before the batch opened."""
    s = ExternalServicesSettings.load()
    if s.refund_dispatch_timing != "weekly":
        return 0
    ids = list(ExternalRefund.objects.select_for_update(skip_locked=True).filter(
        destination=RefundDestination.BANK, status=RefundStatus.QUEUED, batch__isnull=True,
        account_confirmed_at__isnull=False, account_confirmed_at__lte=cutoff,
    ).values_list("pk", flat=True))
    ExternalRefund.objects.filter(pk__in=ids).update(batch=batch)
    return len(ids)


def send_due() -> dict:
    """Send every refund that is due now, one email each. Returns counts."""
    from .bank_refunds import due_for_dispatch, mark_stale, recipients, send

    s = ExternalServicesSettings.load()
    uncertain = mark_stale()
    if not recipients(s)[0]:
        return {"refunds_skipped": "no_recipient", "refunds_uncertain": uncertain}
    sent = failed = 0
    for pk in list(due_for_dispatch(s).order_by("pk").values_list("pk", flat=True)[:100]):
        result = send(pk)
        sent += result == RefundStatus.SENT
        failed += result == RefundStatus.FAILED
    return {"refunds_sent": sent, "refunds_failed": failed, "refunds_uncertain": uncertain}
