from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class RemoteAnalysisConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.remote_analysis"
    verbose_name = _("Remote Analysis")

    def ready(self):
        from iic_booking.remote_analysis import signals  # noqa: F401
