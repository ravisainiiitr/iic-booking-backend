from django.apps import AppConfig


class ImpactConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.impact"
    verbose_name = "Research impact (funding lines, acknowledgements, publication discovery, funder reports)"
