"""SPA admin APIs for Analysis Software Catalog and Equipment↔Software mapping (R6.1)."""

from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils.text import slugify
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog, EquipmentAnalysisSoftware
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis, CanViewRemoteAnalysis

_VIEW = [IsAuthenticated, CanViewRemoteAnalysis]
_MANAGE = [IsAuthenticated, CanManageRemoteAnalysis]


def _license_type_choices() -> list[tuple[str, str]]:
    field = AnalysisSoftwareCatalog._meta.get_field("license_type")
    return list(getattr(field, "choices", None) or [])


def _equipment_pk(equipment) -> int:
    """Equipment uses equipment_id as PK (not Django's default .id)."""
    return int(getattr(equipment, "pk", None) or getattr(equipment, "equipment_id"))


def _serialize_catalog(cat: AnalysisSoftwareCatalog, *, usage: dict | None = None) -> dict:
    usage = usage or {}
    return {
        "id": str(cat.id),
        "name": cat.name,
        "slug": cat.slug,
        "vendor": cat.vendor,
        "version_constraint": cat.version_constraint,
        "license_type": cat.license_type,
        "max_concurrent": cat.max_concurrent,
        "license_server_url": getattr(cat, "license_server_url", "") or "",
        "license_seats": getattr(cat, "license_seats", 0) or 0,
        "description": cat.description,
        "typical_usage": getattr(cat, "typical_usage", "") or "",
        "accepted_file_types": getattr(cat, "accepted_file_types", None) or [],
        "category": cat.category,
        "icon_url": cat.icon_url,
        "default_session_duration_hours": cat.default_session_duration_hours,
        "is_active": cat.is_active,
        "is_archived": getattr(cat, "is_archived", False),
        "ai_tags": cat.ai_tags or [],
        "ai_metadata": cat.ai_metadata or {},
        "supported_department_ids": list(cat.supported_departments.values_list("id", flat=True)),
        "capability_ids": [str(c.id) for c in cat.capabilities.all()],
        "software_requirement_id": str(cat.software_requirement_id) if cat.software_requirement_id else None,
        "equipment_mapping_count": usage.get("equipment_mapping_count", cat.equipment_mappings.count()),
        "installed_match_count": usage.get("installed_match_count", 0),
        "created_at": cat.created_at.isoformat() if cat.created_at else None,
        "updated_at": cat.updated_at.isoformat() if cat.updated_at else None,
    }


