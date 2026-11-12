from django.apps import AppConfig


class ConnectConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.connect"
    label = "connect"
    verbose_name = "IITR Connect"

    def ready(self):
        from . import signals  # noqa: F401
