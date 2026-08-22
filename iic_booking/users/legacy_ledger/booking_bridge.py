"""Phase 8B — hybrid LegacyBookingBlock + DailySlot.BLOCKED slot protection."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from django.db import IntegrityError, transaction
from django.utils import timezone

from iic_booking.equipment.models import DailySlot, Equipment, SlotStatus
from iic_booking.users.legacy_ledger.equipment_mapping import get_active_mapping_for_old_id
from iic_booking.users.models.portal_migration import (
    LegacyBookingBlock,
    LegacyBookingBlockStatus,
    LegacyBookingMigrationBatch,
    LegacyBookingMigrationBatchStatus,
    PortalMigrationState,
)

LEGACY_MIGRATION_SLOT_BLOCKED = "LEGACY_MIGRATION_SLOT_BLOCKED"
LEGACY_BLOCK_MESSAGE = (
    "This time slot is temporarily unavailable because an existing booking "
    "from the previous booking portal is being carried forward during migration."
)


def migration_window(state: PortalMigrationState | None = None) -> tuple[datetime | None, datetime | None]:
    state = state or PortalMigrationState.get_solo()
    return state.migration_start_at, state.migration_window_end_at


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize fixture/legacy discovery rows. Does not invent MySQL column names."""
    legacy_id = row.get("legacy_booking_id") or row.get("source_booking_id") or row.get("id")
    start = row.get("start_at") or row.get("start") or row.get("start_datetime")
    end = row.get("end_at") or row.get("end") or row.get("end_datetime")
    old_eq = row.get("old_equipment_id") or row.get("equipment_id") or row.get("old_equipment")
    status = str(row.get("status") or row.get("booking_status") or "").upper()
    amount = row.get("amount") or row.get("booking_amount") or row.get("total_charge") or 0
    return {
        "legacy_booking_id": int(legacy_id) if legacy_id is not None else None,
        "start_at": start,
        "end_at": end,
        "old_equipment_id": int(old_eq) if old_eq is not None else None,
        "status": status,
        "amount": Decimal(str(amount or 0)),
        "user_key": str(row.get("employee_id") or row.get("emp_id") or row.get("user_id") or ""),
        "raw": row,
    }


def classify_discovery_row(norm: dict[str, Any], window_start, window_end) -> str:
    if norm["legacy_booking_id"] is None or not norm["start_at"] or not norm["end_at"]:
        return "invalid"
    st = norm["status"]
    if st in {"CANCELLED", "CANCELED"}:
        return "cancelled"
    if st in {"COMPLETED", "DONE"}:
        return "completed"
    # window: start_at in [window_start, window_end)
    start = norm["start_at"]
    if timezone.is_naive(start):
        start = timezone.make_aware(start, timezone.get_current_timezone())
    if window_start and start < window_start:
        return "invalid"
    if window_end and start >= window_end:
        return "invalid"
    if not norm["old_equipment_id"]:
        return "unmapped"
    mapping = get_active_mapping_for_old_id(norm["old_equipment_id"])
    if not mapping:
        return "unmapped"
    return "eligible"


