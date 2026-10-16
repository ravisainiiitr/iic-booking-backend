"""
Equipment Under Maintenance handled day by day (``equipment.maintenance_auto``).

  manage.py maintenance_auto                      # dry run: what adopting each equipment would do (read-only)
  manage.py maintenance_auto --adopt XPS --apply  # start the mechanism for equipment already Under Maintenance
  manage.py maintenance_auto --daily-check        # run the 08:30 check now (idempotent per day)

Adopting puts today's bookings on hold for the users' choice, emails users with upcoming bookings, pauses the
results-overdue clock of past bookings awaiting results and emails the OIC / Lab Operators. Ask before applying.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Dry run / adopt / daily check for automatic equipment maintenance handling."

    def add_arguments(self, parser):
        parser.add_argument("--adopt", nargs="+", metavar="CODE", help="Equipment codes (or ids) to adopt.")
        parser.add_argument("--apply", action="store_true", help="Actually adopt (default: dry run).")
        parser.add_argument("--daily-check", action="store_true", help="Run the 08:30 check now.")

    def handle(self, *args, **opts):
        from iic_booking.equipment.maintenance_auto import daily_check, dry_run_report_text, start_period
        from iic_booking.equipment.maintenance_policy import is_equipment_under_maintenance_status
        from iic_booking.equipment.models import Equipment

        if opts["daily_check"]:
            self.stdout.write(str(daily_check()))
            return
        self.stdout.write(dry_run_report_text())
        codes = opts.get("adopt") or []
        if not codes:
            return
        if not opts["apply"]:
            self.stdout.write("Dry run only: add --apply to adopt.")
            return
        for code in codes:
            eq = Equipment.objects.filter(code=code).first()
            if eq is None and str(code).isdigit():
                eq = Equipment.objects.filter(pk=int(code)).first()
            if eq is None:
                raise CommandError(f"Equipment {code!r} not found.")
            if not is_equipment_under_maintenance_status(eq.status):
                raise CommandError(f"Equipment {eq.code} is not Under Maintenance (status {eq.status}).")
            with transaction.atomic():
                out = start_period(eq, adopted=True)
            if out.already_active:
                self.stdout.write(f"{eq.code}: already handled (period {out.period.pk}).")
                continue
            self.stdout.write(
                f"{eq.code}: adopted period={out.period.pk} slots_marked={len(out.slots_marked)} "
                f"on_hold={len(out.bookings_on_hold)} bookings_notified={len(out.bookings_notified)} "
                f"users_notified={len(out.users_notified)} results_paused={len(out.results_paused)}"
            )
