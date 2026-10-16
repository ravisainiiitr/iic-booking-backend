"""
Equipment Under Maintenance handled day by day (``equipment.maintenance_auto``).

  manage.py maintenance_auto                      # dry run: what adopting each equipment would do (read-only)
  manage.py maintenance_auto --adopt XPS --apply  # start the mechanism for equipment already Under Maintenance
  manage.py maintenance_auto --daily-check        # run the 08:30 check now (idempotent per day)
  manage.py maintenance_auto --mark-repair 344 --reason "..." --apply  # status -> REPAIR via the normal save path
  manage.py maintenance_auto --refund-dry-run     # read-only: what the 5-day auto refund would refund now

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
        parser.add_argument(
            "--mark-repair", metavar="CODE",
            help="Set the equipment to Under Maintenance (REPAIR) through the normal status path (with --apply).",
        )
        parser.add_argument("--reason", default="", help="Disruption reason recorded with --mark-repair.")
        parser.add_argument(
            "--refund-dry-run", action="store_true",
            help="Read-only: undecided disruption bookings the 5-day auto refund would refund now.",
        )

    def handle(self, *args, **opts):
        from iic_booking.equipment.maintenance_auto import daily_check, dry_run_report_text, start_period
        from iic_booking.equipment.maintenance_policy import is_equipment_under_maintenance_status
        from iic_booking.equipment.models import Equipment

        if opts["daily_check"]:
            self.stdout.write(str(daily_check()))
            return
        if opts["refund_dry_run"]:
            self._refund_dry_run()
            return
        if opts["mark_repair"]:
            self._mark_repair(opts["mark_repair"], opts["reason"], opts["apply"])
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

    def _equipment(self, code):
        from iic_booking.equipment.models import Equipment

        eq = Equipment.objects.filter(code=code).first()
        if eq is None and str(code).isdigit():
            eq = Equipment.objects.filter(pk=int(code)).first()
        if eq is None:
            raise CommandError(f"Equipment {code!r} not found.")
        return eq

    def _mark_repair(self, code, reason, apply):
        from iic_booking.equipment.disruption_service import DisruptionInput
        from iic_booking.equipment.maintenance_auto import active_period
        from iic_booking.equipment.models import EquipmentStatus

        eq = self._equipment(code)
        self.stdout.write(f"{eq.code} (id={eq.pk}): status={eq.status} period={'yes' if active_period(eq) else 'no'}")
        if eq.status == EquipmentStatus.REPAIR:
            raise CommandError(f"{eq.code} is already Under Maintenance (REPAIR).")
        if not apply:
            self.stdout.write("Dry run only: add --apply to set it Under Maintenance (REPAIR).")
            return
        eq.status = EquipmentStatus.REPAIR
        eq._disruption_input = DisruptionInput(source="EQUIPMENT_STATUS", reason=reason)
        with transaction.atomic():
            eq.save()
        out = getattr(eq, "_maintenance_outcome", None)
        if out is None:
            self.stdout.write(f"{eq.code}: status=REPAIR; no maintenance period was started.")
            return
        self.stdout.write(
            f"{eq.code}: status=REPAIR period={out.period.pk} slots_marked={len(out.slots_marked)} "
            f"on_hold={len(out.bookings_on_hold)} bookings_notified={len(out.bookings_notified)} "
            f"users_notified={len(out.users_notified)} results_paused={len(out.results_paused)}"
        )

    def _refund_dry_run(self):
        from iic_booking.equipment.maintenance_policy import auto_cancel_dry_run

        report = auto_cancel_dry_run()
        self.stdout.write(
            f"refund dry run now={report['now']} count={report['count']} total_refund={report['total_refund']} "
            f"flagged={report['flagged']}"
        )
        for r in report["rows"]:
            self.stdout.write(
                f"  booking={r['booking_id']} eq={r['equipment_code']} status={r['status']} kind={r['disruption_kind']}"
                f" disrupted_on={r['disrupted_on']} deadline={r['deadline']} refund={r['refund_amount']}"
                f" charged={r['charged']} already_credited={r['already_credited']} flags={','.join(r['flags']) or '-'}"
            )
