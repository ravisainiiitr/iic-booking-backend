"""Link slots that sit in a disruption status without any disruption event (records only).

A slot is an orphan when it is Under Maintenance, Operator Absent, Scheduled Maintenance or an Other Reasons
block and has no event link at all. Slots whose only links are to deleted events are reported but left alone:
staff deleted those entries on purpose. Blocks that are not disruptions (repeat rules, training, holidays,
legacy migration) are skipped as in ``backfill_disruption_events``.

Under Maintenance orphans from the start of an open whole-equipment event onwards are linked to that event.
The rest are grouped into runs per equipment and type (same rules as live recording) and recorded as backfilled
slot events; Started by comes from the slot status change log when one user made the change, reason is left
blank. Slots and bookings are never changed.

Dry run by default; ``--apply`` writes. Output is equipment codes, counts and ids only.
"""

from __future__ import annotations

from collections import Counter
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from .backfill_disruption_events import _other_reason_exclusions


class Command(BaseCommand):
    help = "Link disruption-status slots that have no disruption event (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the links and events (default: dry run).")
        parser.add_argument("--equipment-id", type=int, action="append", default=[])
        parser.add_argument("--focus-code", default="", help="Also print this equipment's line when it has nothing.")

    def handle(self, *args, **options):
        result = repair_disruption_orphans(apply=options["apply"], equipment_ids=options["equipment_id"] or None)
        self.stdout.write(f"mode={'APPLY' if options['apply'] else 'DRY RUN'}")
        for key in ("equipment_scanned", "orphan_slots", "linked_to_equipment_event", "new_events",
                    "slots_in_new_events", "events_with_started_by", "deleted_event_only", "not_disruption"):
            self.stdout.write(f"{key}={result[key]}")
        for dtype, n in sorted(result["events_by_type"].items()):
            self.stdout.write(f"events_by_type.{dtype}={n}")
        focus = (options["focus_code"] or "").strip()
        printed_focus = False
        for code, row in sorted(result["per_equipment"].items()):
            printed_focus = printed_focus or code == focus
            self.stdout.write("eq=" + code + " " + " ".join(f"{k}={v}" for k, v in row.items()))
        if focus and not printed_focus:
            self.stdout.write(f"eq={focus} nothing to repair")
        if result["event_ids"]:
            self.stdout.write(f"event_ids={result['event_ids'][:300]}")


def _started_by_from_logs(equipment, run, status):
    """(user_id, changed_at) when every slot of ``run`` was last put into ``status`` by the same user."""
    from iic_booking.equipment.models import SlotStatusChangeLog

    ids = {r["id"] for r in run}
    last: dict[int, tuple] = {}
    for changed_at, user_id, slot_ids in (
        SlotStatusChangeLog.objects.filter(equipment=equipment, new_status=status)
        .order_by("changed_at")
        .values_list("changed_at", "changed_by_id", "slot_ids")
    ):
        for sid in slot_ids or []:
            if sid in ids:
                last[sid] = (user_id, changed_at)
    if len(last) != len(ids):
        return None, None
    users = {u for u, _ in last.values()}
    if len(users) != 1 or None in users:
        return None, None
    return users.pop(), min(t for _, t in last.values())