def _serialize_mapping(row: EquipmentAnalysisSoftware) -> dict:
    cat = row.catalog
    eq = row.equipment
    return {
        "id": str(row.id),
        "equipment_id": _equipment_pk(eq),
        "equipment_name": getattr(eq, "name", "") or "",
        "equipment_code": getattr(eq, "code", "") or "",
        "department_id": getattr(eq, "internal_department_id", None),
        "department_name": getattr(getattr(eq, "internal_department", None), "name", None),
        "catalog_id": str(cat.id),
        "catalog_name": cat.name,
        "catalog_slug": cat.slug,
        "catalog_vendor": cat.vendor,
        "catalog_is_active": cat.is_active,
        "is_default": bool(row.is_default),
        "button_label_override": row.button_label_override,
        "sort_order": row.sort_order,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _apply_catalog_body(cat: AnalysisSoftwareCatalog, body: dict, *, creating: bool = False) -> list[str]:
    errors: list[str] = []
    if creating or "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            errors.append("name is required")
        else:
            cat.name = name
    if "slug" in body and body.get("slug"):
        cat.slug = slugify(str(body["slug"])) or cat.slug
    for field in (
        "vendor",
        "version_constraint",
        "description",
        "typical_usage",
        "category",
        "icon_url",
        "license_server_url",
    ):
        if field in body:
            setattr(cat, field, str(body.get(field) or ""))
    if "license_type" in body:
        value = str(body.get("license_type") or "").strip()
        valid = {c[0] for c in _license_type_choices()}
        if value and value not in valid:
            errors.append(f"license_type must be one of: {', '.join(sorted(valid))}")
        else:
            cat.license_type = value
    if "max_concurrent" in body:
        try:
            cat.max_concurrent = max(0, int(body.get("max_concurrent") or 0))
        except (TypeError, ValueError):
            errors.append("max_concurrent must be an integer")
    if "license_seats" in body:
        try:
            cat.license_seats = max(0, int(body.get("license_seats") or 0))
        except (TypeError, ValueError):
            errors.append("license_seats must be an integer")
    if "default_session_duration_hours" in body:
        try:
            cat.default_session_duration_hours = max(1, int(body.get("default_session_duration_hours") or 4))
        except (TypeError, ValueError):
            errors.append("default_session_duration_hours must be an integer")
    if "accepted_file_types" in body:
        raw = body.get("accepted_file_types") or []
        if isinstance(raw, str):
            raw = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
        if not isinstance(raw, list):
            errors.append("accepted_file_types must be a list")
        else:
            cat.accepted_file_types = [str(x).strip() for x in raw if str(x).strip()]
    if "ai_tags" in body:
        raw = body.get("ai_tags") or []
        if isinstance(raw, str):
            raw = [p.strip() for p in raw.split(",") if p.strip()]
        if not isinstance(raw, list):
            errors.append("ai_tags must be a list")
        else:
            cat.ai_tags = [str(x).strip() for x in raw if str(x).strip()]
    if "ai_metadata" in body:
        meta = body.get("ai_metadata")
        if meta is None:
            cat.ai_metadata = {}
        elif not isinstance(meta, dict):
            errors.append("ai_metadata must be an object")
        else:
            cat.ai_metadata = meta
    if "is_active" in body:
        cat.is_active = bool(body.get("is_active"))
    if "is_archived" in body:
        cat.is_archived = bool(body.get("is_archived"))
        if cat.is_archived:
            cat.is_active = False
    return errors


@api_view(["GET", "POST"])
@permission_classes(_VIEW)
def catalog_collection(request):
    """GET/POST /api/v1/analysis/catalog/software/"""
    if request.method == "POST":
        if not CanManageRemoteAnalysis().has_permission(request, None):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        body = request.data or {}
        cat = AnalysisSoftwareCatalog()
        errors = _apply_catalog_body(cat, body, creating=True)
        if errors:
            return Response({"detail": "; ".join(errors)}, status=status.HTTP_400_BAD_REQUEST)
        cat.save()
        dept_ids = body.get("supported_department_ids") or body.get("department_ids") or []
        if isinstance(dept_ids, list) and dept_ids:
            cat.supported_departments.set(dept_ids)
        cat.ensure_software_requirement()
        return Response(_serialize_catalog(cat), status=status.HTTP_201_CREATED)

    qs = AnalysisSoftwareCatalog.objects.all().annotate(
        equipment_mapping_count=Count("equipment_mappings", distinct=True)
    )
    q = (request.query_params.get("q") or request.query_params.get("search") or "").strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(slug__icontains=q)
            | Q(vendor__icontains=q)
            | Q(category__icontains=q)
            | Q(description__icontains=q)
        )
    category = (request.query_params.get("category") or "").strip()
    if category:
        qs = qs.filter(category__iexact=category)
    license_type = (request.query_params.get("license_type") or "").strip()
    if license_type:
        qs = qs.filter(license_type=license_type)
    dept = request.query_params.get("department") or request.query_params.get("department_id")
    if dept:
        qs = qs.filter(supported_departments__id=dept)
    active = (request.query_params.get("active") or "").strip().lower()
    if active in {"1", "true", "yes"}:
        qs = qs.filter(is_active=True, is_archived=False)
    elif active in {"0", "false", "no"}:
        qs = qs.filter(is_active=False)
    archived = (request.query_params.get("archived") or "").strip().lower()
    if archived in {"1", "true", "yes"}:
        qs = qs.filter(is_archived=True)
    elif archived in {"0", "false", "no"}:
        qs = qs.filter(is_archived=False)
    qs = qs.order_by("name").distinct()

    from iic_booking.remote_analysis.models import InstalledSoftware

    results = []
    for cat in qs[:500]:
        installed = InstalledSoftware.objects.filter(
            is_present=True, software_name__icontains=cat.name
        ).count()
        results.append(
            _serialize_catalog(
                cat,
                usage={
                    "equipment_mapping_count": getattr(cat, "equipment_mapping_count", 0),
                    "installed_match_count": installed,
                },
            )
        )
    return Response(
        {
            "count": len(results),
            "results": results,
            "license_types": [
                {"value": value, "label": str(label)} for value, label in _license_type_choices()
            ],
        }
    )


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes(_VIEW)
def catalog_detail(request, catalog_id):
    cat = get_object_or_404(AnalysisSoftwareCatalog, pk=catalog_id)
    if request.method == "GET":
        from iic_booking.remote_analysis.models import InstalledSoftware

        installed = InstalledSoftware.objects.filter(
            is_present=True, software_name__icontains=cat.name
        ).count()
        return Response(
            _serialize_catalog(
                cat,
                usage={
                    "equipment_mapping_count": cat.equipment_mappings.count(),
                    "installed_match_count": installed,
                },
            )
        )
    if not CanManageRemoteAnalysis().has_permission(request, None):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "DELETE":
        # Soft-disable / archive — never hard-delete to preserve mappings & history
        cat.is_active = False
        cat.is_archived = True
        cat.save(update_fields=["is_active", "is_archived", "updated_at"])
        return Response({"ok": True, "archived": True, "id": str(cat.id)})

    body = request.data or {}
    errors = _apply_catalog_body(cat, body)
    if errors:
        return Response({"detail": "; ".join(errors)}, status=status.HTTP_400_BAD_REQUEST)
    cat.save()
    if "supported_department_ids" in body or "department_ids" in body:
        dept_ids = body.get("supported_department_ids") or body.get("department_ids") or []
        if isinstance(dept_ids, list):
            cat.supported_departments.set(dept_ids)
    cat.ensure_software_requirement()
    return Response(_serialize_catalog(cat))


