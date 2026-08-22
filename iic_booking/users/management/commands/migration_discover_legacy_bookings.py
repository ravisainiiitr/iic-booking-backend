"""Discover legacy bookings for the configured migration window (read-only).

Live MySQL booking column map is NOT hard-coded. Pass --fixture JSON with
normalized rows, or pipe via --fixture-file.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from iic_booking.users.legacy_ledger.booking_bridge import discover_legacy_bookings


class Command(BaseCommand):
    help = "Read-only discovery of legacy bookings for migration window (fixture rows)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture-file",
            type=str,
            default="",
            help="JSON file: list of normalized legacy booking dicts.",
        )
        parser.add_argument(
            "--fixture-json",
            type=str,
            default="[]",
            help="Inline JSON list of normalized rows (default empty).",
        )

    def handle(self, *args, **options):
        rows = []
        path = (options.get("fixture_file") or "").strip()
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    rows = json.load(fh)
            except OSError as exc:
                raise CommandError(str(exc)) from exc
        else:
            rows = json.loads(options.get("fixture_json") or "[]")
        if not isinstance(rows, list):
            raise CommandError("Fixture must be a JSON list")
        report = discover_legacy_bookings(rows)
        self.stdout.write(json.dumps({"counts": report["counts"], "schema_note": report["schema_note"]}, indent=2))
        for key in ("eligible", "unmapped", "conflicting", "cancelled", "completed", "invalid"):
            self.stdout.write(f"{key}={report['counts'][key]}")
