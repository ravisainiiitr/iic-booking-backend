"""Sync RemoteAnalysisSettings singleton from environment variables."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from iic_booking.remote_analysis.guacamole.settings_env import (
    persist_from_environ,
    production_guacamole_configured,
)


class Command(BaseCommand):
    help = (
        "Apply RA_GUACAMOLE_* / RA_MOCK_GUACAMOLE environment variables to "
        "RemoteAnalysisSettings and validate production Guacamole configuration."
    )

    def handle(self, *args, **options):
        obj = persist_from_environ()
        ok, problems = production_guacamole_configured(obj)
        self.stdout.write(
            self.style.SUCCESS(
                f"RemoteAnalysisSettings updated | mock_guacamole={obj.mock_guacamole} "
                f"| api={obj.guacamole_api_url or '(empty)'} "
                f"| base={obj.guacamole_base_url or '(empty)'}"
            )
        )
        if not ok:
            self.stdout.write(self.style.ERROR("Production Guacamole incomplete: " + ", ".join(problems)))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Guacamole configuration OK."))
