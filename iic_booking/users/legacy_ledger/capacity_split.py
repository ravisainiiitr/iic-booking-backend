"""Capacity-split resolution: 1 legacy equipment → 2 new machines (TG/DTA time-band fold)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from iic_booking.users.models.portal_migration import (
    LegacyEquipmentCapacitySplit,
    LegacyEquipmentCapacitySplitPolicy,
    LegacyEquipmentCapacitySplitStatus,
    LegacyEquipmentMapping,
    LegacyEquipmentMappingStatus,
)

# Minutes from local midnight → (band A|B, new start minutes from midnight).
# TG/DTA scheme (operator-approved):
#   overnight extended capacity → B daytime grid
#   daytime slots → A same wall-clock
TIME_BAND_FOLD_MAP: dict[int, tuple[str, int]] = {
    0: ("B", 9 * 60),  # 00:00 → B 09:00
    2 * 60 + 15: ("B", 11 * 60 + 15),  # 02:15 → B 11:15
    4 * 60 + 30: ("B", 13 * 60 + 30),  # 04:30 → B 13:30
    6 * 60 + 45: ("B", 15 * 60 + 45),  # 06:45 → B 15:45
    9 * 60: ("A", 9 * 60),  # 09:00 → A 09:00
    11 * 60 + 15: ("A", 11 * 60 + 15),  # 11:15 → A 11:15
    13 * 60 + 30: ("A", 13 * 60 + 30),  # 13:30 → A 13:30
    15 * 60 + 45: ("A", 15 * 60 + 45),  # 15:45 → A 15:45
}

POLICY_SCHEME_LABEL = {
    LegacyEquipmentCapacitySplitPolicy.TIME_BAND_FOLD: (
        "Overnight 00:00/02:15/04:30/06:45 → B at 09:00/11:15/13:30/15:45; "
        "daytime 09:00–15:45 → A same wall-clock"
    ),
}


def _aware(dt: datetime) -> datetime:
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def local_minutes_of_day(dt: datetime) -> int:
    local = timezone.localtime(_aware(dt))
    return local.hour * 60 + local.minute


def format_hhmm(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h:02d}:{m:02d}"


def get_active_capacity_split(old_equipment_id: int) -> LegacyEquipmentCapacitySplit | None:
    return (
        LegacyEquipmentCapacitySplit.objects.select_related("target_a", "target_b")
        .filter(
            old_equipment_id=int(old_equipment_id),
            status=LegacyEquipmentCapacitySplitStatus.ACTIVE,
        )
        .first()
    )


def legacy_equipment_is_mapped(old_equipment_id: int | None) -> bool:
    if old_equipment_id is None:
        return False
    if get_active_capacity_split(int(old_equipment_id)):
        return True
    from iic_booking.users.legacy_ledger.equipment_mapping import get_active_mapping_for_old_id

    return get_active_mapping_for_old_id(int(old_equipment_id)) is not None


def apply_time_band_fold(
    split: LegacyEquipmentCapacitySplit,
    *,
    start_at: datetime,
    end_at: datetime | None = None,
) -> dict[str, Any]:
    """Remap one legacy booking onto target_a or target_b per TIME_BAND_FOLD."""
    start = _aware(start_at)
    if end_at is None:
        end = start + timedelta(minutes=135)
    else:
        end = _aware(end_at)
    duration = end - start
    if duration.total_seconds() <= 0:
        duration = timedelta(minutes=135)

    old_mins = local_minutes_of_day(start)
    mapping = TIME_BAND_FOLD_MAP.get(old_mins)
    local = timezone.localtime(start)

    if mapping is None:
        return {
            "ok": False,
            "needs_review": True,
            "reason": f"unmapped_legacy_slot_start_{format_hhmm(old_mins)}",
            "old_equipment_id": split.old_equipment_id,
            "legacy_start_at": start.isoformat(),
            "legacy_end_at": end.isoformat(),
            "legacy_start_local": format_hhmm(old_mins),
            "policy": split.policy,
            "split_id": split.id,
            "new_equipment_id": None,
            "new_equipment_code": "",
            "band": None,
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
        }

    band, new_mins = mapping
    target = split.target_a if band == "A" else split.target_b
    new_local_naive = datetime(
        local.year,
        local.month,
        local.day,
        new_mins // 60,
        new_mins % 60,
        0,
        0,
    )
    tz = timezone.get_current_timezone()
    new_start = timezone.make_aware(new_local_naive, tz)
    new_end = new_start + duration

    return {
        "ok": True,
        "needs_review": False,
        "reason": "",
        "old_equipment_id": split.old_equipment_id,
        "legacy_start_at": start.isoformat(),
        "legacy_end_at": end.isoformat(),
        "legacy_start_local": format_hhmm(old_mins),
        "policy": split.policy,
        "split_id": split.id,
        "band": band,
        "new_equipment_id": target.equipment_id,
        "new_equipment_code": target.code or "",
        "new_equipment_name": target.name or "",
        "start_at": new_start.isoformat(),
        "end_at": new_end.isoformat(),
        "new_start_local": format_hhmm(new_mins),
        "remapped": band == "B" or new_mins != old_mins,
    }


def resolve_legacy_booking_target(
    *,
    old_equipment_id: int | None,
    start_at: datetime | None,
    end_at: datetime | None = None,
) -> dict[str, Any] | None:
    """
    Resolve new equipment (+ remapped times) for a legacy booking.
    Prefer ACTIVE capacity split; else ACTIVE 1:1 mapping (times unchanged).
    """
    if old_equipment_id is None or start_at is None:
        return None

    split = get_active_capacity_split(int(old_equipment_id))
    if split is not None:
        if split.policy == LegacyEquipmentCapacitySplitPolicy.TIME_BAND_FOLD:
            return apply_time_band_fold(split, start_at=start_at, end_at=end_at)
        return {
            "ok": False,
            "needs_review": True,
            "reason": f"unsupported_policy_{split.policy}",
            "old_equipment_id": int(old_equipment_id),
            "split_id": split.id,
            "new_equipment_id": None,
            "start_at": _aware(start_at).isoformat(),
            "end_at": _aware(end_at).isoformat() if end_at else None,
        }

    from iic_booking.users.legacy_ledger.equipment_mapping import get_active_mapping_for_old_id

    mapping = get_active_mapping_for_old_id(int(old_equipment_id))
    if not mapping or not mapping.new_equipment_id:
        return None
    eq = mapping.new_equipment
    start = _aware(start_at)
    end = _aware(end_at) if end_at else start + timedelta(minutes=135)
    return {
        "ok": True,
        "needs_review": False,
        "reason": "",
        "old_equipment_id": int(old_equipment_id),
        "legacy_start_at": start.isoformat(),
        "legacy_end_at": end.isoformat(),
        "policy": "ONE_TO_ONE",
        "split_id": None,
        "band": None,
        "new_equipment_id": eq.equipment_id,
        "new_equipment_code": eq.code or "",
        "new_equipment_name": eq.name or "",
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "remapped": False,
        "mapping_id": mapping.id,
    }


def capacity_split_row(split: LegacyEquipmentCapacitySplit) -> dict[str, Any]:
    return {
        "id": split.id,
        "old_equipment_id": split.old_equipment_id,
        "old_equipment_code": split.old_equipment_code,
        "old_equipment_name": split.old_equipment_name,
        "target_a_id": split.target_a_id,
        "target_a_code": getattr(split.target_a, "code", "") or "",
        "target_a_name": getattr(split.target_a, "name", "") or "",
        "target_b_id": split.target_b_id,
        "target_b_code": getattr(split.target_b, "code", "") or "",
        "target_b_name": getattr(split.target_b, "name", "") or "",
        "policy": split.policy,
        "policy_label": POLICY_SCHEME_LABEL.get(split.policy, split.policy),
        "status": split.status,
        "notes": split.notes,
        "slot_scheme": [
            {
                "old_start": format_hhmm(old_m),
                "band": band,
                "new_start": format_hhmm(new_m),
            }
            for old_m, (band, new_m) in sorted(TIME_BAND_FOLD_MAP.items())
        ],
        "updated_at": split.updated_at.isoformat() if split.updated_at else None,
    }


def supersede_one_to_one_mapping(old_equipment_id: int, *, actor=None) -> int:
    """Disable ACTIVE 1:1 mappings when a capacity split becomes ACTIVE."""
    qs = LegacyEquipmentMapping.objects.filter(
        old_equipment_id=int(old_equipment_id),
        status=LegacyEquipmentMappingStatus.ACTIVE,
    )
    n = 0
    for m in qs:
        m.status = LegacyEquipmentMappingStatus.DISABLED
        m.mapping_reason = (
            (m.mapping_reason + "\n" if m.mapping_reason else "")
            + "Superseded by ACTIVE capacity split (1→2)."
        ).strip()
        m.updated_by = actor
        m.save(update_fields=["status", "mapping_reason", "updated_by", "updated_at"])
        n += 1
    return n


def preview_capacity_split_assignments(
    split: LegacyEquipmentCapacitySplit,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply split to discovery-like rows (read-only preview)."""
    from iic_booking.users.legacy_ledger.booking_bridge import normalize_row

    assigned: list[dict] = []
    needs_review: list[dict] = []
    skipped = 0
    for raw in rows:
        norm = normalize_row(raw)
        if norm.get("old_equipment_id") != split.old_equipment_id:
            skipped += 1
            continue
        if not norm.get("start_at"):
            needs_review.append({**norm, "reason": "missing_start_at"})
            continue
        result = apply_time_band_fold(
            split,
            start_at=norm["start_at"],
            end_at=norm.get("end_at"),
        )
        entry = {
            "legacy_booking_id": norm.get("legacy_booking_id"),
            **result,
        }
        if result.get("ok"):
            assigned.append(entry)
        else:
            needs_review.append(entry)

    by_band = {"A": 0, "B": 0}
    for a in assigned:
        b = a.get("band")
        if b in by_band:
            by_band[b] += 1

    return {
        "split": capacity_split_row(split),
        "counts": {
            "assigned": len(assigned),
            "needs_review": len(needs_review),
            "skipped_other_equipment": skipped,
            "band_a": by_band["A"],
            "band_b": by_band["B"],
        },
        "assigned": assigned[:500],
        "needs_review": needs_review[:200],
    }
