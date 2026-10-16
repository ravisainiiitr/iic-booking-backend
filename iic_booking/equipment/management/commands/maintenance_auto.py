"""
Equipment Under Maintenance handled day by day (``equipment.maintenance_auto``).

  manage.py maintenance_auto                      # dry run: what adopting each equipment would do (read-only)
  manage.py maintenance_auto --adopt XPS --apply  # start the mechanism for equipment already Under Maintenance
  manage.py maintenance_auto --daily-check        # run the 08:30 check now (idempotent per day)
  manage.py maintenance_auto --mark-repair 344 --reason "..." --apply  # status -> REPAIR via the normal save path
  manage.py maintenance_auto --refund-dry-run     # read-only: what the 5-day auto refund would refund now
  manage.py maintenance_auto --hold-policy [--apply]          # existing maintenance holds -> automatic reschedule
  manage.py maintenance_auto --reschedule-preview             # read-only: where bookings on hold would go now
  manage.py maintenance_auto --enable-refund-job [--apply]    # schedule the 5-day auto refund (every 15 minutes)

Adopting puts today's bookings on hold (rescheduled automatically once the equipment is back), emails users with
upcoming bookings, pauses the results-overdue clock of past bookings awaiting results and emails the OIC / Lab
Operators. Ask before applying.
"""

REFUND_TASK = "equipment.process_maintenance_booking_deadlines"
REFUND_TASK_NAME = "Disruption bookings: 5-day automatic full refund (not maintenance holds)"

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
        parser.add_argument(
            "--hold-policy", action="store_true",
            help="Bookings put on hold by the mechanism: no decision deadline, rescheduled automatically (with --apply).",
        )
        parser.add_argument(
            "--reschedule-preview", action="store_true",
            help="Read-only: bookings on hold for maintenance and the slot each would get if placed now.",
        )
        parser.add_argument(
            "--enable-refund-job", action="store_true",
            help="Register the 5-day auto refund in Celery beat, duplicate-safe (with --apply).",
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
        if opts["hold_policy"]:
            self._hold_policy(opts["apply"])
            return
        if opts["reschedule_preview"]:
            self._reschedule_preview()
            return
        if opts["enable_refund_job"]:
            self._enable_refund_job(opts["apply"])
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

    def _hold_policy(self, apply):
        from iic_booking.equipment.booking_events import create_booking_event
        from iic_booking.equipment.models import (
            Booking,
            BookingDisruptionKind,
            BookingEventType,
            BookingStatus,
            MaintenanceNotification,
        )

        holds = Booking.objects.filter(
            status=BookingStatus.DISRUPTION_PENDING,
            disruption_kind=BookingDisruptionKind.MAINTENANCE,
            maintenance_auto_reschedule_pending=False,
        )
        by_mechanism = set(
            MaintenanceNotification.objects.filter(kind="DAY", booking__in=holds).values_list("booking_id", flat=True)
        )
        mine = sorted(by_mechanism)
        others = sorted(set(holds.values_list("booking_id", flat=True)) - by_mechanism)
        self.stdout.write(f"maintenance holds by the mechanism to convert={len(mine)} ids={mine}")
        self.stdout.write(
            f"other maintenance holds (left for the user's choice, no auto refund)={len(others)} ids={others[:60]}"
        )
        if not apply:
            self.stdout.write("Dry run only: add --apply to convert.")
            return
        converted = 0
        with transaction.atomic():
            for booking in Booking.objects.select_for_update().filter(pk__in=mine):
                if booking.status != BookingStatus.DISRUPTION_PENDING or booking.maintenance_auto_reschedule_pending:
                    continue
                booking.maintenance_auto_reschedule_pending = True
                booking.maintenance_decision_deadline_at = None
                booking.save(update_fields=["maintenance_auto_reschedule_pending", "maintenance_decision_deadline_at"])
                create_booking_event(
                    booking=booking,
                    event_type=BookingEventType.COMMENT,
                    created_by=None,
                    system_actor=True,
                    comment=(
                        "Maintenance hold policy updated: no decision deadline and no automatic refund. The booking is "
                        "moved automatically to the earliest free slot (in booking order) once the equipment is "
                        "Operational; the user may cancel for a full refund until then."
                    ),
                    metadata={"maintenance_auto_reschedule": True, "hold_policy_update": True},
                    send_notification=False,
                )
                converted += 1
        self.stdout.write(f"converted={converted}")

    def _reschedule_preview(self):
        from iic_booking.equipment.maintenance_reschedule import allocation_preview, settings_row
        from iic_booking.equipment.models import Booking, BookingStatus, Equipment

        cfg = settings_row()
        self.stdout.write(
            f"settings min_lead_hours={cfg.min_lead_hours} horizon_days={cfg.horizon_days} "
            f"deadline_extension_hours={cfg.deadline_extension_hours} cancel_grace_hours={cfg.cancel_grace_hours} "
            f"saved={'yes' if cfg.pk else 'no (defaults)'}"
        )
        ids = sorted(
            set(
                Booking.objects.filter(
                    status=BookingStatus.DISRUPTION_PENDING, maintenance_auto_reschedule_pending=True
                ).values_list("equipment_id", flat=True)
            )
        )
        for eq in Equipment.objects.filter(pk__in=ids).order_by("code"):
            report = allocation_preview(eq)
            self.stdout.write(f"{eq.code} (id={eq.pk}) status={eq.status} holds={report['holds']}")
            for r in report["rows"]:
                self.stdout.write(
                    f"  booking={r['booking_id']} slots={r['slots']} original={r['original_start']} "
                    f"new={r['new_start'] or '(no free run yet)'}"
                )

    def _enable_refund_job(self, apply):
        from django.utils import timezone
        from django_celery_beat.models import CrontabSchedule, PeriodicTask, PeriodicTasks

        rows = list(PeriodicTask.objects.filter(task=REFUND_TASK).order_by("pk"))
        self.stdout.write(
            f"existing rows for {REFUND_TASK}={len(rows)} enabled={[r.pk for r in rows if r.enabled]}"
        )
        if not apply:
            self.stdout.write("Dry run only: add --apply to schedule it every 15 minutes.")
            return
        with transaction.atomic():
            crontab = (
                CrontabSchedule.objects.filter(
                    minute="*/15", hour="*", day_of_week="*", day_of_month="*", month_of_year="*",
                    timezone="Asia/Kolkata",
                )
                .order_by("pk")
                .first()
            )
            if crontab is None:
                crontab = CrontabSchedule.objects.create(
                    minute="*/15", hour="*", day_of_week="*", day_of_month="*", month_of_year="*",
                    timezone="Asia/Kolkata",
                )
            if rows:
                keep = next((r for r in rows if r.enabled), rows[0])
                PeriodicTask.objects.filter(task=REFUND_TASK).exclude(pk=keep.pk).delete()
                keep.crontab = crontab
                keep.interval = None
                keep.enabled = True
                keep.save()
            else:
                keep = PeriodicTask.objects.create(name=REFUND_TASK_NAME, task=REFUND_TASK, crontab=crontab, enabled=True)
            PeriodicTasks.objects.update_or_create(ident=1, defaults={"last_update": timezone.now()})
        self.stdout.write(f"scheduled row={keep.pk} every 15 minutes enabled=True")

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
