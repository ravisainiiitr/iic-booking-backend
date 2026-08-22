"""Preview migration email templates (sample data; Main Admin / staging)."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.migration_notifications import preview_templates


class Command(BaseCommand):
    help = "Preview Faculty/Student/OIC/Admin migration emails (sample data)."

    def handle(self, *args, **options):
        data = preview_templates()
        # Avoid dumping full HTML in logs
        slim = {
            k: {
                "subject": v["subject"],
                "preheader": v["preheader"],
                "html_length": v["html_length"],
                "text_excerpt": v["text_excerpt"],
                "sample_context": v["sample_context"],
            }
            for k, v in data.items()
        }
        self.stdout.write(json.dumps(slim, indent=2))