@api_view(["POST"])
@permission_classes(_MANAGE)
def catalog_disable(request, catalog_id):
    cat = get_object_or_404(AnalysisSoftwareCatalog, pk=catalog_id)
    cat.is_active = False
    cat.save(update_fields=["is_active", "updated_at"])
    return Response(_serialize_catalog(cat))


@api_view(["POST"])
@permission_classes(_MANAGE)
def catalog_enable(request, catalog_id):
    cat = get_object_or_404(AnalysisSoftwareCatalog, pk=catalog_id)
    cat.is_active = True
    cat.is_archived = False
    cat.save(update_fields=["is_active", "is_archived", "updated_at"])
    return Response(_serialize_catalog(cat))


@api_view(["POST"])
@permission_classes(_MANAGE)
def catalog_archive(request, catalog_id):
    cat = get_object_or_404(AnalysisSoftwareCatalog, pk=catalog_id)
    cat.is_active = False
    cat.is_archived = True
    cat.save(update_fields=["is_active", "is_archived", "updated_at"])
    return Response(_serialize_catalog(cat))


@api_view(["POST"])
@permission_classes(_MANAGE)
def workstation_software_allocation(request, install_id):
    """
    R11: enable/disable a specific InstalledSoftware row for allocation
    without uninstalling from the RAA PC.
    """
    from iic_booking.remote_analysis.models import InstalledSoftware
    from iic_booking.remote_analysis.services.audit import record_event
    from iic_booking.remote_analysis.constants import AuditCategory

    row = get_object_or_404(InstalledSoftware, pk=install_id)
    body = request.data or {}
    if "allocation_enabled" not in body:
        return Response({"detail": "allocation_enabled required"}, status=status.HTTP_400_BAD_REQUEST)
    old = bool(row.allocation_enabled)
    row.allocation_enabled = bool(body.get("allocation_enabled"))
    row.save(update_fields=["allocation_enabled", "last_updated"])
    record_event(
        category=AuditCategory.INVENTORY,
        action="AllocationEligibilityChanged",
        details=f"{row.software_name}: {old} → {row.allocation_enabled}",
        workstation=row.workstation,
        actor=request.user if getattr(request.user, "is_authenticated", False) else None,
    )
    return Response(
        {
            "id": row.id,
            "software_name": row.software_name,
            "workstation_id": str(row.workstation_id),
            "allocation_enabled": row.allocation_enabled,
            "is_present": row.is_present,
        }
    )


