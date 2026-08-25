"""Phase 8C — staging T0 orchestration (never production)."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.users.legacy_ledger.booking_bridge import (
    arm_legacy_block,
    discover_legacy_bookings,
)
from iic_booking.users.legacy_ledger.equipment_mapping import (
    get_active_mapping_for_old_id,
    validate_legacy_equipment_mappings,
)
from iic_booking.users.legacy_ledger.migration_dry_run import migration_dry_run
from iic_booking.users.legacy_ledger.migration_notifications import (
    create_notification_batch,
    queue_notification_batch,
)
from iic_booking.users.models import User
from iic_booking.users.models.portal_migration import (
    LegacyBookingBlockStatus,
    LegacyBookingMigrationBatch,
    LegacyBookingMigrationBatchStatus,
    MigrationT0Event,
    PortalMigrationState,
)


def _is_production() -> bool:
    return str(getattr(settings, "DEPLOYMENT_ENVIRONMENT", "") or "").upper() in {"PRODUCTION", "PROD"}


def run_staging_t0(
    *,
    legacy_rows: list[dict],
    confirm_staging_t0: bool,
    created_by=None,
    queue_emails: bool = True,
    email_dry_run: bool = False,
) -> dict[str, Any]:
    """
    Transactionally controlled staging T0:
      validate → activate mode → arm blocks → queue notifications → record T0
    """
    if _is_production():
        raise RuntimeError("STAGING ONLY — refusing T0 in PRODUCTION.")
    if not confirm_staging_t0:
        raise RuntimeError("Pass confirm_staging_t0=True after dry-run READY.")

    from iic_booking.users.legacy_ledger.datetime_contract import (
        contract_blocks_t0,
        load_datetime_contract,
        validate_contract_for_discovery,
    )
    from django.db import connection

    contract = load_datetime_contract()
    if contract_blocks_t0(contract):
        return {"ok": False, "stage": "datetime_contract", "error": "datetime_contract_not_approved"}
    gate = validate_contract_for_discovery(contract)
    if not gate.get("ready_for_discovery"):
        return {"ok": False, "stage": "datetime_contract", "gate": gate}

    tables = set(connection.introspection.table_names())
    if "users_legacybookingblock" not in tables:
        return {"ok": False, "stage": "migrations", "error": "users.0101–0104 schema not applied"}

    dry = migration_dry_run(legacy_rows)
    if dry.get("verdict") != "READY FOR MIGRATION":
        return {"ok": False, "stage": "validate", "dry_run": dry}

    mapping = validate_legacy_equipment_mappings()
    if not mapping.get("ready"):
        return {"ok": False, "stage": "mappings", "mapping": mapping["counts"]}

    state = PortalMigrationState.get_solo()
    if not state.migration_start_at or not state.migration_window_end_at:
        return {"ok": False, "stage": "window", "error": "migration window not configured"}
    if not (state.new_portal_url or "").strip():
        return {"ok": False, "stage": "new_portal_url", "error": "new_portal_url required"}

    steps: dict[str, Any] = {"dry_run": dry["verdict"], "mapping_ready": True}

    with transaction.atomic():
        batch = LegacyBookingMigrationBatch.objects.create(
            window_start=state.migration_start_at,
            window_end=state.migration_window_end_at,
            status=LegacyBookingMigrationBatchStatus.ARMED,
            created_by=created_by,
            counts={},
        )
        discovery = discover_legacy_bookings(legacy_rows)
        armed = 0
        conflicts = 0
        for entry in discovery.get("eligible") or []:
            mapping_obj = get_active_mapping_for_old_id(entry["old_equipment_id"])
            if not mapping_obj or not mapping_obj.new_equipment_id:
                conflicts += 1
                continue
            start = parse_datetime(str(entry["start_at"])) if isinstance(entry["start_at"], str) else entry["start_at"]
            end = parse_datetime(str(entry["end_at"])) if isinstance(entry["end_at"], str) else entry["end_at"]
            block = arm_legacy_block(
                legacy_booking_id=int(entry["legacy_booking_id"]),
                equipment=mapping_obj.new_equipment,
                start_at=start,
                end_at=end,
                batch=batch,
                payload={"staging": True},
            )
            if block.status == LegacyBookingBlockStatus.ACTIVE:
                armed += 1
            else:
                conflicts += 1

        # Activate migration mode AFTER blocks established
        state.booking_migration_mode = "ACTIVE"
        # New portal remains bookable; end_user lock is independent.
        state.end_user_booking_enabled = True
        state.save(update_fields=["booking_migration_mode", "end_user_booking_enabled", "updated_at"])

        batch.status = LegacyBookingMigrationBatchStatus.ACTIVE
        batch.counts = {
            "discovered": discovery["counts"],
            "blocked": armed,
            "conflicts": conflicts,
            "eligible": discovery["counts"].get("eligible", 0),
            "unmapped": discovery["counts"].get("unmapped", 0),
            "cancelled": discovery["counts"].get("cancelled", 0),
            "completed": discovery["counts"].get("completed", 0),
        }
        batch.save(update_fields=["status", "counts"])

        notif_batch, notif_report = create_notification_batch(
            migration_batch=batch,
            dry_run=email_dry_run,
            created_by=created_by,
            users=User.objects.filter(is_active=True).exclude(email=""),
        )
        queue_result = {"queued": 0}
        if queue_emails and not email_dry_run:
            queue_result = queue_notification_batch(notif_batch)
        elif email_dry_run:
            queue_result = {"queued": 0, "reason": "email_dry_run"}

        t0 = MigrationT0Event.objects.create(
            environment="STAGING",
            t0_at=timezone.now(),
            booking_migration_mode=state.booking_migration_mode,
            migration_batch=batch,
            notification_batch=notif_batch,
            created_by=created_by,
            steps={
                **steps,
                "blocks_armed": armed,
                "block_conflicts": conflicts,
                "notification_counts": notif_batch.counts,
                "queue_result": queue_result,
                "old_portal_freeze_signal": "MIGRATION_BOOKING_DISABLED",
                "new_portal_booking_enabled": True,
            },
            notes="Phase 8C staging T0 simulation",
        )

    return {
        "ok": True,
        "t0_event_id": t0.id,
        "t0_at": t0.t0_at.isoformat() if t0.t0_at else None,
        "migration_batch_id": batch.id,
        "notification_batch_id": notif_batch.id,
        "blocks_armed": armed,
        "queue_result": queue_result,
        "notification_report": {
            "total_recipients": notif_report["total_recipients"],
            "faculty": notif_report["faculty"],
            "students": notif_report["students"],
            "oic": notif_report["oic"],
            "admin": notif_report["admin"],
            "skipped": notif_report["skipped"],
        },
        "booking_migration_mode": state.booking_migration_mode,
    }