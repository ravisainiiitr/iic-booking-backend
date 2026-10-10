"""Read-only before/after utilization factor per equipment.

Before: booked slot hours ÷ all slot hours. After: the same hours inside the weekly view window on working days
(``iic_booking.equipment.utilization``).

Usage:
  python manage.py utilization_report                       # last 30 days up to today
  python manage.py utilization_report --from 2026-10-01 --to 2026-10-31
  python manage.py utilization_report --equipment 12,XRD1 --all --csv
"""

import csv
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from iic_booking.equipment.models import Equipment
from iic_booking.equipment.utilization import UtilizationTally, compute_utilization_by_equipment, view_window_for

EQUIPMENT_FIELDS = ("equipment_id", "code", "name", "weekly_view_time_from", "weekly_view_time_to")
COLUMNS = [
    "equipment_id", "code", "name", "window", "slot_hours_all", "booked_hours_all", "before_pct",
    "slot_hours_window", "booked_hours_window", "after_pct", "booked_hours_outside_window",
]


def _pct(value) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"


def _parse_date(value: str, flag: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"{flag} must be YYYY-MM-DD") from exc


class Command(BaseCommand):
    help = "Print utilization factor before/after the weekly view window, weekend and holiday rules (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="date_from", default=None, help="Start date YYYY-MM-DD (default: 29 days ago)")
        parser.add_argument("--to", dest="date_to", default=None, help="End date YYYY-MM-DD (default: today)")
        parser.add_argument("--equipment", default="", help="Comma-separated equipment ids or codes (default: all)")
        parser.add_argument("--all", action="store_true", help="Include equipment without slots in the period")
        parser.add_argument("--csv", action="store_true", help="CSV output")

    def handle(self, *args, **options):
        today = timezone.localdate()
        end = _parse_date(options["date_to"], "--to") if options["date_to"] else today
        start = _parse_date(options["date_from"], "--from") if options["date_from"] else end - timedelta(days=29)
        if start > end:
            raise CommandError("--from must not be after --to")

        qs = Equipment.objects.only(*EQUIPMENT_FIELDS)
        tokens = [t.strip() for t in (options["equipment"] or "").split(",") if t.strip()]
        if tokens:
            ids = [int(t) for t in tokens if t.isdigit()]
            codes = [t for t in tokens if not t.isdigit()]
            qs = qs.filter(Q(equipment_id__in=ids) | Q(code__in=codes))
        equipment = {e.equipment_id: e for e in qs.order_by("code")}

        with transaction.atomic():
            tallies = compute_utilization_by_equipment(list(equipment), start, end)
            transaction.set_rollback(True)

        rows = []
        total = UtilizationTally()
        for eid, tally in sorted(tallies.items(), key=lambda kv: (equipment.get(kv[0]).code if equipment.get(kv[0]) else "")):
            if not tally.slots and not options["all"]:
                continue
            eq = equipment.get(eid) or Equipment.objects.only(*EQUIPMENT_FIELDS).filter(equipment_id=eid).first()
            total.merge(tally)
            rows.append([
                eid, getattr(eq, "code", ""), getattr(eq, "name", ""), view_window_for(eq).label(),
                f"{tally.all_slot_hours:.1f}", f"{tally.all_slot_booked_hours:.1f}", _pct(tally.all_slot_factor),
                f"{tally.available_hours:.1f}", f"{tally.booked_hours:.1f}", _pct(tally.factor),
                f"{tally.booked_hours_outside_window:.1f}",
            ])
        rows.append([
            "", "ALL", f"{len(rows)} equipment", "",
            f"{total.all_slot_hours:.1f}", f"{total.all_slot_booked_hours:.1f}", _pct(total.all_slot_factor),
            f"{total.available_hours:.1f}", f"{total.booked_hours:.1f}", _pct(total.factor),
            f"{total.booked_hours_outside_window:.1f}",
        ])

        self.stdout.write(f"period={start.isoformat()}..{end.isoformat()} equipment={len(rows) - 1}")
        if options["csv"]:
            writer = csv.writer(self.stdout)
            writer.writerow(COLUMNS)
            writer.writerows(rows)
            return
        widths = [max(len(str(r[i])) for r in [COLUMNS] + rows) for i in range(len(COLUMNS))]
        widths[2] = min(widths[2], 40)
        for r in [COLUMNS] + rows:
            self.stdout.write("  ".join(str(v)[: widths[i]].ljust(widths[i]) for i, v in enumerate(r)))