@api_view(["GET"])
@permission_classes(_VIEW)
def catalog_usage(request, catalog_id):
    cat = get_object_or_404(AnalysisSoftwareCatalog, pk=catalog_id)
    from iic_booking.remote_analysis.models import InstalledSoftware

    mappings = [
        {
            "id": str(m.id),
            "equipment_id": m.equipment_id,
            "equipment_name": getattr(m.equipment, "name", ""),
            "equipment_code": getattr(m.equipment, "code", ""),
            "is_default": m.is_default,
        }
        for m in cat.equipment_mappings.select_related("equipment").order_by("equipment__name")
    ]
    installed = [
        {
            "id": s.id,
            "software_name": s.software_name,
            "version": s.version,
            "publisher": s.publisher,
            "workstation_id": str(s.workstation_id),
            "workstation_hostname": s.workstation.hostname,
            "workstation_status": s.workstation.status,
            "licensed": s.licensed,
            "allocation_enabled": bool(getattr(s, "allocation_enabled", True)),
            "catalog_id": str(s.catalog_id) if s.catalog_id else None,
            "last_updated": s.last_updated.isoformat() if s.last_updated else None,
            "discovery_source": "raa_inventory",
        }
        for s in InstalledSoftware.objects.filter(
            is_present=True, software_name__icontains=cat.name
        )
        .select_related("workstation")
        .order_by("workstation__hostname")[:200]
    ]
    return Response(
        {
            "catalog": _serialize_catalog(cat),
            "equipment_mappings": mappings,
            "installed_instances": installed,
            "mapping_count": len(mappings),
            "installed_count": len(installed),
        }
    )


@api_view(["POST"])
@permission_classes(_MANAGE)
def catalog_import(request):
    """Bulk import catalog rows. Body: { items: [...] } or a bare list."""
    body = request.data or {}
    items = body.get("items") if isinstance(body, dict) else body
    if not isinstance(items, list):
        return Response({"detail": "items list required"}, status=status.HTTP_400_BAD_REQUEST)

    created = updated = skipped = 0
    errors: list[dict] = []
    with transaction.atomic():
        for idx, raw in enumerate(items):
            if not isinstance(raw, dict):
                skipped += 1
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                errors.append({"index": idx, "detail": "name required"})
                continue
            slug = slugify(str(raw.get("slug") or name)) or f"software-{idx}"
            cat = AnalysisSoftwareCatalog.objects.filter(slug=slug).first()
            if cat is None:
                cat = AnalysisSoftwareCatalog(name=name, slug=slug)
                creating = True
            else:
                creating = False
            errs = _apply_catalog_body(cat, {**raw, "name": name, "slug": slug}, creating=creating)
            if errs:
                errors.append({"index": idx, "detail": "; ".join(errs)})
                continue
            cat.save()
            cat.ensure_software_requirement()
            if creating:
                created += 1
            else:
                updated += 1
    return Response(
        {"created": created, "updated": updated, "skipped": skipped, "errors": errors},
        status=status.HTTP_200_OK,
    )


