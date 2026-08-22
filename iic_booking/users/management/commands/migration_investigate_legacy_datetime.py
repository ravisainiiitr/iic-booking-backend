"""READ-ONLY investigation of legacy booking datetime columns (production-safe)."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.legacy_booking_mysql import investigate_legacy_booking_datetime


class Command(BaseCommand):
    help = "Investigate legacy MySQL booking_date/time_required semantics (READ-ONLY, no PII)."

    def add_arguments(self, parser):
        parser.add_argument("--json-out", type=str, default="")

    def handle(self, *args, **options):
        report = investigate_legacy_booking_datetime()
        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)
        out = (options.get("json_out") or "").strip()
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(payload)
        if not report.get("ok"):
            self.stdout.write(self.style.ERROR(report.get("error") or "investigation_failed"))
