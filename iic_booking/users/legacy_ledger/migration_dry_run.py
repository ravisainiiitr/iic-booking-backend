"""Phase 8B dry-run — no writes."""

from __future__ import annotations

from typing import Any, Iterable

from iic_booking.users.legacy_ledger.booking_bridge import discover_legacy_bookings, reconcile_legacy_blocks
from iic_booking.users.legacy_ledger.equipment_mapping import validate_legacy_equipment_mappings
from iic_booking.users.legacy_ledger.legacy_user_resolution import summarize_user_mapping_counts
from iic_booking.users.models import User
from iic_booking.users.models.portal_migration import (
    LegacyBookingBlock,
    LegacyBookingBlockStatus,
    PortalMigrationState,
)


def migration_dry_run(legacy_rows: Iterable[dict] | None = None) -> dict[str, Any]:
    state = PortalMigrationState.get_solo()
    mapping = validate_legacy_equipment_mappings()
    discovery = discover_legacy_bookings(legacy_rows or [])
    recon = reconcile_legacy_blocks()
    flat_rows = []
    for bucket in (
        "eligible",
        "unmapped",
        "conflicting",
        "cancelled",
        "completed",
        "outside_window",
        "invalid",
        "duplicate",
    ):
        flat_rows.extend(discovery.get(bucket) or [])
    user_map_counts = summarize_user_mapping_counts(flat_rows)
    test_users = User.objects.filter(is_test_account=True).count()
    from iic_booking.equipment.models import Booking
    from iic_booking.users.models.portal_migration import (
        MigrationBookingSettlement,
        MigrationSettlementStatus,
    )

    test_bookings = Booking.objects.filter(user__is_test_account=True).count()
    active_blocks = LegacyBookingBlock.objects.filter(status=LegacyBookingBlockStatus.ACTIVE).count()
    settlements_completed = MigrationBookingSettlement.objects.filter(
        status=MigrationSettlementStatus.COMPLETED
    ).count()
    settlements_pending_eligible = MigrationBookingSettlement.objects.filter(
        status=MigrationSettlementStatus.PENDING
    ).count()

    blockers: list[str] = []
    if not state.migration_start_at or not state.migration_window_end_at:
        blockers.append("migration_window_not_configured")
    if not (state.new_portal_url or "").strip():
        blockers.append("new_portal_url_not_configured")
    if mapping["counts"]["conflict"] or mapping["counts"]["invalid"]:
        blockers.append("equipment_mapping_conflicts_or_invalid")
    if discovery["counts"]["unmapped"]:
        blockers.append("unmapped_legacy_bookings_in_discovery_set")
    if discovery["counts"]["conflicting"]:
        blockers.append("conflicting_legacy_bookings_in_discovery_set")
    if not recon.get("ok"):
        blockers.append("active_block_reconciliation_issues")

    ready = len(blockers) == 0
    return {
        "verdict": "READY FOR MIGRATION" if ready else "NOT READY",
        "blockers": blockers,
        "freeze_state": {
            "booking_migration_mode": state.booking_migration_mode,
            "end_user_booking_enabled": state.end_user_booking_enabled,
            "phase": state.phase,
            "migration_start_at": state.migration_start_at.isoformat() if state.migration_start_at else None,
            "migration_window_end_at": state.migration_window_end_at.isoformat()
            if state.migration_window_end_at
            else None,
            "new_portal_url": state.new_portal_url,
        },
        "equipment_mapping": mapping["counts"],
        "discovery": discovery["counts"],
        "user_identity_resolved": user_map_counts.get("resolved", 0),
        "user_identity_unresolved": user_map_counts.get("unresolved", 0),
        "user_mapping_blocks_readiness": False,
        "legacy_booking_count": sum(discovery["counts"].values()),
        "eligible_block_count": discovery["counts"].get("eligible", 0),
        "conflict_count": discovery["counts"].get("conflicting", 0),
        "unmapped_count": discovery["counts"].get("unmapped", 0),
        "active_blocks": active_blocks,
        "reconciliation_ok": recon.get("ok"),
        "test_accounts": test_users,
        "test_bookings": test_bookings,
        "settlement_count": {
            "completed": settlements_completed,
            "pending": settlements_pending_eligible,
        },
        "proposed_t0_actions": [
            "External: block NEW bookings on OLD portal (out of this Django process)",
            "Set booking_migration_mode=ACTIVE",
            "Keep/enable new-portal booking for authorized flows",
            "Arm eligible LegacyBookingBlock rows (DailySlot.BLOCKED)",
            "Queue MigrationNotificationBatch via Celery (do not block T0 on SMTP)",
            "Do not auto-refund; Phase-8A remains explicit OIC/Main Admin action",
        ],
        "schema_note": discovery.get("schema_note"),
    }
