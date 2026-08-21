"""Lightweight REAL integration status (no env edits, no secret printing)."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from iic_booking.users.legacy_ledger.real_integration_guards import (
    assert_staging_environment,
    build_real_integration_status,
    format_status_human,
)


class Command(BaseCommand):
    help = "Report REAL integration configuration status without printing secrets."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Emit JSON only")

    def handle(self, *args, **options):
        module = __import__("django.conf", fromlist=["settings"]).settings.SETTINGS_MODULE
        if "production" in (module or "").lower():
            raise CommandError("REFUSED: cannot run under production settings.")
        try:
            assert_staging_environment()
        except Exception as exc:  # noqa: BLE001
            raise CommandError(str(exc)) from exc

        report = build_real_integration_status()
        if options.get("json"):
            # Strip deep details that might confuse operators; keep presence-safe fields
            self.stdout.write(json.dumps(report, indent=2, default=str))
        else:
            self.stdout.write(format_status_human(report))
