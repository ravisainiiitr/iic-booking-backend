from django.apps import AppConfig


class LearningConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.learning"
    verbose_name = "Online learning (modules, SOP sign-off, quizzes) and booking logbook"
