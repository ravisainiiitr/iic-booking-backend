from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class RemoteAnalysisConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "iic_booking.remote_analysis"
    verbose_name = _("Remote Analysis")

    def ready(self):
        from iic_booking.remote_analysis import signals  # noqa: F401

        # Optional: persist Guacamole env overlays at process start (production bootstrap).
        import os

        if os.environ.get("RA_APPLY_ENV_SETTINGS", "").strip().lower() in {"1", "true", "yes"}:
            try:
                from django.db import connection

                if connection.introspection.table_names():
                    from iic_booking.remote_analysis.guacamole.settings_env import persist_from_environ

                    persist_from_environ()
            except Exception:
                # App may start before migrations; command sync_remote_analysis_settings remains authoritative.
                pass
