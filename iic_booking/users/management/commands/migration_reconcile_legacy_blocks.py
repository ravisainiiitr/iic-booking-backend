"""Reconcile active LegacyBookingBlock rows vs DailySlot.BLOCKED (read-only)."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.booking_bridge import reconcile_legacy_blocks


class Command(BaseCommand):
    help = "Reconcile legacy booking blocks. No writes."

    def handle(self, *args, **options):
        report = reconcile_legacy_blocks()
        self.stdout.write(json.dumps(report, indent=2, default=str))
        if report.get("ok"):
            self.stdout.write(self.style.SUCCESS("RECONCILIATION OK"))
        else:
            self.stdout.write(self.style.ERROR("RECONCILIATION ISSUES"))
