from django.apps import AppConfig


class GstEinvoiceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.gst_einvoice"
    label = "gst_einvoice"
    verbose_name = "GST e-invoice (sandbox demonstration)"