def discover_legacy_bookings(
    rows: Iterable[dict[str, Any]],
    *,
    window_start=None,
    window_end=None,
) -> dict[str, Any]:
    """Read-only discovery against provided rows (fixtures). No DB writes."""
    state = PortalMigrationState.get_solo()
    window_start = window_start or state.migration_start_at
    window_end = window_end or state.migration_window_end_at
    buckets = {
        "eligible": [],
        "unmapped": [],
        "conflicting": [],
        "cancelled": [],
        "completed": [],
        "invalid": [],
    }
    for raw in rows:
        norm = normalize_row(raw)
        bucket = classify_discovery_row(norm, window_start, window_end)
        mapping = (
            get_active_mapping_for_old_id(norm["old_equipment_id"])
            if norm.get("old_equipment_id")
            else None
        )
        entry = {
            "legacy_booking_id": norm["legacy_booking_id"],
            "old_equipment_id": norm["old_equipment_id"],
            "new_equipment_id": getattr(mapping.new_equipment, "equipment_id", None) if mapping else None,
            "start_at": norm["start_at"].isoformat() if hasattr(norm["start_at"], "isoformat") else norm["start_at"],
            "end_at": norm["end_at"].isoformat() if hasattr(norm["end_at"], "isoformat") else norm["end_at"],
            "status": norm["status"],
            "amount": str(norm["amount"]),
            "mapping_status": mapping.status if mapping else "UNMAPPED",
            "eligibility": bucket,
            # no PII beyond opaque user_key presence
            "user_key_present": bool(norm["user_key"]),
        }
        if bucket == "eligible" and mapping:
            # soft conflict preview: overlapping ACTIVE blocks on same equipment
            start = norm["start_at"]
            end = norm["end_at"]
            if timezone.is_naive(start):
                start = timezone.make_aware(start, timezone.get_current_timezone())
            if timezone.is_naive(end):
                end = timezone.make_aware(end, timezone.get_current_timezone())
            overlaps = LegacyBookingBlock.objects.filter(
                new_equipment=mapping.new_equipment,
                status=LegacyBookingBlockStatus.ACTIVE,
                start_at__lt=end,
                end_at__gt=start,
            ).exclude(legacy_booking_id=norm["legacy_booking_id"])
            if overlaps.exists():
                entry["eligibility"] = "conflicting"
                buckets["conflicting"].append(entry)
                continue
        buckets[bucket if bucket in buckets else "invalid"].append(entry)
    return {
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "counts": {k: len(v) for k, v in buckets.items()},
        **buckets,
        "schema_note": (
            "Live MySQL booking column map is NOT hard-coded. "
            "Pass normalized fixture rows or a verified column map before production discovery."
        ),
    }


def find_overlapping_slots(equipment: Equipment, start_at, end_at):
    if timezone.is_naive(start_at):
        start_at = timezone.make_aware(start_at, timezone.get_current_timezone())
    if timezone.is_naive(end_at):
        end_at = timezone.make_aware(end_at, timezone.get_current_timezone())
    return list(
        DailySlot.objects.select_related("slot_master")
        .filter(slot_master__equipment=equipment)
        .filter(start_datetime__lt=end_at, end_datetime__gt=start_at)
        .order_by("start_datetime")
    )


def arm_legacy_block(
    *,
    legacy_booking_id: int,
    equipment: Equipment,
    start_at,
    end_at,
    batch: LegacyBookingMigrationBatch | None = None,
    payload: dict | None = None,
) -> LegacyBookingBlock:
    """Create ACTIVE block and mark overlapping AVAILABLE slots BLOCKED (hybrid protection)."""
    if LegacyBookingBlock.objects.filter(
        legacy_booking_id=legacy_booking_id,
        source="LEGACY_PORTAL",
        status=LegacyBookingBlockStatus.ACTIVE,
    ).exists():
        raise ValueError("duplicate_active_block")

    slots = find_overlapping_slots(equipment, start_at, end_at)
    # Conflict if any already BOOKED
    if any(s.status == SlotStatus.BOOKED for s in slots):
        block = LegacyBookingBlock.objects.create(
            legacy_booking_id=legacy_booking_id,
            new_equipment=equipment,
            start_at=start_at,
            end_at=end_at,
            status=LegacyBookingBlockStatus.CONFLICT,
            migration_batch=batch,
            slot_ids=[],
            legacy_payload=payload or {},
        )
        return block

    try:
        with transaction.atomic():
            block = LegacyBookingBlock.objects.create(
                legacy_booking_id=legacy_booking_id,
                new_equipment=equipment,
                start_at=start_at,
                end_at=end_at,
                status=LegacyBookingBlockStatus.ACTIVE,
                migration_batch=batch,
                legacy_payload=payload or {},
            )
            claimed: list[int] = []
            label = block.blocked_label
            for slot in DailySlot.objects.select_for_update().filter(
                id__in=[s.id for s in slots],
                status=SlotStatus.AVAILABLE,
            ):
                slot.status = SlotStatus.BLOCKED
                slot.blocked_label = label
                slot.save(update_fields=["status", "blocked_label"])
                claimed.append(slot.id)
            block.slot_ids = claimed
            block.save(update_fields=["slot_ids"])
            if batch and batch.status in (
                LegacyBookingMigrationBatchStatus.DRAFT,
                LegacyBookingMigrationBatchStatus.VALIDATED,
                LegacyBookingMigrationBatchStatus.ARMED,
            ):
                batch.status = LegacyBookingMigrationBatchStatus.ACTIVE
                batch.save(update_fields=["status"])
    except IntegrityError as exc:
        raise ValueError("duplicate_active_block") from exc
    return block


