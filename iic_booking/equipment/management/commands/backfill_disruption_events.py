"""Create DisruptionEvent records from slots and equipment that are already disrupted (records only).

Slots in Under Maintenance or Operator Absent, and Other Reasons blocks (BLOCKED on a working day that is not a
holiday, repeat-rule, training or legacy-migration block), are grouped into runs per equipment with the same
rules as live recording. Equipment currently Under Maintenance gets one whole-equipment event. Slots already
linked to an event are skipped, so the command is idempotent. Slots and bookings are never changed.

Dry run by default; ``--apply`` writes. Output is counts and ids only.
"""

from __future__ import annotations

from collections import Counter
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone


class Command(BaseCommand):
    help = "Backfill disruption events from existing slot / equipment status (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the events (default: dry run).")
        parser.add_argument("--equipment-id", type=int, action="append", default=[])

    def handle(self, *args, **options):
        result = backfill_disruption_events(apply=options["apply"], equipment_ids=options["equipment_id"] or None)
        mode = "APPLY" if options["apply"] else "DRY RUN"
        self.stdout.write(f"mode={mode}")
        for key in (
            "equipment_scanned",
            "candidate_slots",
            "skipped_already_linked",
            "slot_events",
            "slot_events_with_reason",
            "equipment_events",
            "linked_slots",
        ):
            self.stdout.write(f"{key}={result[key]}")
        for dtype, n in sorted(result["events_by_type"].items()):
            self.stdout.write(f"events_by_type.{dtype}={n}")
        self.stdout.write(f"equipment_ids_with_events={result['equipment_ids'][:200]}")


def _other_reason_exclusions(slot_ids):
    """Slot ids among ``slot_ids`` that are BLOCKED for a reason that is not a disruption."""
    from iic_booking.equipment.models import RecurringSlotBlockRuleSlot

    excluded = set(
        RecurringSlotBlockRuleSlot.objects.filter(daily_slot_id__in=slot_ids).values_list("daily_slot_id", flat=True)
    )
    try:
        from iic_booking.training.models import SessionSlotReservation

        excluded |= set(
            SessionSlotReservation.objects.filter(daily_slot_id__in=slot_ids).values_list("daily_slot_id", flat=True)
        )
    except Exception:
        pass
    return excluded


