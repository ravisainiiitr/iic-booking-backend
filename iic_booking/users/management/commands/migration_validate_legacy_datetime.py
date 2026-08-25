"""READ-ONLY legacy booking datetime sanity report."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.legacy_datetime_validation import validate_legacy_datetime_readonly


class Command(BaseCommand):
    help = "Phase 10E — validate legacy booking datetime semantics (READ-ONLY MySQL)."

    def add_arguments(self, parser):
        parser.add_argument("--json-out", type=str, default="")
        parser.add_argument(
            "--default-artifact",
            action="store_true",
            help="Write docs/release/migration/legacy_datetime_validation.json",
        )

    def handle(self, *args, **options):
        report = validate_legacy_datetime_readonly()
        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)
        out = (options.get("json_out") or "").strip()
        if options.get("default_artifact"):
            from pathlib import Path

            from django.conf import settings

            out = str(Path(getattr(settings, "BASE_DIR", ".")) / "docs/release/migration/legacy_datetime_validation.json")
        if out:
            from pathlib import Path

            path = Path(out)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                fh.write(payload + "\n")
            self.stdout.write(self.style.SUCCESS(f"Wrote {path}"))
        if not report.get("ok"):
            self.stdout.write(self.style.ERROR(report.get("error") or "validation_failed"))
        elif report.get("contract_approval_status") != "APPROVED":
            self.stdout.write(self.style.WARNING("Datetime contract: OPERATOR_REQUIRED — discovery blocked until approval"))
