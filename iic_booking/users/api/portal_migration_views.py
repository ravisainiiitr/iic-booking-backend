"""Portal migration admin and booking-lock APIs. Never accept or return MySQL passwords."""

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.legacy_ledger.booking_lock import booking_status_payload
from iic_booking.users.legacy_ledger.reconcile import run_full_reconciliation
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
    PortalMigrationPhase,
    PortalMigrationPhaseTransition,
    PortalMigrationState,
)
from iic_booking.users.models.user_type import UserType


def _is_migration_admin(user) -> bool:
    return bool(getattr(user, "is_superuser", False) or getattr(user, "user_type", None) == UserType.ADMIN)


def _dashboard_payload() -> dict:
    state = PortalMigrationState.get_solo()
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
    return {
        "health": health,
        "phase": state.phase,
        "end_user_booking_enabled": state.end_user_booking_enabled,
        "incremental_sync_enabled": state.incremental_sync_enabled,
        "legacy_ledger_frozen": state.legacy_ledger_frozen,
        "old_mysql_configured": mysql_configured,
        "old_mysql_connection_status": "CONFIGURED" if mysql_configured else "NOT_CONFIGURED",
        "last_successful_sync": state.last_sync_at.isoformat() if state.last_sync_at else None,
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
            "last_successful_sync": state.last_sync_at.isoformat() if state.last_sync_at else None,
        },
        "next_operator_hint": PHASE_OPERATOR_HINTS.get(state.phase, ""),
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
    state = PortalMigrationState.get_solo()
    if request.method == "PATCH":
        data = request.data or {}
        if "phase" in data:
            return Response(
                {"error": "Use POST /portal-migration/admin/transition/ for explicit phase changes."},
                status=status.HTTP_400_BAD_REQUEST,
            )
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
