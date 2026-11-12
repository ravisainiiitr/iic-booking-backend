"""Scheduled and queued work for portal GST invoices (does nothing while the module is switched off)."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="gst_einvoice.prepare_request")
def prepare_request(request_id: int) -> str:
    """Prepare (and, when every rule passes, issue) the portal invoice for one new SRIC invoice request."""
    from iic_booking.external_services.models import SricInvoiceRequest

    from . import conf, service

    if not conf.enabled():
        return "disabled"
    req = SricInvoiceRequest.objects.select_related("booking__user").filter(pk=request_id).first()
    if req is None or not service.eligible_request(req):
        return "not_eligible"
    inv = service.prepare_from_request(req)
    return f"{inv.pk}:{inv.status}"


@shared_task(name="gst_einvoice.process_pending")
def process_pending() -> dict:
    """Every 15 minutes: requests missed by the queue, and unanswered e-invoice submissions (looked up first)."""
    from . import service

    return service.process_pending()
