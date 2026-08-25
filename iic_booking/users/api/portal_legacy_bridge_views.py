"""Phase 8B/10D admin APIs: equipment mapping, legacy bookings, blocks (Main Administrator)."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.equipment.models import Equipment
from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic
from iic_booking.users.legacy_ledger.booking_bridge import (
    abort_migration_batch,
    reconcile_legacy_blocks,
)
from iic_booking.users.legacy_ledger.equipment_mapping import (
    validate_legacy_equipment_mapping_save,
    validate_legacy_equipment_mappings,
)
from iic_booking.users.legacy_ledger.legacy_booking_admin import (
    build_equipment_mapping_table,
    build_legacy_booking_list,
    build_migration_summary_dashboard,
    count_bookings_by_legacy_equipment,
    is_main_admin,
    is_oic_or_admin,
    run_legacy_booking_discovery,
)
from iic_booking.users.legacy_ledger.legacy_conflict_analysis import analyze_booking_conflicts
from iic_booking.users.legacy_ledger.datetime_contract import (
    approve_datetime_contract,
    datetime_contract_ui_payload,
    load_datetime_contract,
)
from iic_booking.users.legacy_ledger.schema_gate import (
    bridge_schema_ready_for_orm,
    portal_bridge_schema_status,
    safe_portal_migration_state,
    schema_pending_payload,
    schema_pending_response,
)
from iic_booking.users.legacy_ledger.legacy_equipment_mapping_import import (
    default_mapping_file_path,
    preview_equipment_mapping_import,
    validate_equipment_mapping_file,
    parse_equipment_mapping_file,
)
from iic_booking.users.legacy_ledger.legacy_upcoming_discovery import discover_upcoming_legacy_week
from iic_booking.users.legacy_ledger.test_account_dry_run import test_account_cleanup_dry_run
from iic_booking.users.legacy_ledger.legacy_equipment_inventory import fetch_new_portal_equipment_inventory
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
    return is_main_admin(user)


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
    """Main Administrator: list/create explicit OLD→NEW equipment mappings. OIC: read-only GET scoped."""
    if request.method == "POST" and not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "GET" and not is_oic_or_admin(request.user):
        return Response({"error": "Administrator or OIC access required."}, status=status.HTTP_403_FORBIDDEN)

    if not bridge_schema_ready_for_orm():
        # Expected until users.0102 — do not 500 on missing LegacyEquipmentMapping table.
        return schema_pending_response(
            endpoint="equipment-mappings",
            datetime_contract=datetime_contract_ui_payload(load_datetime_contract()),
            new_equipment_options=[],
            validation_counts={},
            discovery_counts={},
            equipment_window_stats={
                "total_legacy_equipment": 0,
                "used_in_migration_window": 0,
                "mapped_in_window": 0,
                "unmapped_in_window": 0,
            },
        )

    if request.method == "GET":
        qs = LegacyEquipmentMapping.objects.select_related("new_equipment", "department").all()
        dept = request.GET.get("department_id")
        mstatus = (request.GET.get("mapping_status") or request.GET.get("status") or "").strip()
        mapped_filter = (request.GET.get("mapped") or "").strip().lower()
        if dept:
            qs = qs.filter(department_id=dept)
        if mstatus:
            qs = qs.filter(status=mstatus)
        limit = min(int(request.GET.get("limit", 500)), 2000)
        rows = [_mapping_row(m) for m in qs.order_by("old_equipment_id")[:limit]]
        report = validate_legacy_equipment_mappings()
        discovery = run_legacy_booking_discovery([])
        booking_counts = count_bookings_by_legacy_equipment(discovery)
        table = build_equipment_mapping_table(booking_counts=booking_counts)
        eligible_equipment_ids = {int(k) for k, v in booking_counts.items() if v > 0}
        mapped_in_window = sum(
            1
            for r in table
            if r.get("mapping_status") in (LegacyEquipmentMappingStatus.ACTIVE, "CAPACITY_SPLIT")
            and int(r.get("legacy_booking_count") or 0) > 0
        )
        not_required_statuses = {
            LegacyEquipmentMappingStatus.DISABLED,
            LegacyEquipmentMappingStatus.RETIRED,
        }
        mapped_statuses = {LegacyEquipmentMappingStatus.ACTIVE, "CAPACITY_SPLIT"} | not_required_statuses
        unmapped_in_window = sum(
            1
            for r in table
            if r.get("mapping_status") not in mapped_statuses
            and int(r.get("legacy_booking_count") or 0) > 0
        )
        if mapped_filter == "mapped":
            table = [
                r
                for r in table
                if r.get("mapping_status") in (LegacyEquipmentMappingStatus.ACTIVE, "CAPACITY_SPLIT")
            ]
        elif mapped_filter == "unmapped":
            table = [r for r in table if r.get("mapping_status") not in mapped_statuses]
        elif mapped_filter in ("not_required", "retired", "disabled"):
            table = [r for r in table if r.get("mapping_status") in not_required_statuses]
        min_bookings = request.GET.get("min_booking_count")
        if min_bookings is not None:
            try:
                mb = int(min_bookings)
                table = [r for r in table if int(r.get("legacy_booking_count") or 0) >= mb]
            except ValueError:
                pass
        search = (request.GET.get("search") or request.GET.get("q") or "").strip().lower()
        if search:
            table = [
                r
                for r in table
                if search in str(r.get("old_equipment_id") or "").lower()
                or search in (r.get("old_equipment_name") or "").lower()
                or search in (r.get("new_equipment_name") or "").lower()
            ]
        if not _is_main_admin(request.user):
            oic_eq = set(get_equipment_ids_managed_by_oic(request.user.id))
            table = [r for r in table if r.get("new_equipment_id") in oic_eq]
        new_equipment = fetch_new_portal_equipment_inventory().get("equipment") or []
        from iic_booking.users.legacy_ledger.capacity_split import capacity_split_row
        from iic_booking.users.models.portal_migration import LegacyEquipmentCapacitySplit

        split_rows = [
            capacity_split_row(s)
            for s in LegacyEquipmentCapacitySplit.objects.select_related("target_a", "target_b").order_by(
                "old_equipment_id"
            )
        ]
        return Response(
            {
                "count": qs.count(),
                "results": rows,
                "table": table,
                "capacity_splits": split_rows,
                "new_equipment_options": new_equipment,
                "validation_counts": report["counts"],
                "discovery_counts": discovery.get("counts"),
                "equipment_window_stats": {
                    "total_legacy_equipment": len(table),
                    "used_in_migration_window": len(eligible_equipment_ids),
                    "mapped_in_window": mapped_in_window,
                    "unmapped_in_window": unmapped_in_window,
                },
                "datetime_contract": datetime_contract_ui_payload(load_datetime_contract()),
            },
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
    validation = validate_legacy_equipment_mapping_save(
        old_equipment_id=int(old_id),
        new_equipment_id=int(new_id) if new_id is not None else None,
        status=map_status,
    )
    if not validation["valid"]:
        return Response(
            {"error": "validation_failed", "errors": validation["errors"], "warnings": validation["warnings"]},
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


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_legacy_equipment_mapping_export(request):
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    discovery = run_legacy_booking_discovery([])
    booking_counts = count_bookings_by_legacy_equipment(discovery)
    table = build_equipment_mapping_table(booking_counts=booking_counts)
    return Response({"count": len(table), "results": table}, status=status.HTTP_200_OK)


@api_view(["GET", "PATCH", "DELETE"])
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
    if request.method == "DELETE":
        old_id = m.old_equipment_id
        m.delete()
        return Response(
            {"ok": True, "deleted": True, "mapping_id": mapping_id, "old_equipment_id": old_id},
            status=status.HTTP_200_OK,
        )
    data = request.data or {}
    new_status = m.status
    new_eq_id = getattr(m.new_equipment, "equipment_id", None) if m.new_equipment else None
    if "status" in data:
        st = str(data.get("status") or "").upper()
        if st not in LegacyEquipmentMappingStatus.values:
            return Response({"error": "invalid status"}, status=status.HTTP_400_BAD_REQUEST)
        new_status = st
    if "new_equipment_id" in data:
        raw = data.get("new_equipment_id")
        if raw in (None, ""):
            new_eq_id = None
        else:
            try:
                new_eq_id = int(raw)
            except ValueError:
                return Response({"error": "invalid new_equipment_id"}, status=status.HTTP_400_BAD_REQUEST)
    # Unmap / not-required: clearing the new link must not leave status ACTIVE.
    if new_eq_id is None and new_status == LegacyEquipmentMappingStatus.ACTIVE:
        if "status" in data:
            return Response(
                {"error": "ACTIVE mapping requires new_equipment_id"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        new_status = LegacyEquipmentMappingStatus.UNMAPPED
    validation = validate_legacy_equipment_mapping_save(
        old_equipment_id=m.old_equipment_id,
        new_equipment_id=new_eq_id,
        status=new_status,
        exclude_mapping_id=m.id,
    )
    if not validation["valid"]:
        return Response(
            {"error": "validation_failed", "errors": validation["errors"], "warnings": validation["warnings"]},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if "status" in data or new_status != m.status:
        m.status = new_status
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


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def portal_legacy_equipment_capacity_splits(request):
    """List / create capacity-split mappings (1 legacy → 2 new machines)."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    if not bridge_schema_ready_for_orm():
        return schema_pending_response(endpoint="equipment-capacity-splits")

    from iic_booking.users.legacy_ledger.capacity_split import (
        POLICY_SCHEME_LABEL,
        capacity_split_row,
        supersede_one_to_one_mapping,
    )
    from iic_booking.users.models.portal_migration import (
        LegacyEquipmentCapacitySplit,
        LegacyEquipmentCapacitySplitPolicy,
        LegacyEquipmentCapacitySplitStatus,
    )

    if request.method == "GET":
        rows = [
            capacity_split_row(s)
            for s in LegacyEquipmentCapacitySplit.objects.select_related("target_a", "target_b").order_by(
                "old_equipment_id"
            )
        ]
        return Response(
            {
                "count": len(rows),
                "results": rows,
                "policies": [
                    {"id": p.value, "label": POLICY_SCHEME_LABEL.get(p.value, p.label)}
                    for p in LegacyEquipmentCapacitySplitPolicy
                ],
            },
            status=status.HTTP_200_OK,
        )

    data = request.data or {}
    old_id = data.get("old_equipment_id")
    target_a_id = data.get("target_a_id") or data.get("target_a")
    target_b_id = data.get("target_b_id") or data.get("target_b")
    if old_id is None or target_a_id is None or target_b_id is None:
        return Response(
            {"error": "old_equipment_id, target_a_id, and target_b_id are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if int(target_a_id) == int(target_b_id):
        return Response({"error": "target_a and target_b must be different equipment."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        target_a = Equipment.objects.get(pk=int(target_a_id))
        target_b = Equipment.objects.get(pk=int(target_b_id))
    except (ValueError, Equipment.DoesNotExist):
        return Response({"error": "invalid target_a_id or target_b_id"}, status=status.HTTP_400_BAD_REQUEST)

    policy = str(data.get("policy") or LegacyEquipmentCapacitySplitPolicy.TIME_BAND_FOLD).upper()
    if policy not in LegacyEquipmentCapacitySplitPolicy.values:
        return Response({"error": "invalid policy"}, status=status.HTTP_400_BAD_REQUEST)
    st = str(data.get("status") or LegacyEquipmentCapacitySplitStatus.ACTIVE).upper()
    if st not in LegacyEquipmentCapacitySplitStatus.values:
        return Response({"error": "invalid status"}, status=status.HTTP_400_BAD_REQUEST)

    if LegacyEquipmentCapacitySplit.objects.filter(old_equipment_id=int(old_id)).exists():
        return Response(
            {"error": "capacity_split_already_exists", "detail": "PATCH the existing split instead."},
            status=status.HTTP_409_CONFLICT,
        )

    split = LegacyEquipmentCapacitySplit.objects.create(
        old_equipment_id=int(old_id),
        old_equipment_code=str(data.get("old_equipment_code") or ""),
        old_equipment_name=str(data.get("old_equipment_name") or ""),
        target_a=target_a,
        target_b=target_b,
        policy=policy,
        status=st,
        notes=str(data.get("notes") or ""),
        created_by=request.user,
        updated_by=request.user,
    )
    superseded = 0
    if st == LegacyEquipmentCapacitySplitStatus.ACTIVE:
        superseded = supersede_one_to_one_mapping(int(old_id), actor=request.user)
    payload = capacity_split_row(split)
    payload["superseded_one_to_one_mappings"] = superseded
    return Response(payload, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def portal_legacy_equipment_capacity_split_detail(request, split_id: int):
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    if not bridge_schema_ready_for_orm():
        return schema_pending_response(endpoint="equipment-capacity-splits")

    from iic_booking.users.legacy_ledger.capacity_split import (
        capacity_split_row,
        supersede_one_to_one_mapping,
    )
    from iic_booking.users.models.portal_migration import (
        LegacyEquipmentCapacitySplit,
        LegacyEquipmentCapacitySplitPolicy,
        LegacyEquipmentCapacitySplitStatus,
    )

    try:
        split = LegacyEquipmentCapacitySplit.objects.select_related("target_a", "target_b").get(pk=split_id)
    except LegacyEquipmentCapacitySplit.DoesNotExist:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(capacity_split_row(split), status=status.HTTP_200_OK)

    if request.method == "DELETE":
        oid = split.old_equipment_id
        split.delete()
        return Response({"ok": True, "deleted": True, "old_equipment_id": oid}, status=status.HTTP_200_OK)

    data = request.data or {}
    if "target_a_id" in data or "target_a" in data:
        raw = data.get("target_a_id", data.get("target_a"))
        try:
            split.target_a = Equipment.objects.get(pk=int(raw))
        except (TypeError, ValueError, Equipment.DoesNotExist):
            return Response({"error": "invalid target_a_id"}, status=status.HTTP_400_BAD_REQUEST)
    if "target_b_id" in data or "target_b" in data:
        raw = data.get("target_b_id", data.get("target_b"))
        try:
            split.target_b = Equipment.objects.get(pk=int(raw))
        except (TypeError, ValueError, Equipment.DoesNotExist):
            return Response({"error": "invalid target_b_id"}, status=status.HTTP_400_BAD_REQUEST)
    if split.target_a_id == split.target_b_id:
        return Response({"error": "target_a and target_b must be different equipment."}, status=status.HTTP_400_BAD_REQUEST)
    if "policy" in data:
        policy = str(data.get("policy") or "").upper()
        if policy not in LegacyEquipmentCapacitySplitPolicy.values:
            return Response({"error": "invalid policy"}, status=status.HTTP_400_BAD_REQUEST)
        split.policy = policy
    if "status" in data:
        st = str(data.get("status") or "").upper()
        if st not in LegacyEquipmentCapacitySplitStatus.values:
            return Response({"error": "invalid status"}, status=status.HTTP_400_BAD_REQUEST)
        split.status = st
    for field in ("old_equipment_code", "old_equipment_name", "notes"):
        if field in data:
            setattr(split, field, str(data.get(field) or ""))
    split.updated_by = request.user
    split.save()
    superseded = 0
    if split.status == LegacyEquipmentCapacitySplitStatus.ACTIVE:
        superseded = supersede_one_to_one_mapping(split.old_equipment_id, actor=request.user)
    payload = capacity_split_row(split)
    payload["superseded_one_to_one_mappings"] = superseded
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def portal_legacy_equipment_capacity_split_preview(request, split_id: int):
    """Preview TIME_BAND_FOLD assignment for provided legacy rows (or discovery)."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    if not bridge_schema_ready_for_orm():
        return schema_pending_response(endpoint="equipment-capacity-splits-preview")

    from iic_booking.users.legacy_ledger.capacity_split import preview_capacity_split_assignments
    from iic_booking.users.models.portal_migration import LegacyEquipmentCapacitySplit

    try:
        split = LegacyEquipmentCapacitySplit.objects.select_related("target_a", "target_b").get(pk=split_id)
    except LegacyEquipmentCapacitySplit.DoesNotExist:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)

    data = request.data or {}
    rows = data.get("rows") or data.get("legacy_rows") or []
    if not rows:
        discovery = run_legacy_booking_discovery([])
        from iic_booking.users.legacy_ledger.legacy_booking_admin import discovery_rows_flat

        rows = discovery_rows_flat(discovery)
    return Response(preview_capacity_split_assignments(split, list(rows)), status=status.HTTP_200_OK)


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

    state, schema = safe_portal_migration_state()
    if not bridge_schema_ready_for_orm():
        return Response(
            {
                **schema_pending_payload(endpoint="legacy-overview"),
                "booking_migration_mode": getattr(state, "booking_migration_mode", "NORMAL") or "NORMAL",
                "migration_start_at": None,
                "migration_window_end_at": None,
                "new_portal_url": getattr(state, "new_portal_url", "") or "",
                "departments": list(Department.objects.order_by("code").values("id", "code", "name")[:500]),
                "equipment": [],
                "mapping_counts": {},
                "block_counts": {},
                "batches": [],
                "reconciliation": {"ok": False, "schema": schema},
                "note": "SCHEMA_PENDING — mapping/block tables require users.0102.",
            },
            status=status.HTTP_200_OK,
        )
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


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def portal_legacy_bookings(request):
    """
    Legacy booking → new equipment → slot occupancy preview.
    Main Admin: global. OIC: read-only scoped to managed equipment.
    """
    if not is_oic_or_admin(request.user):
        return Response({"error": "Administrator or OIC access required."}, status=status.HTTP_403_FORBIDDEN)

    if not bridge_schema_ready_for_orm():
        return schema_pending_response(
            endpoint="legacy-bookings",
            discovery_counts={},
            summary={},
            schema_note="SCHEMA_PENDING — discovery/mapping require users.0102 window columns + tables",
            conflict_report={"conflict_count": 0, "by_type": {}},
            read_only=True,
            scope="global" if _is_main_admin(request.user) else "oic_equipment",
        )

    legacy_rows = []
    if request.method == "POST":
        legacy_rows = (request.data or {}).get("legacy_rows") or []
    elif request.GET.get("legacy_rows_json"):
        import json

        try:
            legacy_rows = json.loads(request.GET.get("legacy_rows_json") or "[]")
        except json.JSONDecodeError:
            return Response({"error": "invalid legacy_rows_json"}, status=status.HTTP_400_BAD_REQUEST)

    discovery = run_legacy_booking_discovery(legacy_rows)
    eligible = [r for r in (discovery.get("eligible") or [])]
    conflict_report = analyze_booking_conflicts(eligible) if eligible else {"conflict_count": 0, "conflicts": []}
    conflict_by_id = {c["legacy_booking_id"]: c.get("conflict_type") for c in conflict_report.get("conflicts") or []}
    for bucket in discovery:
        if isinstance(discovery.get(bucket), list):
            for row in discovery[bucket]:
                if row.get("legacy_booking_id") in conflict_by_id:
                    row["conflict_status"] = conflict_by_id[row["legacy_booking_id"]]
    filters = {
        "eligibility": request.GET.get("eligibility") or request.GET.get("migration_status"),
        "user_mapping_status": request.GET.get("user_mapping_status"),
        "old_equipment_id": request.GET.get("old_equipment_id") or request.GET.get("equipment_id"),
        "search": request.GET.get("search") or request.GET.get("q"),
    }
    results = build_legacy_booking_list(
        discovery,
        actor=request.user,
        include_pii=bool(request.GET.get("include_pii")),
        filters=filters,
    )
    limit = min(int(request.GET.get("limit", 500)), 2000)
    offset = max(int(request.GET.get("offset", 0)), 0)
    page = results[offset : offset + limit]
    summary = build_migration_summary_dashboard(discovery)
    return Response(
        {
            "count": len(results),
            "results": page,
            "discovery_counts": discovery.get("counts"),
            "summary": summary,
            "schema_note": discovery.get("schema_note"),
            "conflict_report": {
                "conflict_count": conflict_report.get("conflict_count", 0),
                "by_type": conflict_report.get("by_type"),
            },
            "read_only": not _is_main_admin(request.user),
            "scope": "global" if _is_main_admin(request.user) else "oic_equipment",
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_migration_summary_dashboard(request):
    """Phase 10D summary metrics for Main Administrator migration dashboard."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    if not bridge_schema_ready_for_orm():
        return schema_pending_response(endpoint="migration-summary")
    legacy_rows_param = request.GET.get("include_discovery")
    discovery = run_legacy_booking_discovery([]) if legacy_rows_param else {"counts": {}}
    payload = build_migration_summary_dashboard(discovery)
    payload["reconciliation"] = reconcile_legacy_blocks()
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_legacy_booking_detail(request, legacy_booking_id: int):
    """Inspect one legacy booking block / discovery row (scoped)."""
    if not is_oic_or_admin(request.user):
        return Response({"error": "Administrator or OIC access required."}, status=status.HTTP_403_FORBIDDEN)
    block = (
        LegacyBookingBlock.objects.select_related("new_equipment", "resolved_user", "migration_batch")
        .filter(legacy_booking_id=legacy_booking_id)
        .order_by("-created_at")
        .first()
    )
    if block:
        if not _is_main_admin(request.user):
            oic_scope = set(get_equipment_ids_managed_by_oic(request.user.id))
            if block.new_equipment_id not in oic_scope:
                return Response({"error": "Out of equipment scope."}, status=status.HTTP_403_FORBIDDEN)
        return Response(
            {
                "legacy_booking_id": block.legacy_booking_id,
                "legacy_user_id": block.legacy_user_id,
                "legacy_employee_id": block.legacy_employee_id,
                "legacy_equipment_id": block.legacy_equipment_id,
                "new_equipment_id": block.new_equipment_id,
                "start_at": block.start_at.isoformat(),
                "end_at": block.end_at.isoformat(),
                "duration_minutes": block.duration_minutes,
                "source_status": block.source_status,
                "block_status": block.status,
                "user_mapping_status": block.user_mapping_status,
                "user_mapping_source": block.user_mapping_source,
                "resolved_user_id": block.resolved_user_id,
                "slot_ids": block.slot_ids,
                "migration_batch_id": block.migration_batch_id,
                "created_at": block.created_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )
    return Response({"error": "Not found.", "legacy_booking_id": legacy_booking_id}, status=status.HTTP_404_NOT_FOUND)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def portal_datetime_contract(request):
    """Operator datetime contract status + Main Administrator approval (Phase 10F)."""
    if not is_oic_or_admin(request.user):
        return Response({"error": "Administrator or OIC access required."}, status=status.HTTP_403_FORBIDDEN)

    schema = portal_bridge_schema_status()

    if request.method == "POST":
        if not _is_main_admin(request.user):
            return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
        body = request.data if isinstance(request.data, dict) else {}
        # File-based approval — does not require users.0102 window columns.
        result = approve_datetime_contract(
            approved_by=str(body.get("approved_by") or request.user.email or request.user.pk),
            approval_reason=str(body.get("approval_reason") or "").strip(),
            confirm=bool(body.get("confirm")),
            approved_strategy=body.get("approved_strategy"),
            actor_user_id=request.user.pk,
            actor_email=getattr(request.user, "email", "") or "",
        )
        result["schema"] = schema
        if not result.get("ok"):
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_200_OK)

    contract = load_datetime_contract()
    validation_report = None
    # Skip MySQL/ORM validation when 0102 columns missing — ProgrammingError poisons ATOMIC_REQUESTS.
    if _is_main_admin(request.user) and schema.get("has_migration_start_at"):
        try:
            from iic_booking.users.legacy_ledger.legacy_datetime_validation import validate_legacy_datetime_readonly

            validation_report = validate_legacy_datetime_readonly()
        except Exception:  # noqa: BLE001 — embed validation when possible; never fail GET
            validation_report = None
    payload = datetime_contract_ui_payload(contract, validation_report=validation_report)
    payload["schema"] = schema
    if not schema.get("ready"):
        payload["schema_gate"] = "OPERATOR_REQUIRED"
        payload["schema_note"] = (
            "Datetime contract is file-based and usable before Migrate Production. "
            "Migration window persist requires users.0102 (migration_start_at)."
        )
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def portal_legacy_conflict_analysis(request):
    """Read-only conflict report for eligible legacy bookings."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    if request.GET.get("use_upcoming"):
        report = discover_upcoming_legacy_week()
        eligible = [
            {
                "legacy_booking_id": c.get("legacy_booking_id"),
                "old_equipment_id": c.get("legacy_equipment_id"),
                "start_at": c.get("legacy_booking_start"),
                "end_at": c.get("legacy_booking_end"),
                "new_equipment_id": c.get("new_equipment_id"),
            }
            for c in report.get("candidates") or []
            if c.get("eligibility") == "eligible"
        ]
    else:
        legacy_rows = (request.data or {}).get("legacy_rows") if request.method == "POST" else []
        discovery = run_legacy_booking_discovery(legacy_rows or [])
        eligible = discovery.get("eligible") or []
    analysis = analyze_booking_conflicts(eligible)
    return Response({**analysis, "audit_mode": "READ_ONLY"}, status=status.HTTP_200_OK)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def portal_equipment_mapping_import_preview(request):
    """Preview explicit legacy_equipment_mapping.json — dry-run only in Phase 10E."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "POST":
        rows = parse_equipment_mapping_file((request.data or {}).get("mappings") or request.data or [])
        required = request.data.get("required_legacy_ids") if isinstance(request.data, dict) else None
        required_set = {int(x) for x in required} if required else None
        preview = validate_equipment_mapping_file(rows, required_legacy_ids=required_set)
        return Response({"ok": preview["valid"], "preview": preview, "dry_run": True}, status=status.HTTP_200_OK)
    path = request.GET.get("file") or str(default_mapping_file_path())
    preview = preview_equipment_mapping_import(path)
    return Response(preview, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_test_account_dry_run(request):
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    return Response(test_account_cleanup_dry_run(), status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_legacy_upcoming_discovery(request):
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    report = discover_upcoming_legacy_week()
    return Response(report, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_phase10g_go_no_go(request):
    """Phase 10G GO/NO-GO dashboard — READ-ONLY. Never activates T0."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    try:
        from iic_booking.users.legacy_ledger.phase10g_readiness_closure import build_phase10g_final_readiness

        report = build_phase10g_final_readiness(
            backup_verified=request.GET.get("backup_verified") in ("1", "true", "yes"),
            conflicts_resolved_or_excluded=request.GET.get("conflicts_resolved") in ("1", "true", "yes"),
            explicit_t0_authorization=False,
        )
        # Compact dashboard payload — no green for unverified gates
        matrix = report.get("gate_matrix") or {}
        return Response(
            {
                "phase": "10G",
                "environment": report.get("release_audit", {}).get("hard_off_runtime", {}).get("DEPLOYMENT_ENVIRONMENT"),
                "verdict": report.get("verdict"),
                "t0_executed": False,
                "production_baseline_sha": report.get("production_baseline_sha"),
                "backend_local_sha": report.get("backend_local_sha"),
                "gate_matrix": matrix,
                "blockers": report.get("blockers"),
                "datetime_contract": report.get("datetime_contract_review"),
                "schema": report.get("schema_readiness", {}).get("classification") or portal_bridge_schema_status(),
                "production_safety": report.get("production_safety"),
                "operator_next_actions": report.get("operator_next_actions"),
                "audit_mode": "READ_ONLY",
            },
            status=status.HTTP_200_OK,
        )
    except Exception as exc:  # noqa: BLE001 — never 500 GO/NO-GO for schema gaps
        return Response(
            schema_pending_payload(
                endpoint="phase10g-go-no-go",
                phase="10G",
                verdict="NOT READY — SCHEMA_PENDING",
                error=str(exc)[:300],
            ),
            status=status.HTTP_200_OK,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_phase10i_go_no_go(request):
    """Phase 10I GO/NO-GO dashboard — READ-ONLY. Never activates T0 or approves datetime."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    try:
        from iic_booking.users.legacy_ledger.phase10i_readiness_closure import (
            build_datetime_review,
            build_phase10i_final_readiness,
        )

        report = build_phase10i_final_readiness(
            backup_verified=request.GET.get("backup_verified") in ("1", "true", "yes"),
            datetime_review=build_datetime_review(),
            release_plan={"reviewed_released": False},
            explicit_evidence={"explicit_mappings": 0},
        )
        return Response(
            {
                "phase": "10I",
                "verdict": report.get("verdict"),
                "t0_executed": False,
                "production_baseline_sha": report.get("production_baseline_sha"),
                "backend_local_sha": report.get("backend_local_sha"),
                "frontend_local_sha": report.get("frontend_local_sha"),
                "gate_matrix": report.get("gate_matrix"),
                "blockers": report.get("blockers"),
                "hard_refuse_reasons": report.get("hard_refuse_reasons"),
                "datetime_contract_status": report.get("datetime_contract_status"),
                "migration_window": report.get("migration_window"),
                "discovery_status": report.get("discovery_status"),
                "production_safety": report.get("production_safety"),
                "operator_next_actions": report.get("operator_next_actions"),
                "schema": portal_bridge_schema_status(),
                "audit_mode": "READ_ONLY",
            },
            status=status.HTTP_200_OK,
        )
    except Exception as exc:  # noqa: BLE001
        return Response(
            schema_pending_payload(
                endpoint="phase10i-go-no-go",
                phase="10I",
                verdict="NOT READY — SCHEMA_PENDING",
                error=str(exc)[:300],
            ),
            status=status.HTTP_200_OK,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_phase10j_go_no_go(request):
    """Phase 10J GO/NO-GO — READ-ONLY. Never activates T0, approves datetime, or invents dates."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    try:
        from iic_booking.users.legacy_ledger.phase10i_readiness_closure import build_datetime_review
        from iic_booking.users.legacy_ledger.phase10j_readiness_closure import (
            build_phase10j_final_readiness,
        )

        report = build_phase10j_final_readiness(
            backup_verified=request.GET.get("backup_verified") in ("1", "true", "yes"),
            finance_reviewed=request.GET.get("finance_reviewed") in ("1", "true", "yes"),
            datetime_review=build_datetime_review(),
            release_plan={"reviewed_released": False},
            explicit_evidence={"explicit_mappings": 0},
            schema_migrate_authorized=False,
            equipment_mapping_authorized=False,
            discovery_result=None,
        )
        return Response(
            {
                "phase": "10J",
                "verdict": report.get("verdict"),
                "t0_executed": False,
                "production_baseline_sha": report.get("production_baseline_sha"),
                "backend_local_sha": report.get("backend_local_sha"),
                "frontend_local_sha": report.get("frontend_local_sha"),
                "gate_matrix": report.get("gate_matrix"),
                "blockers": report.get("blockers"),
                "hard_refuse_reasons": report.get("hard_refuse_reasons"),
                "operator_gate_inspection": report.get("operator_gate_inspection"),
                "datetime_contract_status": report.get("datetime_contract_status"),
                "migration_window": report.get("migration_window"),
                "discovery_status": report.get("discovery_status"),
                "discovery_executed": report.get("discovery_executed"),
                "production_safety": report.get("production_safety"),
                "operator_next_actions": report.get("operator_next_actions"),
                "work_blocked_operator_required": report.get("work_blocked_operator_required"),
                "schema": portal_bridge_schema_status(),
                "audit_mode": "READ_ONLY",
            },
            status=status.HTTP_200_OK,
        )
    except Exception as exc:  # noqa: BLE001
        return Response(
            schema_pending_payload(
                endpoint="phase10j-go-no-go",
                phase="10J",
                verdict="NOT READY — SCHEMA_PENDING",
                error=str(exc)[:300],
            ),
            status=status.HTTP_200_OK,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_phase10k_go_no_go(request):
    """Phase 10K GO/NO-GO — READ-ONLY. Never activates T0, approves datetime, invents dates, or migrates."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    try:
        from iic_booking.users.legacy_ledger.phase10i_readiness_closure import build_datetime_review
        from iic_booking.users.legacy_ledger.phase10k_readiness_closure import (
            build_phase10k_final_readiness,
        )

        report = build_phase10k_final_readiness(
            backup_verified=request.GET.get("backup_verified") in ("1", "true", "yes"),
            finance_reviewed=request.GET.get("finance_reviewed") in ("1", "true", "yes"),
            datetime_review=build_datetime_review(),
            release_plan={"reviewed_released": False},
            explicit_evidence={"explicit_mappings": 0},
            schema_migrate_authorized=False,
            equipment_mapping_authorized=False,
            discovery_result=None,
        )
        return Response(
            {
                "phase": "10K",
                "verdict": report.get("verdict"),
                "t0_executed": False,
                "production_baseline_sha": report.get("production_baseline_sha"),
                "backend_local_sha": report.get("backend_local_sha"),
                "frontend_local_sha": report.get("frontend_local_sha"),
                "gate_matrix": report.get("gate_matrix"),
                "blockers": report.get("blockers"),
                "hard_refuse_reasons": report.get("hard_refuse_reasons"),
                "operator_gate_inspection": report.get("operator_gate_inspection"),
                "datetime_contract_status": report.get("datetime_contract_status"),
                "migration_window": report.get("migration_window"),
                "discovery_status": report.get("discovery_status"),
                "discovery_executed": report.get("discovery_executed"),
                "users_0102_migration_start_at": report.get("users_0102_migration_start_at"),
                "raa_booking_regression": report.get("raa_booking_regression"),
                "production_safety": report.get("production_safety"),
                "operator_next_actions": report.get("operator_next_actions"),
                "work_blocked_operator_required": report.get("work_blocked_operator_required"),
                "schema": portal_bridge_schema_status(),
                "audit_mode": "READ_ONLY",
            },
            status=status.HTTP_200_OK,
        )
    except Exception as exc:  # noqa: BLE001
        return Response(
            schema_pending_payload(
                endpoint="phase10k-go-no-go",
                phase="10K",
                verdict="NOT READY — SCHEMA_PENDING",
                error=str(exc)[:300],
            ),
            status=status.HTTP_200_OK,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_phase10l_go_no_go(request):
    """Phase 10L GO/NO-GO — READ-ONLY consolidation. Continues independent RO/prep; never T0."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    try:
        from iic_booking.users.legacy_ledger.phase10i_readiness_closure import build_datetime_review
        from iic_booking.users.legacy_ledger.phase10l_readiness_closure import (
            build_phase10l_final_readiness,
        )

        report = build_phase10l_final_readiness(
            backup_verified=request.GET.get("backup_verified") in ("1", "true", "yes"),
            finance_reviewed=request.GET.get("finance_reviewed") in ("1", "true", "yes"),
            datetime_review=build_datetime_review(),
            release_plan={"reviewed_released": False},
            explicit_evidence={"explicit_mappings": 0},
            schema_migrate_authorized=False,
            equipment_mapping_authorized=False,
            discovery_result=None,
        )
        return Response(
            {
                "phase": "10L",
                "verdict": report.get("verdict"),
                "t0_executed": False,
                "production_baseline_sha": report.get("production_baseline_sha"),
                "backend_local_sha": report.get("backend_local_sha"),
                "frontend_local_sha": report.get("frontend_local_sha"),
                "gate_matrix": report.get("gate_matrix"),
                "blockers": report.get("blockers"),
                "hard_refuse_reasons": report.get("hard_refuse_reasons"),
                "operator_gate_inspection": report.get("operator_gate_inspection"),
                "stage_machine": report.get("stage_machine"),
                "datetime_contract_status": report.get("datetime_contract_status"),
                "migration_window": report.get("migration_window"),
                "discovery_status": report.get("discovery_status"),
                "discovery_executed": report.get("discovery_executed"),
                "users_0102_migration_start_at": report.get("users_0102_migration_start_at"),
                "raa_booking_regression": report.get("raa_booking_regression"),
                "migration_manifest_status": (report.get("migration_manifest") or {}).get("status"),
                "production_safety": report.get("production_safety"),
                "operator_next_actions": report.get("operator_next_actions"),
                "work_completed_this_phase": report.get("work_completed_this_phase"),
                "work_blocked_operator_required": report.get("work_blocked_operator_required"),
                "schema": portal_bridge_schema_status(),
                "audit_mode": "READ_ONLY",
            },
            status=status.HTTP_200_OK,
        )
    except Exception as exc:  # noqa: BLE001
        return Response(
            schema_pending_payload(
                endpoint="phase10l-go-no-go",
                phase="10L",
                verdict="NOT READY — SCHEMA_PENDING",
                error=str(exc)[:300],
            ),
            status=status.HTTP_200_OK,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_phase10m_go_no_go(request):
    """Phase 10M GO/NO-GO — gate clearance checkpoint. Never T0 / auto-approve / invent dates."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    try:
        from iic_booking.users.legacy_ledger.phase10i_readiness_closure import build_datetime_review
        from iic_booking.users.legacy_ledger.phase10m_readiness_closure import (
            build_phase10m_final_readiness,
            maybe_run_production_discovery,
        )
        from iic_booking.users.legacy_ledger.phase10j_readiness_closure import inspect_operator_gates

        gates = inspect_operator_gates(
            backup_verified=request.GET.get("backup_verified") in ("1", "true", "yes"),
            finance_reviewed=request.GET.get("finance_reviewed") in ("1", "true", "yes"),
        )
        auto = maybe_run_production_discovery(discovery_allowed=bool(gates.get("discovery_allowed")))
        report = build_phase10m_final_readiness(
            backup_verified=request.GET.get("backup_verified") in ("1", "true", "yes"),
            finance_reviewed=request.GET.get("finance_reviewed") in ("1", "true", "yes"),
            datetime_review=build_datetime_review(),
            release_plan={"reviewed_released": False},
            explicit_evidence={"explicit_mappings": 0},
            schema_migrate_authorized=False,
            equipment_mapping_authorized=False,
            discovery_result=None,
            auto_discovery_result=auto,
        )
        return Response(
            {
                "phase": "10M",
                "verdict": report.get("verdict"),
                "t0_executed": False,
                "checkpoint": "OPERATOR_GATE_CLEARANCE",
                "gates_cleared": report.get("gates_cleared"),
                "gates_not_cleared": report.get("gates_not_cleared"),
                "gate_clearance": report.get("gate_clearance"),
                "gate_matrix": report.get("gate_matrix"),
                "blockers": report.get("blockers"),
                "hard_refuse_reasons": report.get("hard_refuse_reasons"),
                "datetime_contract_status": report.get("datetime_contract_status"),
                "migration_window": report.get("migration_window"),
                "discovery_status": report.get("discovery_status"),
                "discovery_executed": report.get("discovery_executed"),
                "remaining_operator_actions": report.get("remaining_operator_actions"),
                "production_safety": report.get("production_safety"),
                "operator_next_actions": report.get("operator_next_actions"),
                "schema": portal_bridge_schema_status(),
                "audit_mode": "READ_ONLY",
            },
            status=status.HTTP_200_OK,
        )
    except Exception as exc:  # noqa: BLE001
        return Response(
            schema_pending_payload(
                endpoint="phase10m-go-no-go",
                phase="10M",
                verdict="NOT READY — SCHEMA_PENDING",
                error=str(exc)[:300],
            ),
            status=status.HTTP_200_OK,
        )
