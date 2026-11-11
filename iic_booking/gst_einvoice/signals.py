"""The hook into the existing invoice flow: a new ``SricInvoiceRequest`` is offered to the portal invoice after its
transaction commits. Nothing happens while the module is off, or (sandbox) for anyone but test accounts."""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender="external_services.SricInvoiceRequest", dispatch_uid="gst_einvoice_request_created")
def request_created(sender, instance, created, **kwargs):
    if not created or kwargs.get("raw"):
        return
    from . import conf

    if not conf.enabled():
        return
    pk = instance.pk

    def queue():
        from .tasks import prepare_request

        try:
            prepare_request.delay(pk)
        except Exception:  # noqa: BLE001 - broker down: process_pending picks it up within 15 minutes
            logger.warning("GST e-invoice: could not queue request %s; the periodic run will take it up", pk)

    transaction.on_commit(queue)
