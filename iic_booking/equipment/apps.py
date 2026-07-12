from django.apps import AppConfig


class EquipmentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'iic_booking.equipment'
    verbose_name = 'Equipment'

    def ready(self):
        import iic_booking.equipment.signals  # noqa: F401
