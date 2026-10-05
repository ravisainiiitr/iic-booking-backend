from django.apps import AppConfig


class DepartmentModulesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.department_modules"
    label = "department_modules"
    verbose_name = "Department modules"

    def ready(self):
        from . import signals  # noqa: F401
