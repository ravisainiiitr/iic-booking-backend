"""Staging T0 simulation — NEVER production."""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from iic_booking.users.legacy_ledger.migration_t0 import run_staging_t0


class Command(BaseCommand):
    help = "Run Phase 8C staging T0 (requires --confirm-staging-t0). Refuses PRODUCTION."

    def add_arguments(self, parser):
        parser.add_argument("--fixture-file", type=str, default="")
        parser.add_argument("--fixture-json", type=str, default="[]")
        parser.add_argument("--confirm-staging-t0", action="store_true")
        parser.add_argument("--email-dry-run", action="store_true", default=False)
        parser.add_argument("--queue-emails", action="store_true", default=False)

    def handle(self, *args, **options):
        env = str(getattr(settings, "DEPLOYMENT_ENVIRONMENT", "") or "").upper()
        if env in {"PRODUCTION", "PROD"}:
            raise CommandError("Refusing T0 on PRODUCTION.")
        rows = []
        path = (options.get("fixture_file") or "").strip()
        if path:
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
        else:
            rows = json.loads(options.get("fixture_json") or "[]")
        if not options.get("confirm_staging_t0"):
            raise CommandError("Pass --confirm-staging-t0 after dry-run READY.")
        result = run_staging_t0(
            legacy_rows=rows,
            confirm_staging_t0=True,
            queue_emails=bool(options.get("queue_emails")),
            email_dry_run=bool(options.get("email_dry_run")),
        )
        self.stdout.write(json.dumps(result, indent=2, default=str))
        if not result.get("ok"):
            raise CommandError("Staging T0 failed validation gates.")
        self.stdout.write(self.style.SUCCESS("STAGING T0 COMPLETE"))
