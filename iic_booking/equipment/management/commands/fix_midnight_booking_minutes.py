"""Store the slot length of bookings whose slots were extended to end at midnight (24:00).

Slot Ends 24:00 Fix and Full-Day Slots NMR TXI moved Daily Slots of these equipment from 23:59 (or 23:59:59)
to midnight, but the bookings on them kept ``total_time_minutes`` from the shorter slots (1439 instead of 1440,
719 instead of 720). For bookings (any status) with at least one slot of the listed equipment ending at local
midnight, the length is recomputed as at booking time (sum of the minutes of the booking's slots) and stored
when it is higher by at most one minute per midnight slot. Other differences and bookings in an input-edit
payment window are reported and left alone.

Only ``total_time_minutes`` is written, through ``QuerySet.update`` (no ``save()``, no signals): charges,
amounts, wallet, statuses, events and notifications are not touched; the charge fields of the updated bookings
are compared before and after and the change is rolled back if any differ.

Dry run by default; ``--apply`` writes. Output is equipment codes, booking ids and minutes only.
"""

from __future__ import annotations

from collections import Counter
from datetime import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

DEFAULT_EQUIPMENT_IDS = (338, 46, 8, 51, 7, 4, 82, 94)
UNCHANGED_FIELDS = (
    "total_charge",
    "charge_breakdown",
    "amount_due",
    "wallet_amount_applied",
    "reward_discount_amount",
    "charge_recalculation_pending_amount",
    "status",
    "input_values",
)


class Command(BaseCommand):
    help = "Store the slot length of bookings on slots extended to midnight (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write total_time_minutes (default: dry run).")
        parser.add_argument("--equipment-id", type=int, action="append", default=[])

    def handle(self, *args, **options):
        result = fix_midnight_booking_minutes(
            apply=options["apply"], equipment_ids=options["equipment_id"] or DEFAULT_EQUIPMENT_IDS
        )
        self.stdout.write(f"mode={'APPLY' if options['apply'] else 'DRY RUN'}")
        for code, row in result["per_equipment"].items():
            changes = ",".join(f"{k}:{n}" for k, n in sorted(row.pop("changes").items())) or "-"
            self.stdout.write(f"eq={code} " + " ".join(f"{k}={v}" for k, v in row.items()) + f" changes={changes}")
        for row in result["fixes"]:
            self.stdout.write("fix " + " ".join(f"{k}={v}" for k, v in row.items()))
        for row in result["skipped"]:
            self.stdout.write("skip " + " ".join(f"{k}={v}" for k, v in row.items()))
        self.stdout.write(f"updated={result['updated']}")
        if options["apply"]:
            self.stdout.write(f"charges_unchanged={result['charges_unchanged']}")
        self.stdout.write(f"remaining_to_fix={result['remaining_to_fix']}")


def _ends_at_midnight(start, end) -> bool:
    if not start or not end or end <= start:
        return False
    return timezone.localtime(end).time() == time(0)


def _charge_state(booking_ids):
    from iic_booking.equipment.models import Booking

    return {
        row["booking_id"]: row
        for row in Booking.objects.filter(pk__in=booking_ids).values("booking_id", *UNCHANGED_FIELDS)
    }


def fix_midnight_booking_minutes(*, apply: bool, equipment_ids) -> dict:
    from iic_booking.equipment.models import Booking, BookingEvent, DailySlot, Equipment
    from iic_booking.equipment.quota_utils import remaining_slot_minutes_for_booking

    codes = dict(Equipment.objects.filter(pk__in=equipment_ids).values_list("pk", "code"))
    midnight_eq: dict[int, int] = {}
    for booking_id, start, end, eq_id in (
        DailySlot.objects.filter(slot_master__equipment_id__in=equipment_ids, booking__isnull=False)
        .order_by("start_datetime", "id")
        .values_list("booking_id", "start_datetime", "end_datetime", "slot_master__equipment_id")
    ):
        if _ends_at_midnight(start, end):
            midnight_eq.setdefault(booking_id, eq_id)

    per_equipment = {
        codes.get(eq_id, f"id{eq_id}"): {
            "eq_id": eq_id,
            "bookings": 0,
            "to_fix": 0,
            "updated": 0,
            "already_ok": 0,
            "skipped": 0,
            "changes": Counter(),
        }
        for eq_id in equipment_ids
    }
    fixes, skipped, plan = [], [], []
    bookings = (
        Booking.objects.filter(pk__in=list(midnight_eq))
        .only("booking_id", "virtual_booking_id", "status", "total_time_minutes",
              "charge_recalculation_pay_deadline", "charge_recalculation_revert_snapshot")
        .prefetch_related("daily_slots")
        .order_by("booking_id")
    )
    for booking in bookings:
        code = codes.get(midnight_eq[booking.pk], f"id{midnight_eq[booking.pk]}")
        line = per_equipment[code]
        line["bookings"] += 1
        old = int(booking.total_time_minutes or 0)
        new = remaining_slot_minutes_for_booking(booking)
        if new == old:
            line["already_ok"] += 1
            continue
        midnight_slots = sum(
            1 for s in booking.daily_slots.all() if _ends_at_midnight(s.start_datetime, s.end_datetime)
        )
        row = {
            "booking_id": booking.pk,
            "ref": (booking.virtual_booking_id or "-").replace(" ", "_"),
            "eq": code.replace(" ", "_"),
            "status": booking.status,
            "old": old,
            "new": new,
            "midnight_slots": midnight_slots,
        }
        reason = None
        if booking.charge_recalculation_pay_deadline or booking.charge_recalculation_revert_snapshot:
            reason = "input_edit_payment_window"
        elif not 0 < new - old <= midnight_slots:
            reason = "difference_not_from_midnight_ends"
        if reason:
            line["skipped"] += 1
            skipped.append({**row, "reason": reason})
            continue
        line["to_fix"] += 1
        line["changes"][f"{old}->{new}"] += 1
        fixes.append(row)
        plan.append((booking.pk, old, new, code))

    written: set[int] = set()
    charges_unchanged = None
    if apply and plan:
        ids = [pk for pk, *_ in plan]
        with transaction.atomic():
            before = _charge_state(ids)
            events_before = BookingEvent.objects.filter(booking_id__in=ids).count()
            for pk, old, new, _ in plan:
                if Booking.objects.filter(pk=pk, total_time_minutes=old).update(total_time_minutes=new):
                    written.add(pk)
            after = _charge_state(ids)
            events_after = BookingEvent.objects.filter(booking_id__in=ids).count()
            charges_unchanged = before == after and events_before == events_after
            if not charges_unchanged:
                transaction.set_rollback(True)
                written.clear()
        for pk, _, _, code in plan:
            per_equipment[code]["updated"] += int(pk in written)
        for row in fixes:
            row["updated"] = int(row["booking_id"] in written)
    elif apply:
        charges_unchanged = True
    updated = len(written)

    remaining = sum(
        1
        for pk, old, new, _ in plan
        if Booking.objects.filter(pk=pk).values_list("total_time_minutes", flat=True).first() != new
    )
    return {
        "per_equipment": per_equipment,
        "fixes": fixes,
        "skipped": skipped,
        "updated": updated,
        "charges_unchanged": charges_unchanged,
        "remaining_to_fix": remaining,
    }
