from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SyncConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.sync"
    verbose_name = _("Department Sync")
