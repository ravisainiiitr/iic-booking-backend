"""READ-ONLY upcoming legacy booking discovery for migration window."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.legacy_upcoming_discovery import discover_upcoming_legacy_week


class Command(BaseCommand):
    help = "Phase 10E — discover upcoming legacy bookings (READ-ONLY, no blocks)."

    def add_arguments(self, parser):
        parser.add_argument("--column-map-file", type=str, default="")
        parser.add_argument("--json-out", type=str, default="")
        parser.add_argument(
            "--default-artifact",
            action="store_true",
            help="Write docs/release/migration/legacy_upcoming_week.json when contract approved",
        )

    def handle(self, *args, **options):
        report = discover_upcoming_legacy_week(
            column_map_file=(options.get("column_map_file") or "").strip(),
        )
        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)
        out = (options.get("json_out") or "").strip()
        if options.get("default_artifact") and report.get("ok"):
            from django.conf import settings

            out = out or str(Path(settings.BASE_DIR) / "docs/release/migration/legacy_upcoming_week.json")
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(payload)
            self.stdout.write(self.style.SUCCESS(f"Wrote {out}"))
        if not report.get("ok"):
            self.stdout.write(self.style.ERROR(report.get("error") or "discovery_failed"))
