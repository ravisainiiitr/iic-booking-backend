"""Phase 8B admin APIs: equipment mapping, blocks, batches (Main Administrator)."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.equipment.models import Equipment
from iic_booking.users.legacy_ledger.booking_bridge import (
    abort_migration_batch,
    reconcile_legacy_blocks,
)
from iic_booking.users.legacy_ledger.equipment_mapping import validate_legacy_equipment_mappings
from iic_booking.users.legacy_ledger.migration_dry_run import migration_dry_run
from iic_booking.users.models.portal_migration import (
    LegacyBookingBlock,
    LegacyBookingBlockStatus,
    LegacyBookingMigrationBatch,
    LegacyBookingMigrationBatchStatus,
    LegacyEquipmentMapping,
    LegacyEquipmentMappingStatus,
    PortalMigrationState,
)
from iic_booking.users.models.user_type import UserType


def _is_main_admin(user) -> bool:
    return bool(getattr(user, "is_superuser", False) or getattr(user, "user_type", None) == UserType.ADMIN)


def _mapping_row(m: LegacyEquipmentMapping) -> dict:
    eq = m.new_equipment
    return {
        "id": m.id,
        "old_equipment_id": m.old_equipment_id,
        "old_equipment_code": m.old_equipment_code,
        "old_equipment_name": m.old_equipment_name,
        "new_equipment_id": getattr(eq, "equipment_id", None),
        "new_equipment_code": getattr(eq, "code", "") if eq else "",
        "new_equipment_name": getattr(eq, "name", "") if eq else "",
        "department_id": m.department_id,
        "status": m.status,
        "mapping_reason": m.mapping_reason,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def portal_legacy_equipment_mappings(request):
    """Main Administrator: list/create explicit OLD→NEW equipment mappings."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)

    if request.method == "GET":
        qs = LegacyEquipmentMapping.objects.select_related("new_equipment", "department").all()
        dept = request.GET.get("department_id")
        mstatus = (request.GET.get("mapping_status") or request.GET.get("status") or "").strip()
        if dept:
            qs = qs.filter(department_id=dept)
        if mstatus:
            qs = qs.filter(status=mstatus)
        limit = min(int(request.GET.get("limit", 500)), 2000)
        rows = [_mapping_row(m) for m in qs.order_by("old_equipment_id")[:limit]]
        report = validate_legacy_equipment_mappings()
        return Response(
            {"count": qs.count(), "results": rows, "validation_counts": report["counts"]},
            status=status.HTTP_200_OK,
        )

    data = request.data or {}
    old_id = data.get("old_equipment_id")
    if old_id is None:
        return Response({"error": "old_equipment_id required."}, status=status.HTTP_400_BAD_REQUEST)
    new_eq = None
    new_id = data.get("new_equipment_id")
    if new_id is not None:
        try:
            new_eq = Equipment.objects.get(pk=int(new_id))
        except (ValueError, Equipment.DoesNotExist):
            return Response({"error": "invalid new_equipment_id"}, status=status.HTTP_400_BAD_REQUEST)
    map_status = str(data.get("status") or LegacyEquipmentMappingStatus.UNMAPPED).upper()
    if map_status not in LegacyEquipmentMappingStatus.values:
        return Response({"error": "invalid status"}, status=status.HTTP_400_BAD_REQUEST)
    if map_status == LegacyEquipmentMappingStatus.ACTIVE and new_eq is None:
        return Response(
            {"error": "ACTIVE mapping requires new_equipment_id"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    dept_id = data.get("department_id")
    if dept_id is None and new_eq is not None:
        dept_id = new_eq.internal_department_id
    m = LegacyEquipmentMapping.objects.create(
        old_equipment_id=int(old_id),
        old_equipment_code=str(data.get("old_equipment_code") or ""),
        old_equipment_name=str(data.get("old_equipment_name") or ""),
        new_equipment=new_eq,
        department_id=dept_id,
        status=map_status,
        mapping_reason=str(data.get("mapping_reason") or ""),
        created_by=request.user,
        updated_by=request.user,
    )
    return Response(_mapping_row(m), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def portal_legacy_equipment_mapping_detail(request, mapping_id):
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    try:
        m = LegacyEquipmentMapping.objects.select_related("new_equipment").get(pk=mapping_id)
    except LegacyEquipmentMapping.DoesNotExist:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "GET":
        return Response(_mapping_row(m), status=status.HTTP_200_OK)
    data = request.data or {}
    if "status" in data:
        st = str(data.get("status") or "").upper()
        if st not in LegacyEquipmentMappingStatus.values:
            return Response({"error": "invalid status"}, status=status.HTTP_400_BAD_REQUEST)
        m.status = st
    if "new_equipment_id" in data:
        raw = data.get("new_equipment_id")
        if raw in (None, ""):
            m.new_equipment = None
        else:
            try:
                m.new_equipment = Equipment.objects.get(pk=int(raw))
            except (ValueError, Equipment.DoesNotExist):
                return Response({"error": "invalid new_equipment_id"}, status=status.HTTP_400_BAD_REQUEST)
    for field in ("old_equipment_code", "old_equipment_name", "mapping_reason"):
        if field in data:
            setattr(m, field, str(data.get(field) or ""))
    if "department_id" in data:
        m.department_id = data.get("department_id")
    m.updated_by = request.user
    m.save()
    return Response(_mapping_row(m), status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_legacy_equipment_mapping_validate(request):
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    return Response(validate_legacy_equipment_mappings(), status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_legacy_booking_blocks(request):
    """Main Admin global view of legacy blocks + optional filters."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    qs = LegacyBookingBlock.objects.select_related("new_equipment", "migration_batch").all()
    dept = request.GET.get("department_id")
    equipment_id = request.GET.get("equipment_id")
    bstatus = (request.GET.get("status") or "").strip()
    if dept:
        qs = qs.filter(new_equipment__internal_department_id=dept)
    if equipment_id:
        qs = qs.filter(new_equipment_id=equipment_id)
    if bstatus:
        qs = qs.filter(status=bstatus)
    limit = min(int(request.GET.get("limit", 200)), 1000)
    rows = []
    for b in qs.order_by("-created_at")[:limit]:
        rows.append(
            {
                "id": b.id,
                "legacy_booking_id": b.legacy_booking_id,
                "new_equipment_id": b.new_equipment_id,
                "new_equipment_code": b.new_equipment.code,
                "department_id": b.new_equipment.internal_department_id,
                "start_at": b.start_at.isoformat(),
                "end_at": b.end_at.isoformat(),
                "status": b.status,
                "migration_batch_id": b.migration_batch_id,
                "slot_ids": b.slot_ids,
                "released_at": b.released_at.isoformat() if b.released_at else None,
                "released_reason": b.released_reason,
            }
        )
    return Response({"count": qs.count(), "results": rows}, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_legacy_migration_overview(request):
    """Departments → equipment → mapping → blocks → settlement visibility for Main Admin."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.users.models import Department

    state = PortalMigrationState.get_solo()
    mapping_counts = validate_legacy_equipment_mappings()["counts"]
    block_counts = {
        st: LegacyBookingBlock.objects.filter(status=st).count()
        for st in LegacyBookingBlockStatus.values
    }
    depts = list(
        Department.objects.order_by("code").values("id", "code", "name")[:500]
    )
    eq_qs = Equipment.objects.select_related("internal_department").order_by("code")
    dept_filter = request.GET.get("department_id")
    if dept_filter:
        eq_qs = eq_qs.filter(internal_department_id=dept_filter)
    equipment_rows = []
    for eq in eq_qs[:1000]:
        maps = list(
            LegacyEquipmentMapping.objects.filter(new_equipment=eq).values(
                "id", "old_equipment_id", "status"
            )[:5]
        )
        equipment_rows.append(
            {
                "equipment_id": eq.equipment_id,
                "code": eq.code,
                "name": eq.name,
                "department_id": eq.internal_department_id,
                "department_code": getattr(eq.internal_department, "code", None),
                "mappings": maps,
                "active_blocks": LegacyBookingBlock.objects.filter(
                    new_equipment=eq, status=LegacyBookingBlockStatus.ACTIVE
                ).count(),
            }
        )
    return Response(
        {
            "booking_migration_mode": state.booking_migration_mode,
            "migration_start_at": state.migration_start_at.isoformat() if state.migration_start_at else None,
            "migration_window_end_at": (
                state.migration_window_end_at.isoformat() if state.migration_window_end_at else None
            ),
            "new_portal_url": state.new_portal_url,
            "departments": depts,
            "equipment": equipment_rows,
            "mapping_counts": mapping_counts,
            "block_counts": block_counts,
            "batches": list(
                LegacyBookingMigrationBatch.objects.order_by("-started_at").values(
                    "id", "status", "window_start", "window_end", "counts", "started_at", "completed_at"
                )[:20]
            ),
            "reconciliation": reconcile_legacy_blocks(),
            "note": "Main Administrator is not department-scoped. Other admin roles remain scoped.",
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def portal_legacy_migration_dry_run(request):
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    rows = (request.data or {}).get("legacy_rows") or []
    return Response(migration_dry_run(rows), status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def portal_legacy_batch_abort(request, batch_id):
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    try:
        batch = LegacyBookingMigrationBatch.objects.get(pk=batch_id)
    except LegacyBookingMigrationBatch.DoesNotExist:
        return Response({"error": "Batch not found."}, status=status.HTTP_404_NOT_FOUND)
    if batch.status == LegacyBookingMigrationBatchStatus.COMPLETED:
        return Response(
            {
                "error": "Batch already completed; abort will not reverse Phase-8A financial settlements.",
                "code": "ABORT_AFTER_COMPLETION",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    confirm = bool((request.data or {}).get("confirm"))
    if not confirm:
        return Response({"error": "Set confirm=true"}, status=status.HTTP_400_BAD_REQUEST)
    result = abort_migration_batch(batch, reason=str((request.data or {}).get("reason") or "aborted"))
    # Restore old-portal booking capability only if cutover not irreversible
    state = PortalMigrationState.get_solo()
    mode = (state.booking_migration_mode or "").upper()
    if mode in {"FREEZE", "ACTIVE", "PREPARATION"} and mode != "COMPLETED":
        # Soft restore signal for external old portal; do not auto-unlock new portal freeze flag.
        state.booking_migration_mode = "NORMAL"
        state.save(update_fields=["booking_migration_mode", "updated_at"])
        result["booking_migration_mode"] = state.booking_migration_mode
    return Response(result, status=status.HTTP_200_OK)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def portal_legacy_portal_action_gate(request):
    """Staging/verification API for OLD portal freeze signal (create/reschedule/waitlist/sample)."""
    from iic_booking.users.legacy_ledger.booking_lock import legacy_portal_mutating_booking_blocked

    action = (request.data.get("action") if request.method == "POST" else request.GET.get("action")) or "create"
    action = str(action).lower()
    blocked, code, message = legacy_portal_mutating_booking_blocked()
    if action in {"create", "reschedule", "waitlist", "sample", "new_booking"} and blocked:
        return Response(
            {"error": message, "code": code, "action": action, "allowed": False},
            status=status.HTTP_403_FORBIDDEN,
        )
    return Response(
        {"allowed": True, "action": action, "code": "", "message": ""},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_migration_email_preview(request):
    """Main Admin preview of role-specific migration emails (sample data only)."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.users.legacy_ledger.migration_notifications import preview_templates

    return Response(preview_templates(), status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def portal_migration_notification_dry_run(request):
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.users.legacy_ledger.migration_notifications import create_notification_batch

    batch, report = create_notification_batch(dry_run=True, created_by=request.user)
    return Response(
        {
            "batch_id": batch.id,
            "dry_run": True,
            "emails_sent": 0,
            "counts": batch.counts,
            "skipped_sample": report.get("skipped_rows", [])[:20],
        },
        status=status.HTTP_200_OK,
    )
