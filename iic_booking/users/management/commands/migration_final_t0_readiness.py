"""Phase 10F — final T0 GO/NO-GO readiness report (READ-ONLY)."""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.phase10f_final_t0_readiness import build_final_t0_readiness_report

DEFAULT_ARTIFACT = Path("docs/release/migration/phase10f_final_t0_readiness.json")


class Command(BaseCommand):
    help = "Phase 10F read-only final T0 GO/NO-GO report. Does NOT activate T0 or write production data."

    def add_arguments(self, parser):
        parser.add_argument("--column-map-file", type=str, default="")
        parser.add_argument("--json-out", type=str, default="")
        parser.add_argument("--default-artifact", action="store_true", help="Write docs/release/migration/phase10f_final_t0_readiness.json")
        parser.add_argument("--backup-verified", action="store_true", help="Operator confirms production backup verified")
        parser.add_argument("--backend-release-tag", type=str, default="")
        parser.add_argument("--backend-merge-sha", type=str, default="")
        parser.add_argument("--backend-pr", type=str, default="")
        parser.add_argument("--frontend-release-tag", type=str, default="")
        parser.add_argument("--frontend-merge-sha", type=str, default="")
        parser.add_argument("--frontend-pr", type=str, default="")
        parser.add_argument("--conflicts-resolved", action="store_true")

    def handle(self, *args, **options):
        report = build_final_t0_readiness_report(
            column_map_file=(options.get("column_map_file") or "").strip(),
            backup_verified=bool(options.get("backup_verified")),
            backend_release_tag=(options.get("backend_release_tag") or "").strip(),
            backend_merge_sha=(options.get("backend_merge_sha") or "").strip(),
            frontend_release_tag=(options.get("frontend_release_tag") or "").strip(),
            frontend_merge_sha=(options.get("frontend_merge_sha") or "").strip(),
            backend_pr=(options.get("backend_pr") or "").strip(),
            frontend_pr=(options.get("frontend_pr") or "").strip(),
            conflicts_resolved_or_excluded=bool(options.get("conflicts_resolved")),
        )
        payload = json.dumps(report, indent=2, default=str)
        self.stdout.write(payload)

        out = (options.get("json_out") or "").strip()
        if options.get("default_artifact"):
            out = str(Path(getattr(settings, "BASE_DIR", ".")) / DEFAULT_ARTIFACT)
        if out:
            path = Path(out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload + "\n", encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote {path}"))

        if report["verdict"] == "READY FOR FINAL T0 REVIEW":
            self.stdout.write(self.style.WARNING(report["verdict"]))
        else:
            self.stdout.write(self.style.ERROR(report["verdict"]))
