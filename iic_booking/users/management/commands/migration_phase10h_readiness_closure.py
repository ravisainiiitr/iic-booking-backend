"""Phase 10H — production blocker closure report (READ-ONLY)."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import VERDICT_READY
from iic_booking.users.legacy_ledger.phase10h_readiness_closure import (
    build_phase10h_final_readiness,
    write_phase10h_artifacts,
)


class Command(BaseCommand):
    help = (
        "Phase 10H read-only blocker closure with real MySQL evidence. "
        "Does NOT migrate, activate T0, freeze, email, refund, or cleanup."
    )

    def add_arguments(self, parser):
        parser.add_argument("--json-out", type=str, default="")
        parser.add_argument("--default-artifact", action="store_true")
        parser.add_argument("--backup-verified", action="store_true")

    def handle(self, *args, **options):
        from iic_booking.users.legacy_ledger.legacy_datetime_validation import (
            validate_legacy_datetime_readonly,
        )

        datetime_validation = validate_legacy_datetime_readonly()
        report = build_phase10h_final_readiness(
            backup_verified=bool(options.get("backup_verified")),
            datetime_validation=datetime_validation,
            production_migrate_plan={
                "source": "production docker exec migrate --plan (2026-08-25)",
                "applied": ["0096", "0097", "0098", "0099", "0100"],
                "pending_on_production_image": ["0101", "0102", "0103"],
                "pending_after_phase10d_deploy": ["0104"],
                "migrate_executed": False,
                "forbidden_absent": True,
            },
            explicit_evidence={"legacy_equipment_ids": 45},
        )
        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)

        if options.get("default_artifact"):
            paths = write_phase10h_artifacts(report, datetime_validation=datetime_validation)
            for p in paths:
                self.stdout.write(self.style.SUCCESS(f"Wrote {p}"))

        out = (options.get("json_out") or "").strip()
        if out:
            Path(out).write_text(payload + "\n", encoding="utf-8")

        if report["verdict"] == VERDICT_READY:
            self.stdout.write(self.style.WARNING(report["verdict"]))
        else:
            self.stdout.write(self.style.ERROR(report["verdict"]))

        for k, v in (report.get("production_safety") or {}).items():
            self.stdout.write(f"  {k}: {v}")
