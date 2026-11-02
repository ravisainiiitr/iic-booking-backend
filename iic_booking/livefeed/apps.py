from django.apps import AppConfig


class LiveFeedConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.livefeed"
    verbose_name = "Live instrument feed"
