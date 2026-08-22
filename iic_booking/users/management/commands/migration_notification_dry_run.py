"""Dry-run migration notification recipient selection — ZERO emails sent."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.migration_notifications import create_notification_batch


class Command(BaseCommand):
    help = "Migration email dry-run: classify recipients, send ZERO emails."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", default=True)

    def handle(self, *args, **options):
        batch, report = create_notification_batch(dry_run=True)
        payload = {
            "batch_id": batch.id,
            "emails_sent": 0,
            "total_recipients": report["total_recipients"],
            "Faculty": report["faculty"],
            "Students": report["students"],
            "OIC": report["oic"],
            "Admin": report["admin"],
            "skipped": report["skipped"],
            "invalid_email": report["invalid_email"],
            "duplicate_email": report["duplicate_email"],
        }
        self.stdout.write(json.dumps(payload, indent=2))
        self.stdout.write(self.style.SUCCESS("DRY-RUN complete — ZERO emails sent."))