def backfill_disruption_events(*, apply: bool, equipment_ids=None) -> dict:
    from iic_booking.equipment.disruption_service import (
        _group_runs,
        clean_text,
        disruption_type_for_slot_status,
    )
    from iic_booking.equipment.maintenance_policy import is_equipment_under_maintenance_status
    from iic_booking.equipment.models import (
        Booking,
        BookingStatus,
        DailySlot,
        DisruptionEvent,
        DisruptionEventEdit,
        DisruptionEventSlot,
        DisruptionScope,
        DisruptionSource,
        DisruptionType,
        Equipment,
        Holiday,
        SlotStatus,
    )

    now = timezone.now()
    stats = {
        "equipment_scanned": 0,
        "candidate_slots": 0,
        "skipped_already_linked": 0,
        "slot_events": 0,
        "slot_events_with_reason": 0,
        "equipment_events": 0,
        "linked_slots": 0,
        "events_by_type": Counter(),
        "equipment_ids": [],
    }
    statuses = [SlotStatus.UNDER_MAINTENANCE, SlotStatus.OPERATOR_ABSENT, SlotStatus.SCHEDULED_MAINTENANCE,
                SlotStatus.BLOCKED]
    equipment_qs = Equipment.objects.all().only("equipment_id", "status", "name", "code")
    if equipment_ids:
        equipment_qs = equipment_qs.filter(equipment_id__in=equipment_ids)
    disrupted_booking_statuses = [
        BookingStatus.UNDER_MAINTENANCE,
        BookingStatus.ABSENT,
        BookingStatus.OTHER_DISRUPTION,
        BookingStatus.DISRUPTION_PENDING,
    ]
    touched = set()
    for equipment in equipment_qs.iterator():
        stats["equipment_scanned"] += 1
        rows = list(
            DailySlot.objects.filter(slot_master__equipment=equipment, status__in=statuses)
            .order_by("start_datetime")
            .values("id", "start_datetime", "end_datetime", "status", "blocked_label", "date", "booking_id")
        )
        under_maintenance_now = is_equipment_under_maintenance_status(equipment.status)
        has_equipment_event = DisruptionEvent.objects.filter(
            equipment=equipment, scope=DisruptionScope.EQUIPMENT, ended_at__isnull=True
        ).exists()

        equipment_run_start = None
        if under_maintenance_now and not has_equipment_event:
            # Start of the whole-equipment event: earliest Under Maintenance slot of the unbroken run reaching now.
            um = [r for r in rows if r["status"] == SlotStatus.UNDER_MAINTENANCE]
            past = [r for r in um if r["start_datetime"] <= now]
            if past:
                um_ids = {r["id"] for r in past}
                ordered = list(
                    DailySlot.objects.filter(
                        slot_master__equipment=equipment, start_datetime__lte=now,
                        start_datetime__gte=past[0]["start_datetime"],
                    )
                    .order_by("-start_datetime")
                    .values_list("id", "start_datetime", "status")
                )
                start = None
                for sid, st, status in ordered:
                    if sid in um_ids:
                        start = st
                    elif status in (SlotStatus.AVAILABLE, SlotStatus.BOOKED, SlotStatus.OPERATOR_ABSENT):
                        break
                equipment_run_start = start
            equipment_run_start = equipment_run_start or now

        if not rows and not (under_maintenance_now and not has_equipment_event):
            continue

        linked = set(
            DisruptionEventSlot.objects.filter(daily_slot_id__in=[r["id"] for r in rows]).values_list(
                "daily_slot_id", flat=True
            )
        )
        blocked_ids = [r["id"] for r in rows if r["status"] == SlotStatus.BLOCKED]
        excluded = _other_reason_exclusions(blocked_ids) if blocked_ids else set()
        holidays = set()
        if blocked_ids:
            dates = [r["date"] for r in rows if r["status"] == SlotStatus.BLOCKED]
            holidays = set(Holiday.get_holidays_in_range(min(dates), max(dates)).keys())

        by_type = defaultdict(list)
        for r in rows:
            if r["id"] in linked:
                stats["skipped_already_linked"] += 1
                continue
            if r["status"] == SlotStatus.BLOCKED:
                label = r["blocked_label"] or ""
                if r["id"] in excluded or r["date"] in holidays or label.startswith("LEGACY_MIGRATION"):
                    continue
            if (
                r["status"] == SlotStatus.UNDER_MAINTENANCE
                and equipment_run_start is not None
                and r["start_datetime"] >= equipment_run_start
            ):
                continue  # covered by the whole-equipment event
            dtype = disruption_type_for_slot_status(r["status"])
            by_type[dtype].append(
                {
                    "id": r["id"],
                    "start": r["start_datetime"],
                    "end": r["end_datetime"],
                    "label": r["blocked_label"] or "",
                }
            )
            stats["candidate_slots"] += 1

        plans = []
        for dtype, slot_rows in by_type.items():
            for run in _group_runs(equipment, slot_rows, dtype):
                labels = [r["label"] for r in run if r["label"]] if dtype == DisruptionType.OTHER else []
                reason = clean_text(labels[0]) if labels and len(set(labels)) == 1 else ""
                plans.append((dtype, run, reason))

        if not plans and equipment_run_start is None:
            continue
        touched.add(equipment.equipment_id)
        if not apply:
            for dtype, run, reason in plans:
                stats["slot_events"] += 1
                stats["slot_events_with_reason"] += 1 if reason else 0
                stats["events_by_type"][dtype] += 1
                stats["linked_slots"] += len(run)
            if equipment_run_start is not None:
                stats["equipment_events"] += 1
                stats["events_by_type"]["UNDER_MAINTENANCE"] += 1
            continue

        with transaction.atomic():
            for dtype, run, reason in plans:
                start = min(r["start"] for r in run)
                end = max(r["end"] for r in run)
                bookings = (
                    Booking.objects.filter(
                        equipment=equipment,
                        status__in=disrupted_booking_statuses,
                        released_slot_range__start_datetime__lt=end,
                        released_slot_range__end_datetime__gt=start,
                    )
                    .values("booking_id")
                    .distinct()
                    .count()
                )
                event = DisruptionEvent.objects.create(
                    equipment=equipment,
                    disruption_type=dtype,
                    scope=DisruptionScope.SLOTS,
                    source=DisruptionSource.BACKFILL,
                    start_at=start,
                    end_at=end,
                    started_at=min(start, now),
                    reason=reason,
                    slots_affected=len(run),
                    bookings_affected=bookings,
                    backfilled=True,
                )
                DisruptionEventSlot.objects.bulk_create(
                    [
                        DisruptionEventSlot(
                            event=event, daily_slot_id=r["id"], start_datetime=r["start"], end_datetime=r["end"]
                        )
                        for r in run
                    ]
                )
                DisruptionEventEdit.objects.create(event=event, kind="created", note="Recorded from earlier data")
                stats["slot_events"] += 1
                stats["slot_events_with_reason"] += 1 if reason else 0
                stats["events_by_type"][dtype] += 1
                stats["linked_slots"] += len(run)
            if equipment_run_start is not None:
                event = DisruptionEvent.objects.create(
                    equipment=equipment,
                    disruption_type=DisruptionType.UNDER_MAINTENANCE,
                    scope=DisruptionScope.EQUIPMENT,
                    source=DisruptionSource.BACKFILL,
                    start_at=equipment_run_start,
                    started_at=min(equipment_run_start, now),
                    backfilled=True,
                )
                DisruptionEventEdit.objects.create(
                    event=event, kind="created", note="Equipment was Under Maintenance (recorded from earlier data)"
                )
                stats["equipment_events"] += 1
                stats["events_by_type"]["UNDER_MAINTENANCE"] += 1
    stats["equipment_ids"] = sorted(touched)
    stats["events_by_type"] = dict(stats["events_by_type"])
    return stats
