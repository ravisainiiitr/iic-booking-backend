from django.apps import AppConfig


class EquipmentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'iic_booking.equipment'
    verbose_name = 'Equipment'

    def ready(self):
        import iic_booking.equipment.signals  # noqa: F401
        import iic_booking.equipment.fabrication_material_support  # noqa: F401
        import iic_booking.equipment.remote_analysis_integration.signals  # noqa: F401
        import iic_booking.equipment.peak_window_signals  # noqa: F401
        import iic_booking.equipment.pi_pricing_signals  # noqa: F401
        from iic_booking.equipment.booking_audit import connect_celery_signals
        from iic_booking.equipment.config_audit import connect_signals as connect_config_audit_signals
        from iic_booking.equipment.search_index import connect_signals as connect_search_index_signals

        connect_celery_signals()
        connect_config_audit_signals()
        from iic_booking.equipment.publication_dedupe import connect_signals as connect_publication_signals

        connect_publication_signals()
        from iic_booking.equipment.operating_cycle import connect_signals as connect_operating_cycle_signals

        connect_operating_cycle_signals()
        connect_search_index_signals()
