"""Payment and refund changes reach WhatsApp through signals, so that business code stays untouched."""

from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from . import hooks


@receiver(post_save, sender="payments.PaymentOrder", dispatch_uid="whatsapp_payment_order")
def _payment_order(sender, instance, created, raw=False, **kwargs):
    if not raw:
        hooks.payment_order_saved(instance, created)


@receiver(post_save, sender="payments.PaymentRefund", dispatch_uid="whatsapp_payment_refund")
def _payment_refund(sender, instance, created, raw=False, **kwargs):
    if not raw:
        hooks.payment_refund_saved(instance, created)


@receiver(post_save, sender="external_services.ExternalRefund", dispatch_uid="whatsapp_external_refund")
def _external_refund(sender, instance, created, raw=False, **kwargs):
    if not raw:
        hooks.external_refund_saved(instance, created)

