"""Read-only reconciliation of administrator-approved Wallet Credit Facility ledgers."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from iic_booking.users.models.wallet_credit_facility import WalletCreditFacility
from iic_booking.users.wallet_credit_facility_v2 import reconcile_facility


class Command(BaseCommand):
    help = (
        "READ-ONLY: reconcile Wallet Credit Facility approved/credited/repaid/outstanding "
        "against invoices and payments. Does not modify balances or transactions."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=str,
            default="",
            help="Optional path to write JSON report (read-only analysis).",
        )
        parser.add_argument(
            "--reference",
            type=str,
            default="",
            help="Optional public_reference to reconcile a single facility.",
        )

    def handle(self, *args, **options):
        qs = WalletCreditFacility.objects.all().order_by("id")
        ref = (options.get("reference") or "").strip()
        if ref:
            qs = qs.filter(public_reference=ref)
        rows = [reconcile_facility(f) for f in qs]
        inconsistent = [r for r in rows if not r.get("consistent")]
        report = {
            "total_facilities": len(rows),
            "inconsistent_count": len(inconsistent),
            "facilities": rows,
            "read_only": True,
        }
        self.stdout.write(
            self.style.SUCCESS(
                f"Reconciled {len(rows)} facilities; inconsistent={len(inconsistent)} (read-only)."
            )
        )
        out = (options.get("output") or "").strip()
        if out:
            path = Path(out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            self.stdout.write(f"Wrote {path}")
        else:
            self.stdout.write(json.dumps(report, indent=2))
