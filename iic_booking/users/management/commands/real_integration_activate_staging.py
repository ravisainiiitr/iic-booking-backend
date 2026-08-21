"""Safe-by-default REAL staging activation.

Does NOT edit .envs/.staging/.django.
Does NOT set REAL_INTEGRATION_ENABLED automatically.
Does NOT invent credentials.
Refuses production settings.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from iic_booking.users.legacy_ledger.real_integration_activation import (
    format_activation_human,
    run_staging_activation,
    write_activation_evidence,
)


class Command(BaseCommand):
    help = "Deterministic REAL staging activation (safe by default; never edits env)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--write-docs", action="store_true")
        parser.add_argument("--backend-commit", default="")
        parser.add_argument("--frontend-commit", default="")
        parser.add_argument(
            "--skip-tests",
            action="store_true",
            help="Skip embedded guard test run (still runs fixture-isolation checks).",
        )
        parser.add_argument(
            "--skip-live-probes",
            action="store_true",
            help="Config/guard checks only; do not attempt live MySQL/OAuth probes.",
        )
        parser.add_argument(
            "--fail-on-blocked",
            action="store_true",
            help="Exit 1 when overall is not READY FOR REAL STAGING INTEGRATION.",
        )

    def handle(self, *args, **options):
        module = (getattr(settings, "SETTINGS_MODULE", "") or "").lower()
        if "production" in module:
            raise CommandError("REFUSED: cannot activate under production settings.")

        try:
            report = run_staging_activation(
                backend_commit=options.get("backend_commit") or "",
                frontend_commit=options.get("frontend_commit") or "",
                run_tests=not options.get("skip_tests"),
                attempt_live_probes=not options.get("skip_live_probes"),
            )
        except Exception as exc:  # noqa: BLE001
            raise CommandError(str(exc)) from exc

        if options.get("write_docs"):
            paths = write_activation_evidence(report)
            for p in paths:
                self.stderr.write(f"Wrote {p}")

        if options.get("json"):
            self.stdout.write(json.dumps(report, indent=2, default=str))
        else:
            self.stdout.write(format_activation_human(report))

        if options.get("fail_on_blocked") and not report.get("overall_ready_for_real_integration"):
            raise CommandError(report.get("overall") or "NOT READY FOR REAL INTEGRATION")