@api_view(["GET", "POST"])
@permission_classes(_VIEW)
def mapping_collection(request):
    """GET/POST /api/v1/analysis/catalog/equipment-software/"""
    if request.method == "POST":
        if not CanManageRemoteAnalysis().has_permission(request, None):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        body = request.data or {}
        equipment_id = body.get("equipment_id") or body.get("equipment")
        catalog_id = body.get("catalog_id") or body.get("catalog")
        if not equipment_id or not catalog_id:
            return Response(
                {"detail": "equipment_id and catalog_id are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from iic_booking.equipment.models import Equipment

        equipment = get_object_or_404(Equipment, pk=equipment_id)
        catalog = get_object_or_404(AnalysisSoftwareCatalog, pk=catalog_id)
        if EquipmentAnalysisSoftware.objects.filter(equipment=equipment, catalog=catalog).exists():
            return Response(
                {"detail": "Mapping already exists for this equipment and software."},
                status=status.HTTP_409_CONFLICT,
            )
        row = EquipmentAnalysisSoftware.objects.create(
            equipment=equipment,
            catalog=catalog,
            is_default=bool(body.get("is_default", False)),
            button_label_override=str(body.get("button_label_override") or ""),
            sort_order=int(body.get("sort_order") or 0),
        )
        if row.is_default:
            EquipmentAnalysisSoftware.objects.filter(equipment=equipment).exclude(pk=row.pk).update(
                is_default=False
            )
        from iic_booking.remote_analysis.constants import AuditCategory
        from iic_booking.remote_analysis.services.audit import record_event

        record_event(
            category=AuditCategory.INVENTORY,
            action="EquipmentSoftwareMappingAdded",
            details=f"equipment={_equipment_pk(equipment)} catalog={catalog.name}",
            actor=request.user if getattr(request.user, "is_authenticated", False) else None,
        )
        return Response(_serialize_mapping(row), status=status.HTTP_201_CREATED)

    qs = EquipmentAnalysisSoftware.objects.select_related(
        "equipment", "equipment__internal_department", "catalog"
    )
    equipment_id = request.query_params.get("equipment") or request.query_params.get("equipment_id")
    if equipment_id:
        qs = qs.filter(equipment_id=equipment_id)
    catalog_id = request.query_params.get("catalog") or request.query_params.get("catalog_id")
    if catalog_id:
        qs = qs.filter(catalog_id=catalog_id)
    dept = request.query_params.get("department") or request.query_params.get("department_id")
    if dept:
        qs = qs.filter(equipment__internal_department_id=dept)
    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(equipment__name__icontains=q)
            | Q(equipment__code__icontains=q)
            | Q(catalog__name__icontains=q)
        )
    qs = qs.order_by("equipment__name", "sort_order", "catalog__name")
    return Response({"count": qs.count(), "results": [_serialize_mapping(r) for r in qs[:1000]]})


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes(_VIEW)
def mapping_detail(request, mapping_id):
    row = get_object_or_404(
        EquipmentAnalysisSoftware.objects.select_related(
            "equipment", "equipment__internal_department", "catalog"
        ),
        pk=mapping_id,
    )
    if request.method == "GET":
        return Response(_serialize_mapping(row))
    if not CanManageRemoteAnalysis().has_permission(request, None):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "DELETE":
        from iic_booking.remote_analysis.constants import AuditCategory
        from iic_booking.remote_analysis.services.audit import record_event

        details = f"equipment={row.equipment_id} catalog={row.catalog.name}"
        row.delete()
        record_event(
            category=AuditCategory.INVENTORY,
            action="EquipmentSoftwareMappingRemoved",
            details=details,
            actor=request.user if getattr(request.user, "is_authenticated", False) else None,
        )
        return Response({"ok": True, "deleted": True})
    body = request.data or {}
    if "is_default" in body:
        row.is_default = bool(body.get("is_default"))
    if "button_label_override" in body:
        row.button_label_override = str(body.get("button_label_override") or "")
    if "sort_order" in body:
        try:
            row.sort_order = int(body.get("sort_order") or 0)
        except (TypeError, ValueError):
            return Response({"detail": "sort_order must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
    if "catalog_id" in body and body.get("catalog_id"):
        row.catalog = get_object_or_404(AnalysisSoftwareCatalog, pk=body["catalog_id"])
    row.save()
    if row.is_default:
        EquipmentAnalysisSoftware.objects.filter(equipment_id=row.equipment_id).exclude(pk=row.pk).update(
            is_default=False
        )
    return Response(_serialize_mapping(row))


@api_view(["GET", "PUT"])
@permission_classes(_VIEW)
def mapping_matrix(request):
    """
    Dept → Equipment → Supported Software checkbox matrix.

    GET  ?department=<id>  → equipment rows + catalog columns + checked mapping ids
    PUT  body: { equipment_id, catalog_ids: [...] }  → replace mappings for one equipment
    """
    from iic_booking.equipment.models import Equipment

    if request.method == "PUT":
        if not CanManageRemoteAnalysis().has_permission(request, None):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        body = request.data or {}
        equipment_id = body.get("equipment_id") or body.get("equipment")
        catalog_ids = body.get("catalog_ids") or body.get("software_ids") or []
        if not equipment_id:
            return Response({"detail": "equipment_id required"}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(catalog_ids, list):
            return Response({"detail": "catalog_ids must be a list"}, status=status.HTTP_400_BAD_REQUEST)
        equipment = get_object_or_404(Equipment, pk=equipment_id)
        default_id = body.get("default_catalog_id")
        wanted = {str(x) for x in catalog_ids if x}
        existing = {
            str(m.catalog_id): m
            for m in EquipmentAnalysisSoftware.objects.filter(equipment=equipment).select_related("catalog")
        }
        # Remove unchecked
        for cid, row in list(existing.items()):
            if cid not in wanted:
                row.delete()
                existing.pop(cid, None)
        # Add new
        for cid in wanted:
            if cid in existing:
                continue
            cat = AnalysisSoftwareCatalog.objects.filter(pk=cid, is_archived=False).first()
            if not cat:
                continue
            existing[cid] = EquipmentAnalysisSoftware.objects.create(
                equipment=equipment,
                catalog=cat,
                is_default=bool(default_id and str(default_id) == cid),
            )
        if default_id and str(default_id) in existing:
            EquipmentAnalysisSoftware.objects.filter(equipment=equipment).update(is_default=False)
            row = existing[str(default_id)]
            row.is_default = True
            row.save(update_fields=["is_default", "updated_at"])
        from iic_booking.remote_analysis.constants import AuditCategory
        from iic_booking.remote_analysis.services.audit import record_event

        record_event(
            category=AuditCategory.INVENTORY,
            action="EquipmentSoftwareMappingReplaced",
            details=f"equipment={_equipment_pk(equipment)} catalogs={sorted(wanted)}",
            actor=request.user if getattr(request.user, "is_authenticated", False) else None,
        )
        return Response(
            {
                "equipment_id": _equipment_pk(equipment),
                "mappings": [
                    _serialize_mapping(m)
                    for m in EquipmentAnalysisSoftware.objects.filter(equipment=equipment)
                    .select_related("equipment", "equipment__internal_department", "catalog")
                    .order_by("sort_order", "catalog__name")
                ],
            }
        )

    dept = request.query_params.get("department") or request.query_params.get("department_id")
    eq_qs = Equipment.objects.filter(enable_remote_analysis=True).select_related("internal_department")
    if dept:
        eq_qs = eq_qs.filter(internal_department_id=dept)
    eq_qs = eq_qs.order_by("internal_department__name", "name")

    catalogs = list(
        AnalysisSoftwareCatalog.objects.filter(is_active=True, is_archived=False).order_by("name")
    )
    # R11: enrich catalog columns with live RAA availability for mapping UI cards.
    availability_by_name: dict[str, dict] = {}
    try:
        from iic_booking.equipment.remote_analysis_integration.software import SoftwareMappingService
        from iic_booking.remote_analysis.services.availability import AvailabilityEngine

        svc = SoftwareMappingService()
        engine = AvailabilityEngine()
        for c in catalogs:
            availability_by_name[str(c.id)] = svc._catalog_availability_stats(c.name, availability=engine)
    except Exception:
        availability_by_name = {}

    mappings = EquipmentAnalysisSoftware.objects.filter(equipment__in=eq_qs).select_related("catalog")
    by_eq: dict[int, set[str]] = {}
    defaults: dict[int, str] = {}
    for m in mappings:
        by_eq.setdefault(m.equipment_id, set()).add(str(m.catalog_id))
        if m.is_default:
            defaults[m.equipment_id] = str(m.catalog_id)

    equipment_rows = []
    for eq in eq_qs[:500]:
        eq_pk = _equipment_pk(eq)
        equipment_rows.append(
            {
                "id": eq_pk,
                "name": eq.name,
                "code": getattr(eq, "code", "") or "",
                "department_id": getattr(eq, "internal_department_id", None),
                "department_name": getattr(getattr(eq, "internal_department", None), "name", None),
                "catalog_ids": sorted(by_eq.get(eq_pk, set())),
                "default_catalog_id": defaults.get(eq_pk),
            }
        )

    return Response(
        {
            "catalogs": [
                {
                    "id": str(c.id),
                    "name": c.name,
                    "slug": c.slug,
                    "vendor": c.vendor,
                    "category": c.category,
                    "license_type": c.license_type,
                    **{
                        k: v
                        for k, v in (availability_by_name.get(str(c.id)) or {}).items()
                        if k != "last_inventory_update"
                    },
                    "last_inventory_update": (
                        (
                            (availability_by_name.get(str(c.id)) or {}).get("last_inventory_update").isoformat()
                            if hasattr(
                                (availability_by_name.get(str(c.id)) or {}).get("last_inventory_update"),
                                "isoformat",
                            )
                            else (availability_by_name.get(str(c.id)) or {}).get("last_inventory_update")
                        )
                        if (availability_by_name.get(str(c.id)) or {}).get("last_inventory_update")
                        else None
                    ),
                }
                for c in catalogs
            ],
            "equipment": equipment_rows,
            "inventory_summary": _safe_inventory_summary(),
        }
    )


def _safe_inventory_summary() -> dict:
    try:
        from iic_booking.remote_analysis.services.catalog_sync import inventory_discovery_summary

        return inventory_discovery_summary()
    except Exception:
        return {}


@api_view(["POST"])
@permission_classes(_MANAGE)
def catalog_sync_from_inventory(request):
    """
    R11 ops: backfill AnalysisSoftwareCatalog from present InstalledSoftware rows,
    optionally enqueue REFRESH_SOFTWARE so online agents re-push full inventory.
    """
    body = request.data if isinstance(request.data, dict) else {}
    refresh_agents = bool(body.get("refresh_agents") or body.get("refreshAgents"))
    limit = body.get("limit")
    try:
        limit_i = int(limit) if limit is not None else 50_000
    except (TypeError, ValueError):
        return Response({"detail": "limit must be an integer"}, status=status.HTTP_400_BAD_REQUEST)

    from iic_booking.remote_analysis.services.catalog_sync import (
        archive_infrastructure_catalog_entries,
        archive_unmanaged_auto_catalog_entries,
        backfill_catalog_from_installed,
        inventory_discovery_summary,
    )

    before = inventory_discovery_summary()
    backfill = backfill_catalog_from_installed(limit=limit_i)
    cleanup = {
        **archive_infrastructure_catalog_entries(),
        **archive_unmanaged_auto_catalog_entries(),
    }

    refresh = {"enqueued": 0, "workstation_ids": []}
    if refresh_agents:
        from iic_booking.remote_analysis.constants import CommandType
        from iic_booking.remote_analysis.models import AnalysisWorkstation
        from iic_booking.remote_analysis.services.commands import CommandService

        svc = CommandService()
        ids: list[str] = []
        for ws in AnalysisWorkstation.objects.filter(enabled=True).order_by("hostname")[:200]:
            try:
                cmd = svc.create_command(
                    ws,
                    CommandType.REFRESH_SOFTWARE,
                    created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                )
                ids.append(str(ws.id))
                refresh["enqueued"] += 1
                _ = cmd
            except Exception:
                continue
        refresh["workstation_ids"] = ids

    after = inventory_discovery_summary()
    from iic_booking.remote_analysis.constants import AuditCategory
    from iic_booking.remote_analysis.services.audit import record_event

    record_event(
        category=AuditCategory.INVENTORY,
        action="CatalogSyncFromInventory",
        details=f"backfill={backfill} refresh={refresh['enqueued']}",
        actor=request.user if getattr(request.user, "is_authenticated", False) else None,
    )
    return Response(
        {
            "accepted": True,
            "before": before,
            "backfill": backfill,
            "cleanup": cleanup,
            "after": after,
            "refresh_agents": refresh,
        }
    )
