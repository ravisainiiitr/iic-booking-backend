from django.apps import AppConfig


class TrainingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.training"
    label = "training"
    verbose_name = "Training & Certification"
