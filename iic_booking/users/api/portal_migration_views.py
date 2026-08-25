"""Portal migration admin and booking-lock APIs. Never accept or return MySQL passwords."""

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.legacy_ledger.booking_lock import booking_status_payload
from iic_booking.users.legacy_ledger.reconcile import run_full_reconciliation
from iic_booking.users.legacy_ledger.schema_gate import (
    portal_bridge_schema_status,
    safe_portal_migration_state,
    schema_pending_payload,
)
from iic_booking.users.legacy_ledger.state_machine import (
    IllegalPhaseTransition,
    PHASE_OPERATOR_HINTS,
    ReconciliationGateFailed,
    transition_phase,
)
from iic_booking.users.models.portal_migration import (
    LegacyWalletAccountMapping,
    LegacyWalletLedgerEntry,
    LegacyWalletMappingStatus,
    LegacyWalletSyncDeadLetter,
    MigrationBookingSettlement,
    MigrationSettlementType,
    PortalMigrationPhase,
    PortalMigrationPhaseTransition,
    PortalMigrationState,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.legacy_ledger.migration_refund import (
    MigrationRefundError,
    actor_can_access_booking_for_settlement,
    can_issue_migration_refund,
    classify_settlement_eligibility,
    get_completed_settlement,
    issue_migration_refund,
    migration_settlement_window_open,
    scoped_bookings_queryset,
    settlement_payload,
)


def _is_migration_admin(user) -> bool:
    return bool(getattr(user, "is_superuser", False) or getattr(user, "user_type", None) == UserType.ADMIN)


def _dashboard_payload() -> dict:
    state, schema = safe_portal_migration_state()
    recon = run_full_reconciliation()
    exception_qs = LegacyWalletAccountMapping.objects.exclude(
        mapping_status__in=[
            LegacyWalletMappingStatus.VALID,
            LegacyWalletMappingStatus.MAPPED,
            LegacyWalletMappingStatus.IMPORTED,
            LegacyWalletMappingStatus.RECONCILED,
            LegacyWalletMappingStatus.PENDING,
        ]
    )
    mismatches = [r for r in recon["rows"] if r["status"] == "FAIL"]
    health = "HEALTHY"
    if state.last_sync_error or recon["overall_status"] == "FAIL":
        health = "FAILED"
    elif exception_qs.exists() or recon["overall_status"] == "EXCEPTION" or LegacyWalletSyncDeadLetter.objects.exists():
        health = "DEGRADED"
    mysql_configured = bool((getattr(settings, "OLD_MYSQL_HOST", "") or "").strip())
    fixture_mysql = bool(getattr(settings, "LEGACY_MYSQL_STAGING_FIXTURE_MODE", False))
    start = getattr(state, "migration_start_at", None)
    end = getattr(state, "migration_window_end_at", None)
    last_sync = getattr(state, "last_sync_at", None)
    return {
        "health": health,
        "environment": getattr(settings, "DEPLOYMENT_ENVIRONMENT", "UNKNOWN"),
        "environment_label": getattr(settings, "ENVIRONMENT_LABEL", ""),
        "staging_banner": "STAGING — NON-PRODUCTION",
        "channel_i_fixture_mode": bool(getattr(settings, "CHANNEL_I_STAGING_FIXTURE_MODE", False)),
        "legacy_mysql_mode": "STAGING_FIXTURE" if fixture_mysql else ("CONFIGURED" if mysql_configured else "NOT_CONFIGURED"),
        "phase": state.phase,
        "end_user_booking_enabled": state.end_user_booking_enabled,
        "incremental_sync_enabled": state.incremental_sync_enabled,
        "legacy_ledger_frozen": state.legacy_ledger_frozen,
        "old_mysql_configured": mysql_configured or fixture_mysql,
        "old_mysql_connection_status": (
            "STAGING_FIXTURE" if fixture_mysql else ("CONFIGURED" if mysql_configured else "NOT_CONFIGURED")
        ),
        "schema": schema,
        "schema_gate": schema.get("gate"),
        "last_successful_sync": last_sync.isoformat() if last_sync else None,
        "last_sync_error": state.last_sync_error,
        "last_sync_batch": state.last_sync_batch,
        "sync_duration_ms": state.last_sync_duration_ms,
        "current_watermark": state.last_wallet_txn_watermark,
        "transactions_imported": LegacyWalletLedgerEntry.objects.count(),
        "transactions_imported_total_counter": state.transactions_imported_total,
        "failed_transactions": LegacyWalletSyncDeadLetter.objects.count(),
        "mapping_exceptions": exception_qs.count(),
        "reconciliation_failures": recon["counts"].get("FAIL", 0),
        "total_old_credits": recon["old_credit_total"],
        "total_imported_credits": recon["imported_credit_total"],
        "total_old_debits": recon["old_debit_total"],
        "total_imported_debits": recon["imported_debit_total"],
        "balance_mismatches": len(mismatches),
        "reconciliation_overall": recon["overall_status"],
        "metrics": {
            "legacy_sync_runs_total": state.sync_runs_total,
            "legacy_sync_failures_total": state.sync_failures_total,
            "legacy_transactions_imported_total": state.transactions_imported_total,
            "legacy_mapping_exceptions_total": exception_qs.count(),
            "legacy_reconciliation_failures_total": recon["counts"].get("FAIL", 0),
            "sync_duration": state.last_sync_duration_ms,
            "current_watermark": state.last_wallet_txn_watermark,
            "last_successful_sync": last_sync.isoformat() if last_sync else None,
        },
        "next_operator_hint": PHASE_OPERATOR_HINTS.get(state.phase, ""),
        "booking_migration_mode": getattr(state, "booking_migration_mode", None) or "NORMAL",
        "migration_start_at": start.isoformat() if start else None,
        "migration_window_end_at": end.isoformat() if end else None,
        "new_portal_url": getattr(state, "new_portal_url", "") or "",
        "recent_transitions": list(
            PortalMigrationPhaseTransition.objects.values("from_phase", "to_phase", "actor_email", "created_at")[:10]
        ),
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_booking_status(request):
    return Response(booking_status_payload(request.user), status=status.HTTP_200_OK)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def portal_migration_admin_state(request):
    if not _is_migration_admin(request.user):
        return Response({"error": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
    schema = portal_bridge_schema_status()
    if request.method == "PATCH":
        data = request.data or {}
        window_keys = {
            "migration_start_at",
            "migration_window_end_at",
            "booking_migration_mode",
            "new_portal_url",
        }
        if window_keys.intersection(data.keys()) and not schema.get("has_migration_start_at"):
            return Response(
                schema_pending_payload(
                    endpoint="admin/state",
                    error="SCHEMA_PENDING",
                    detail=(
                        "Cannot persist migration window fields until users.0102 is applied "
                        "(Migrate Production). Do not invent dates or ALTER manually."
                    ),
                ),
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if "phase" in data:
            return Response(
                {"error": "Use POST /portal-migration/admin/transition/ for explicit phase changes."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not schema.get("has_migration_start_at"):
            # Pre-0102: update only base columns via QuerySet.update (no ORM SELECT of missing cols).
            updates = {}
            if "end_user_booking_enabled" in data:
                updates["end_user_booking_enabled"] = bool(data.get("end_user_booking_enabled"))
            if "legacy_ledger_frozen" in data:
                updates["legacy_ledger_frozen"] = bool(data.get("legacy_ledger_frozen"))
            if "incremental_sync_enabled" in data:
                updates["incremental_sync_enabled"] = bool(data.get("incremental_sync_enabled"))
            if "booking_lock_message" in data:
                updates["booking_lock_message"] = str(data.get("booking_lock_message") or "")
            if "booking_opens_at" in data:
                from django.utils.dateparse import parse_datetime

                raw = data.get("booking_opens_at")
                updates["booking_opens_at"] = parse_datetime(str(raw)) if raw else None
            if updates:
                PortalMigrationState.objects.filter(singleton_key="default").update(**updates)
            return Response(
                {**booking_status_payload(request.user), **_dashboard_payload()},
                status=status.HTTP_200_OK,
            )

        state = PortalMigrationState.get_solo()
        if "end_user_booking_enabled" in data:
            state.end_user_booking_enabled = bool(data.get("end_user_booking_enabled"))
        if "legacy_ledger_frozen" in data:
            state.legacy_ledger_frozen = bool(data.get("legacy_ledger_frozen"))
        if "incremental_sync_enabled" in data:
            state.incremental_sync_enabled = bool(data.get("incremental_sync_enabled"))
        if "booking_lock_message" in data:
            state.booking_lock_message = str(data.get("booking_lock_message") or "")
        if "booking_opens_at" in data:
            from django.utils.dateparse import parse_datetime

            raw = data.get("booking_opens_at")
            state.booking_opens_at = parse_datetime(str(raw)) if raw else None
        if "migration_start_at" in data:
            from django.utils.dateparse import parse_datetime

            raw = data.get("migration_start_at")
            state.migration_start_at = parse_datetime(str(raw)) if raw else None
        if "migration_window_end_at" in data:
            from django.utils.dateparse import parse_datetime

            raw = data.get("migration_window_end_at")
            state.migration_window_end_at = parse_datetime(str(raw)) if raw else None
        if "booking_migration_mode" in data:
            mode = str(data.get("booking_migration_mode") or "NORMAL").upper()
            allowed = {"NORMAL", "PREPARATION", "FREEZE", "ACTIVE", "SETTLEMENT", "COMPLETED"}
            if mode not in allowed:
                return Response(
                    {"error": f"Invalid booking_migration_mode. Allowed: {sorted(allowed)}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            state.booking_migration_mode = mode
        if "new_portal_url" in data:
            state.new_portal_url = str(data.get("new_portal_url") or "")
        state.save()
    return Response({**booking_status_payload(request.user), **_dashboard_payload()}, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_migration_dashboard(request):
    if not _is_migration_admin(request.user):
        return Response({"error": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
    return Response(_dashboard_payload(), status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def portal_migration_transition(request):
    if not _is_migration_admin(request.user):
        return Response({"error": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
    to_phase = str((request.data or {}).get("to_phase") or "")
    note = str((request.data or {}).get("note") or "")
    confirm = bool((request.data or {}).get("confirm"))
    if not confirm:
        return Response(
            {"error": "Set confirm=true. Phase changes never auto-run the next cutover step."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    mismatch_count = None
    if to_phase == PortalMigrationPhase.NEW_PORTAL_ACTIVE:
        mismatch_count = run_full_reconciliation()["counts"].get("FAIL", 0)
    try:
        state = transition_phase(
            to_phase=to_phase,
            actor_email=getattr(request.user, "email", "") or "",
            note=note,
            mismatch_count=mismatch_count,
        )
    except (IllegalPhaseTransition, ReconciliationGateFailed) as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(
        {
            "phase": state.phase,
            "hint": PHASE_OPERATOR_HINTS.get(state.phase, ""),
            "dashboard": _dashboard_payload(),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_migration_mapping_report(request):
    if not _is_migration_admin(request.user):
        return Response({"error": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
    status_filter = (request.GET.get("mapping_status") or "").strip()
    employee_id = (request.GET.get("employee_id") or "").strip()
    qs = LegacyWalletAccountMapping.objects.all().order_by("employee_id")
    if status_filter:
        qs = qs.filter(mapping_status=status_filter)
    if employee_id:
        qs = qs.filter(employee_id=employee_id)
    limit = min(int(request.GET.get("limit", 200)), 1000)
    offset = int(request.GET.get("offset", 0))
    rows = []
    for m in qs[offset : offset + limit]:
        rows.append(
            {
                "old_user_id": m.old_user_id,
                "employee_id": m.employee_id,
                "old_name": m.old_name,
                "old_email": m.old_email,
                "channel_i_employee_id": m.channel_i_employee_id,
                "channel_i_name": m.channel_i_name,
                "new_name": m.channel_i_name,
                "channel_i_email": m.channel_i_email,
                "new_email": m.channel_i_email,
                "new_user_id": m.new_user_id,
                "mapping_status": m.mapping_status,
                "exception_reason": m.exception_reason,
                "old_credits": str(m.old_credits),
                "old_debits": str(m.old_debits),
                "imported_credits": str(m.imported_credits),
                "imported_debits": str(m.imported_debits),
                "reconciliation_status": m.reconciliation_status,
                "recommended_action": (
                    "Do not auto-import. Review Employee ID and Channel-I identity."
                    if m.mapping_status
                    not in {
                        LegacyWalletMappingStatus.VALID,
                        LegacyWalletMappingStatus.MAPPED,
                        LegacyWalletMappingStatus.IMPORTED,
                        LegacyWalletMappingStatus.RECONCILED,
                    }
                    else "No mapping exception."
                ),
            }
        )
    return Response({"count": qs.count(), "results": rows}, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_migration_dead_letters(request):
    if not _is_migration_admin(request.user):
        return Response({"error": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
    qs = LegacyWalletSyncDeadLetter.objects.all().order_by("-source_transaction_id")
    limit = min(int(request.GET.get("limit", 200)), 1000)
    rows = [
        {
            "source_transaction_id": d.source_transaction_id,
            "source_user_id": d.source_user_id,
            "employee_id": d.employee_id,
            "reason": d.reason,
            "detail": d.detail,
            "payload": d.payload,
            "recommended_action": "Fix mapping on the new portal; never write to old MySQL.",
        }
        for d in qs[:limit]
    ]
    return Response({"count": qs.count(), "results": rows}, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_migration_settlement_detail(request, booking_id):
    """Read-only migration settlement status for a booking."""
    if not can_issue_migration_refund(request.user):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.equipment.models import Booking

    try:
        booking = Booking.objects.select_related("user", "equipment").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if not actor_can_access_booking_for_settlement(request.user, booking):
        return Response({"error": "Booking is outside your operational scope."}, status=status.HTTP_403_FORBIDDEN)
    settlement = get_completed_settlement(booking) or (
        MigrationBookingSettlement.objects.filter(
            booking=booking, settlement_type=MigrationSettlementType.MIGRATION_REFUND
        )
        .order_by("-id")
        .first()
    )
    freeze_before = PortalMigrationState.get_solo().end_user_booking_enabled
    payload = settlement_payload(settlement, booking)
    payload["booking_id"] = booking.booking_id
    payload["booking_status"] = booking.status
    payload["end_user_booking_enabled"] = freeze_before
    payload["migration_window_open"] = migration_settlement_window_open()
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_migration_refund(request, booking_id):
    """Issue one-time MIGRATION_REFUND for an eligible booking (OIC / Main Admin only)."""
    from iic_booking.equipment.models import Booking

    try:
        booking = Booking.objects.select_related(
            "user", "equipment", "equipment__internal_department"
        ).get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    confirm = bool(
        request.data.get("confirm") is True
        or str(request.data.get("confirm", "")).lower() in ("1", "true", "yes")
    )
    reason = request.data.get("reason") or request.data.get("notes") or ""
    freeze_before = PortalMigrationState.get_solo().end_user_booking_enabled
    slots_before = set(booking.daily_slots.values_list("id", flat=True))
    booking_status_before = booking.status

    try:
        settlement = issue_migration_refund(
            booking=booking,
            actor=request.user,
            reason=reason,
            confirm=confirm,
        )
    except MigrationRefundError as exc:
        return Response(
            {"error": str(exc), "error_code": exc.code},
            status=exc.http_status,
        )

    booking.refresh_from_db()
    freeze_after = PortalMigrationState.get_solo().end_user_booking_enabled
    slots_after = set(booking.daily_slots.values_list("id", flat=True))
    return Response(
        {
            "message": "Migration refund completed.",
            "settlement": settlement_payload(settlement, booking),
            "safety": {
                "end_user_booking_enabled_unchanged": freeze_before == freeze_after,
                "booking_status_unchanged": booking_status_before == booking.status,
                "slots_not_freed": slots_before == slots_after,
                "new_booking_created": False,
            },
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_migration_settlements_report(request):
    """Department-wide (Main Admin) or OIC-scoped migration settlement report."""
    if not can_issue_migration_refund(request.user):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)

    qs = scoped_bookings_queryset(request.user)
    dept = request.GET.get("department_id")
    equipment_id = request.GET.get("equipment_id")
    booking_status = request.GET.get("booking_status")
    refund_status = (request.GET.get("refund_status") or "").strip().lower()

    if dept:
        qs = qs.filter(equipment__internal_department_id=dept)
    if equipment_id:
        qs = qs.filter(equipment_id=equipment_id)
    if booking_status:
        qs = qs.filter(status=booking_status)

    limit = min(int(request.GET.get("limit", 200)), 1000)
    rows = []
    counts = {
        "pending_migration_settlement": 0,
        "refund_completed": 0,
        "refund_failed": 0,
        "already_settled": 0,
        "non_refundable": 0,
    }
    for booking in qs.order_by("-booking_id")[:limit]:
        bucket = classify_settlement_eligibility(booking)
        if bucket == "already_settled":
            counts["already_settled"] += 1
            counts["refund_completed"] += 1
        elif bucket == "refund_failed":
            counts["refund_failed"] += 1
        elif bucket == "non_refundable":
            counts["non_refundable"] += 1
        else:
            counts["pending_migration_settlement"] += 1
        if refund_status:
            mapping = {
                "pending": "pending_migration_settlement",
                "completed": "already_settled",
                "failed": "refund_failed",
                "already_settled": "already_settled",
                "non_refundable": "non_refundable",
            }
            want = mapping.get(refund_status, refund_status)
            if bucket != want:
                continue
        settlement = get_completed_settlement(booking)
        rows.append(
            {
                "booking_id": booking.booking_id,
                "booking_status": booking.status,
                "equipment_id": booking.equipment_id,
                "equipment_code": getattr(booking.equipment, "code", ""),
                "department_id": getattr(booking.equipment, "internal_department_id", None),
                "user_id": booking.user_id,
                "eligibility": bucket,
                "settlement": settlement_payload(settlement, booking),
            }
        )

    return Response(
        {
            "migration_window_open": migration_settlement_window_open(),
            "counts": counts,
            "count": len(rows),
            "results": rows,
            "scope": (
                "all_departments"
                if (getattr(request.user, "is_superuser", False) or request.user.user_type == UserType.ADMIN)
                else "oic_equipment"
            ),
        },
        status=status.HTTP_200_OK,
    )
