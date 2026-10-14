"""Monthly usage reports: dry run (default, counts only) or a [TEST] send to test accounts.

Output is safe for public CI logs: counts and internal user ids only, never names or email addresses.

    python manage.py monthly_usage_reports --month 2026-09
    python manage.py monthly_usage_reports --month 2026-10 --apply-test [--user 78]
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from iic_booking.communication.models import MonthlyReportSettings
from iic_booking.communication.monthly_report_data import ReportMonth
from iic_booking.communication.monthly_reports import AUDIENCES, dry_run_counts, send_test_reports


class Command(BaseCommand):
    help = "Dry-run counts for the monthly usage reports, or a [TEST] send to test accounts only."

    def add_arguments(self, parser):
        parser.add_argument("--month", action="append", default=[], help="YYYY-MM (repeatable; default: last month)")
        parser.add_argument("--audience", action="append", choices=AUDIENCES, default=[])
        parser.add_argument("--apply-test", action="store_true", help="Send [TEST] reports to test accounts only")
        parser.add_argument("--user", action="append", type=int, default=[], help="Test account id (with --apply-test)")

    def handle(self, *args, **opts):
        try:
            months = [ReportMonth.parse(m) for m in opts["month"]] or [ReportMonth.previous_to()]
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        row = MonthlyReportSettings.get_singleton()
        self.stdout.write(
            f"Master switch 'Send monthly usage reports': {'ON' if row.enabled else 'OFF'}; "
            f"audiences on: faculty={row.faculty_enabled} oic={row.oic_enabled} hod={row.hod_enabled}"
        )
        audiences = opts["audience"] or list(AUDIENCES)
        for month in months:
            if opts["apply_test"]:
                result = send_test_reports(month, opts["user"] or None)
            else:
                result = {"mode": "dry-run", **dry_run_counts(month, audiences)}
            self.stdout.write(json.dumps(result, default=str, indent=2))