def repair_disruption_orphans(*, apply: bool, equipment_ids=None) -> dict:
    from iic_booking.equipment.disruption_service import _group_runs
    from iic_booking.equipment.disruption_service import disruption_type_for_slot_status
    from iic_booking.equipment.disruption_service import staff_role_for
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
    from django.contrib.auth import get_user_model

    now = timezone.now()
    stats = {
        "equipment_scanned": 0,
        "orphan_slots": 0,
        "linked_to_equipment_event": 0,
        "new_events": 0,
        "slots_in_new_events": 0,
        "events_with_started_by": 0,
        "deleted_event_only": 0,
        "not_disruption": 0,
        "events_by_type": Counter(),
        "per_equipment": {},
        "event_ids": [],
    }
    statuses = [SlotStatus.UNDER_MAINTENANCE, SlotStatus.OPERATOR_ABSENT, SlotStatus.SCHEDULED_MAINTENANCE,
                SlotStatus.BLOCKED]
    disrupted_booking_statuses = [
        BookingStatus.UNDER_MAINTENANCE,
        BookingStatus.ABSENT,
        BookingStatus.OTHER_DISRUPTION,
        BookingStatus.DISRUPTION_PENDING,
    ]
    users = get_user_model().objects
    equipment_qs = Equipment.objects.all().only("equipment_id", "status", "code")
    if equipment_ids:
        equipment_qs = equipment_qs.filter(equipment_id__in=equipment_ids)

    for equipment in equipment_qs.iterator():
        stats["equipment_scanned"] += 1
        rows = list(
            DailySlot.objects.filter(slot_master__equipment=equipment, status__in=statuses)
            .order_by("start_datetime", "id")
            .values("id", "start_datetime", "end_datetime", "status", "blocked_label", "date")
        )
        if not rows:
            continue
        link_state: dict[int, bool] = {}
        for sid, deleted in DisruptionEventSlot.objects.filter(daily_slot_id__in=[r["id"] for r in rows]).values_list(
            "daily_slot_id", "event__is_deleted"
        ):
            link_state[sid] = link_state.get(sid, False) or not deleted
        deleted_only = [r for r in rows if r["id"] in link_state and not link_state[r["id"]]]
        orphans = [r for r in rows if r["id"] not in link_state]
        blocked = [r for r in orphans if r["status"] == SlotStatus.BLOCKED]
        excluded = _other_reason_exclusions([r["id"] for r in blocked]) if blocked else set()
        holidays = set()
        if blocked:
            holidays = set(Holiday.get_holidays_in_range(min(r["date"] for r in blocked),
                                                         max(r["date"] for r in blocked)).keys())

        def not_disruption(r):
            return r["status"] == SlotStatus.BLOCKED and (
                r["id"] in excluded or r["date"] in holidays or (r["blocked_label"] or "").startswith("LEGACY_MIGRATION")
            )

        skipped = [r for r in orphans if not_disruption(r)]
        orphans = [r for r in orphans if not not_disruption(r)]
        if not orphans and not deleted_only:
            continue

        equipment_event = (
            DisruptionEvent.objects.filter(
                equipment=equipment, scope=DisruptionScope.EQUIPMENT, ended_at__isnull=True, is_deleted=False,
                disruption_type=DisruptionType.UNDER_MAINTENANCE,
            )
            .order_by("start_at")
            .first()
        )
        to_equipment = [
            r for r in orphans
            if equipment_event is not None
            and r["status"] == SlotStatus.UNDER_MAINTENANCE
            and r["start_datetime"] >= equipment_event.start_at
        ]
        covered = {r["id"] for r in to_equipment}
        by_type = defaultdict(list)
        for r in orphans:
            if r["id"] in covered:
                continue
            by_type[(disruption_type_for_slot_status(r["status"]), r["status"])].append(
                {"id": r["id"], "start": r["start_datetime"], "end": r["end_datetime"]}
            )
        plans = [
            (dtype, status, run)
            for (dtype, status), slot_rows in by_type.items()
            for run in _group_runs(equipment, slot_rows, dtype)
        ]

        line = {
            "orphan": len(orphans),
            "to_equipment_event": len(to_equipment),
            "new_events": len(plans),
            "deleted_event_only": len(deleted_only),
            "not_disruption": len(skipped),
        }
        stats["per_equipment"][equipment.code or f"id{equipment.equipment_id}"] = line
        stats["orphan_slots"] += len(orphans)
        stats["deleted_event_only"] += len(deleted_only)
        stats["not_disruption"] += len(skipped)
        stats["linked_to_equipment_event"] += len(to_equipment)
        if not orphans:
            continue

        with transaction.atomic():
            if to_equipment and apply:
                DisruptionEventSlot.objects.bulk_create(
                    [
                        DisruptionEventSlot(event=equipment_event, daily_slot_id=r["id"],
                                            start_datetime=r["start_datetime"], end_datetime=r["end_datetime"])
                        for r in to_equipment
                    ]
                )
                DisruptionEventEdit.objects.create(
                    event=equipment_event, kind="extended",
                    note=f"{len(to_equipment)} slot(s) linked from earlier data",
                )
            for dtype, status, run in plans:
                stats["new_events"] += 1
                stats["slots_in_new_events"] += len(run)
                stats["events_by_type"][dtype] += 1
                user_id, changed_at = _started_by_from_logs(equipment, run, status)
                if user_id:
                    stats["events_with_started_by"] += 1
                if not apply:
                    continue
                start = min(r["start"] for r in run)
                end = max(r["end"] for r in run)
                user = users.filter(pk=user_id).first() if user_id else None
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
                    started_at=changed_at or min(start, now),
                    started_by=user,
                    started_by_role=staff_role_for(user, equipment) if user else "",
                    slots_affected=len(run),
                    bookings_affected=bookings,
                    backfilled=True,
                )
                DisruptionEventSlot.objects.bulk_create(
                    [
                        DisruptionEventSlot(event=event, daily_slot_id=r["id"], start_datetime=r["start"],
                                            end_datetime=r["end"])
                        for r in run
                    ]
                )
                DisruptionEventEdit.objects.create(event=event, kind="created", note="Recorded from earlier data")
                stats["event_ids"].append(event.pk)
    stats["events_by_type"] = dict(stats["events_by_type"])
    return stats
