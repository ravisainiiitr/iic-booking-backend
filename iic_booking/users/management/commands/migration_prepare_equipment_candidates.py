"""Prepare explicit legacy→new equipment mapping candidate report (READ-ONLY)."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.legacy_equipment_inventory import (
    build_equipment_mapping_candidate_report,
)


class Command(BaseCommand):
    help = "List legacy and new equipment inventories; no automatic mapping."

    def add_arguments(self, parser):
        parser.add_argument("--json-out", type=str, default="")

    def handle(self, *args, **options):
        report = build_equipment_mapping_candidate_report()
        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)
        out = (options.get("json_out") or "").strip()
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(payload)
        if not report.get("ok"):
            self.stdout.write(self.style.ERROR("equipment_candidate_report_failed"))
