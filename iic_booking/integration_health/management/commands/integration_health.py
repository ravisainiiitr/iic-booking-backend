"""Integration health from the command line (the nightly GitHub workflow runs it in the django container).

  integration_health                      checks + live checks + safe fixes, prints the result
  integration_health --nightly --email    the above, plus every booking of the last 24 hours step by step, emailed
                                          to the Main Administrators
  integration_health --ensure-schedule    create / update the 15-minute schedule (database beat)
  integration_health --no-fix             report only

Output is safe for public logs: booking IDs only, no names, emails, phone numbers, tokens or keys. Exit code 1 when
something is failing, so the workflow run shows red.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Check that bookings go through every step (I-STEM, WhatsApp, payments, email) and fix what is safe to fix."

    def add_arguments(self, parser):
        parser.add_argument("--nightly", action="store_true", help="Also follow every booking of the last 24 hours.")
        parser.add_argument("--email", action="store_true", help="Email the report to the Main Administrators.")
        parser.add_argument("--no-fix", action="store_true", help="Report only; no automatic fixes.")
        parser.add_argument("--ensure-schedule", action="store_true", help="Create the 15-minute schedule and exit.")

    def handle(self, *args, **opts):
        from iic_booking.integration_health import report
        from iic_booking.integration_health.schedule import ensure_schedule

        if opts["ensure_schedule"]:
            self.stdout.write(f"schedule: {ensure_schedule()}")
            return
        if opts["nightly"]:
            payload = report.nightly(email=opts["email"])
        else:
            payload = report.run(heal=not opts["no_fix"], live=True, alert=False)
        self.stdout.write(f"overall: {payload['overall']}  ({payload['duration_ms']} ms)")
        for c in payload["checks"]:
            self.stdout.write(f"[{c['status'].upper():4}] {c['title']}: {c['summary']}")
            for item in c["items"]:
                self.stdout.write(f"         - {item}")
        self.stdout.write(f"fixes: {payload['fixes']}")
        if "journey" in payload:
            self.stdout.write("--- last 24 hours, booking by booking ---")
            self.stdout.write(report.journey_text(payload["journey"]))
        if payload["overall"] == "fail" or (payload.get("journey") or {}).get("problems"):
            raise SystemExit(1)
