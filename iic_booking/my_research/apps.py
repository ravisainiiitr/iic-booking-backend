from django.apps import AppConfig


class MyResearchConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.my_research"
    label = "my_research"
    verbose_name = "My Research"
