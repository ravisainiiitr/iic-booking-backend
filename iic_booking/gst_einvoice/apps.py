from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class GstEinvoiceConfig(AppConfig):
    name = "iic_booking.gst_einvoice"
    label = "gst_einvoice"
    verbose_name = _("GST invoicing (sandbox)")

    def ready(self):
        from . import signals  # noqa: F401
