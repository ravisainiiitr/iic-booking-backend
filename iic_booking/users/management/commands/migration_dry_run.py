"""Phase 8B migration dry-run — no writes."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from iic_booking.users.legacy_ledger.migration_dry_run import migration_dry_run


class Command(BaseCommand):
    help = "Migration readiness dry-run. Performs NO writes."

    def add_arguments(self, parser):
        parser.add_argument("--fixture-file", type=str, default="")
        parser.add_argument("--fixture-json", type=str, default="[]")

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
        report = migration_dry_run(rows)
        self.stdout.write(json.dumps(report, indent=2, default=str))
        if report.get("verdict") == "READY FOR MIGRATION":
            self.stdout.write(self.style.SUCCESS("READY FOR MIGRATION"))
        else:
            self.stdout.write(self.style.ERROR("NOT READY"))
            for b in report.get("blockers") or []:
                self.stdout.write(f"  blocker: {b}")
