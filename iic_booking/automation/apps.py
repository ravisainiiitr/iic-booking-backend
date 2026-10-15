from django.apps import AppConfig


class AutomationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.automation"
    label = "automation"
    verbose_name = "AI and automation"

    def ready(self):
        from . import signals  # noqa: F401
