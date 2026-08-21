"""Deterministic REAL integration preflight (presence only — never prints secrets).

Usage (staging container / manage.py):

  python manage.py real_integration_preflight
  python manage.py real_integration_preflight --json
  python manage.py real_integration_preflight --write-docs

Exit code 0 always for reporting; overall readiness is in the report body.
Use --fail-on-blocked to exit 1 when overall is not ready.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from iic_booking.users.legacy_ledger.real_integration_guards import (
    assert_staging_environment,
    build_real_integration_preflight,
    format_preflight_human,
)


class Command(BaseCommand):
    help = "Run REAL integration credential/preflight checks without exposing secrets."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Emit JSON only")
        parser.add_argument(
            "--write-docs",
            action="store_true",
            help="Write docs/release/migration/real_integration_preflight.json",
        )
        parser.add_argument(
            "--backend-commit",
            default="",
            help="Optional backend git SHA for evidence",
        )
        parser.add_argument(
            "--frontend-commit",
            default="",
            help="Optional frontend git SHA for evidence",
        )
        parser.add_argument(
            "--fail-on-blocked",
            action="store_true",
            help="Exit 1 when overall is NOT READY FOR REAL INTEGRATION",
        )
        parser.add_argument(
            "--skip-live-probes",
            action="store_true",
            help="Config presence only (default for unit tests). Live probes run unless skipped.",
        )

    def handle(self, *args, **options):
        try:
            assert_staging_environment()
        except Exception as exc:  # noqa: BLE001
            raise CommandError(str(exc)) from exc

        # Operator docs/preflight always include live probes unless explicitly skipped.
        include_live = not options.get("skip_live_probes")
        if options.get("write_docs"):
            include_live = True

        report = build_real_integration_preflight(
            backend_commit=options.get("backend_commit") or "",
            frontend_commit=options.get("frontend_commit") or "",
            include_live_probes=include_live,
        )

        if options.get("write_docs"):
            out = Path(settings.BASE_DIR) / "docs" / "release" / "migration" / "real_integration_preflight.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            self.stderr.write(f"Wrote {out}")

        if options.get("json"):
            self.stdout.write(json.dumps(report, indent=2))
        else:
            self.stdout.write(format_preflight_human(report))

        if options.get("fail_on_blocked") and not report.get("overall_ready_for_real_integration"):
            raise CommandError(report.get("overall") or "NOT READY FOR REAL INTEGRATION")