def release_legacy_block(block: LegacyBookingBlock, *, reason: str = "released") -> LegacyBookingBlock:
    with transaction.atomic():
        label = block.blocked_label
        for slot in DailySlot.objects.select_for_update().filter(id__in=block.slot_ids or []):
            if slot.status == SlotStatus.BLOCKED and (slot.blocked_label or "") == label:
                slot.status = SlotStatus.AVAILABLE
                slot.blocked_label = None
                slot.save(update_fields=["status", "blocked_label"])
        block.status = LegacyBookingBlockStatus.RELEASED
        block.released_at = timezone.now()
        block.released_reason = reason[:255]
        block.save(update_fields=["status", "released_at", "released_reason"])
    return block


def abort_migration_batch(batch: LegacyBookingMigrationBatch, *, reason: str = "aborted") -> dict[str, Any]:
    """Release ACTIVE blocks for batch; preserve audit rows. Does not reverse Phase-8A refunds."""
    released = 0
    for block in LegacyBookingBlock.objects.filter(
        migration_batch=batch, status=LegacyBookingBlockStatus.ACTIVE
    ):
        release_legacy_block(block, reason=reason)
        released += 1
    batch.status = LegacyBookingMigrationBatchStatus.ABORTED
    batch.completed_at = timezone.now()
    counts = dict(batch.counts or {})
    counts["released_on_abort"] = released
    batch.counts = counts
    batch.save(update_fields=["status", "completed_at", "counts"])
    return {"batch_id": batch.id, "status": batch.status, "released": released}


def slots_blocked_by_legacy_migration(slot_ids: list[int]) -> list[DailySlot]:
    prefix = LegacyBookingBlock.BLOCKED_LABEL_PREFIX
    return list(
        DailySlot.objects.filter(
            id__in=slot_ids,
            status=SlotStatus.BLOCKED,
            blocked_label__startswith=prefix,
        )
    )


def reconcile_legacy_blocks(*, window_start=None, window_end=None) -> dict[str, Any]:
    state = PortalMigrationState.get_solo()
    window_start = window_start or state.migration_start_at
    window_end = window_end or state.migration_window_end_at
    active = LegacyBookingBlock.objects.filter(status=LegacyBookingBlockStatus.ACTIVE)
    missing_slots = []
    unexpected = []
    duplicates = []
    outside = []
    for b in active:
        if window_start and b.start_at < window_start:
            outside.append(b.legacy_booking_id)
        if window_end and b.start_at >= window_end:
            outside.append(b.legacy_booking_id)
        for sid in b.slot_ids or []:
            try:
                slot = DailySlot.objects.get(pk=sid)
            except DailySlot.DoesNotExist:
                missing_slots.append({"legacy_booking_id": b.legacy_booking_id, "slot_id": sid})
                continue
            if slot.status != SlotStatus.BLOCKED or (slot.blocked_label or "") != b.blocked_label:
                unexpected.append({"legacy_booking_id": b.legacy_booking_id, "slot_id": sid})
        # duplicate active same legacy id already constrained
    return {
        "active_blocks": active.count(),
        "missing_slot_links": missing_slots,
        "unexpected_slot_state": unexpected,
        "outside_window": outside,
        "duplicates": duplicates,
        "ok": not missing_slots and not unexpected,
    }
