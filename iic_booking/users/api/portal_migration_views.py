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
    PortalMigrationPhase,
    PortalMigrationPhaseTransition,
    PortalMigrationState,
)
from iic_booking.users.models.user_type import UserType


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
        "booking_opens_at": state.booking_opens_at.isoformat() if state.booking_opens_at else None,
        "booking_lock_message": state.booking_lock_message or "",
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
