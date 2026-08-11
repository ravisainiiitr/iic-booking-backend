"""Backfill AnalysisSoftwareCatalog from present InstalledSoftware rows."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from iic_booking.remote_analysis.services.catalog_sync import (
    backfill_catalog_from_installed,
    inventory_discovery_summary,
)


class Command(BaseCommand):
    help = "Promote present InstalledSoftware into AnalysisSoftwareCatalog (R11)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50_000)

    def handle(self, *args, **options):
        before = inventory_discovery_summary()
        self.stdout.write(f"before={before}")
        result = backfill_catalog_from_installed(limit=options["limit"])
        after = inventory_discovery_summary()
        self.stdout.write(self.style.SUCCESS(f"backfill={result}"))
        self.stdout.write(f"after={after}")
