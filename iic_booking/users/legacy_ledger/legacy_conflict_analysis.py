"""Phase 10E — READ-ONLY migration conflict analysis (no block creation)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.utils import timezone

from iic_booking.equipment.models import Booking, DailySlot, SlotStatus
from iic_booking.users.legacy_ledger.booking_bridge import find_overlapping_slots
from iic_booking.users.legacy_ledger.equipment_mapping import get_active_mapping_for_old_id


CONFLICT_LEGACY_VS_NEW = "LEGACY_VS_NEW_CONFLICT"
CONFLICT_LEGACY_VS_LEGACY = "LEGACY_VS_LEGACY_CONFLICT"
CONFLICT_SLOT_BOOKED = "EXISTING_NEW_BOOKING"
CONFLICT_SLOT_UNAVAILABLE = "TARGET_SLOT_MISSING"
EXISTING_NEW_BOOKING = "EXISTING_NEW_BOOKING"
LEGACY_BOOKING = "LEGACY_BOOKING"


def _parse_dt(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    else:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def analyze_booking_conflicts(eligible_rows: list[dict]) -> dict[str, Any]:
    """
    Read-only conflict calculation for eligible legacy bookings.
    Does not create LegacyBookingBlock or modify DailySlot.
    """
    conflicts: list[dict] = []
    by_type: dict[str, int] = {
        CONFLICT_LEGACY_VS_NEW: 0,
        CONFLICT_LEGACY_VS_LEGACY: 0,
        CONFLICT_SLOT_BOOKED: 0,
        CONFLICT_SLOT_UNAVAILABLE: 0,
    }
    seen_intervals: list[tuple[int, datetime, datetime, int]] = []

    for row in eligible_rows:
        legacy_id = row.get("legacy_booking_id")
        old_eq = row.get("old_equipment_id")
        start = _parse_dt(row.get("start_at") or row.get("legacy_booking_start"))
        end = _parse_dt(row.get("end_at") or row.get("legacy_booking_end"))
        if start is None or end is None:
            continue

        mapping = get_active_mapping_for_old_id(old_eq) if old_eq else None
        new_eq = mapping.new_equipment if mapping else None
        new_eq_id = getattr(new_eq, "equipment_id", None) if new_eq else row.get("new_equipment_id")

        # Legacy vs legacy overlap on same new equipment
        for other_id, o_start, o_end, o_new_eq in seen_intervals:
            if o_new_eq and new_eq_id and int(o_new_eq) == int(new_eq_id):
                if start < o_end and end > o_start:
                    by_type[CONFLICT_LEGACY_VS_LEGACY] += 1
                    conflicts.append(
                        {
                            "legacy_booking_id": legacy_id,
                            "conflict_type": CONFLICT_LEGACY_VS_LEGACY,
                            "other_legacy_booking_id": other_id,
                            "new_equipment_id": new_eq_id,
                            "start_at": start.isoformat(),
                            "end_at": end.isoformat(),
                        }
                    )

        if new_eq_id:
            seen_intervals.append((legacy_id, start, end, int(new_eq_id)))

        if not new_eq:
            continue

        slots = find_overlapping_slots(new_eq, start, end)
        booked_slots = [s for s in slots if s.status == SlotStatus.BOOKED]
        if not slots:
            by_type[CONFLICT_SLOT_UNAVAILABLE] += 1
            conflicts.append(
                {
                    "legacy_booking_id": legacy_id,
                    "conflict_type": CONFLICT_SLOT_UNAVAILABLE,
                    "new_equipment_id": new_eq_id,
                    "start_at": start.isoformat(),
                    "end_at": end.isoformat(),
                }
            )
        elif booked_slots:
            by_type[CONFLICT_SLOT_BOOKED] += 1
            conflicts.append(
                {
                    "legacy_booking_id": legacy_id,
                    "conflict_type": CONFLICT_SLOT_BOOKED,
                    "classification": EXISTING_NEW_BOOKING,
                    "new_equipment_id": new_eq_id,
                    "slot_ids": [s.id for s in booked_slots],
                    "start_at": start.isoformat(),
                    "end_at": end.isoformat(),
                }
            )

        # New portal Booking overlaps (schedule lives on related daily_slots).
        booking_qs = (
            Booking.objects.filter(equipment=new_eq)
            .filter(
                daily_slots__start_datetime__lt=end,
                daily_slots__end_datetime__gt=start,
            )
            .exclude(status__in=["CANCELLED", "REFUNDED"])
            .distinct()
        )
        if booking_qs.exists():
            by_type[CONFLICT_LEGACY_VS_NEW] += 1
            for b in booking_qs[:5]:
                conflicts.append(
                    {
                        "legacy_booking_id": legacy_id,
                        "conflict_type": CONFLICT_LEGACY_VS_NEW,
                        "classification": EXISTING_NEW_BOOKING,
                        "new_booking_id": b.pk,
                        "virtual_booking_id": getattr(b, "virtual_booking_id", None),
                        "new_equipment_id": new_eq_id,
                        "start_at": start.isoformat(),
                        "end_at": end.isoformat(),
                    }
                )

    return {
        "conflict_count": len(conflicts),
        "by_type": by_type,
        "conflicts": conflicts,
        "existing_new_booking_conflicts": by_type[CONFLICT_LEGACY_VS_NEW] + by_type[CONFLICT_SLOT_BOOKED],
        "legacy_vs_legacy_conflicts": by_type[CONFLICT_LEGACY_VS_LEGACY],
        "note": "Read-only analysis. No blocks created. No automatic deletion of new-portal bookings.",
    }


def enrich_row_conflict_status(row: dict, conflict_report: dict[str, Any]) -> dict:
    """Attach conflict_status to a discovery row."""
    lid = row.get("legacy_booking_id")
    row_conflicts = [c for c in conflict_report.get("conflicts") or [] if c.get("legacy_booking_id") == lid]
    if row_conflicts:
        row = dict(row)
        row["conflict_status"] = row_conflicts[0].get("conflict_type")
        row["conflict"] = True
        return row
    row = dict(row)
    row["conflict_status"] = "NONE"
    return row
