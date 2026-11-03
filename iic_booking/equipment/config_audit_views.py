"""Main Administrator API for the equipment configuration audit trail (read-only)."""

from __future__ import annotations

from django.db.models import Q
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .config_audit_models import ConfigChangeAction, ConfigChangeSource, EquipmentConfigAuditLog

PAGE_SIZE_DEFAULT = 25
PAGE_SIZE_MAX = 100


def _is_main_admin(user) -> bool:
    from iic_booking.users.models import UserType

    return bool(user and user.is_authenticated and getattr(user, "user_type", None) == UserType.ADMIN)


def _denied():
    return Response(
        {"error": "Only the Main Administrator can view the equipment configuration audit trail."},
        status=status.HTTP_403_FORBIDDEN,
    )


def _row(log: EquipmentConfigAuditLog, *, detail: bool = False) -> dict:
    data = {
        "id": log.pk,
        "equipment_id": log.equipment_id,
        "equipment_code": log.equipment_code,
        "equipment_name": log.equipment_name,
        "object_type": log.object_type,
        "object_label": log.object_label,
        "action": log.action,
        "action_label": log.get_action_display(),
        "setting": log.setting,
        "setting_label": log.setting_label,
        "previous_value": log.previous_value,
        "new_value": log.new_value,
        "changed_by_id": log.changed_by_id,
        "changed_by": log.changed_by_label or ("System" if log.is_system else ""),
        "changed_by_role": log.changed_by_role,
        "changed_by_department": log.changed_by_department,
        "is_system": log.is_system,
        "source": log.source,
        "source_label": log.get_source_display(),
        "changed_at": log.changed_at.isoformat(),
    }
    if detail:
        data.update({"object_id": log.object_id, "process": log.process, "reference": log.reference})
    return data


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_config_audit_list(request):
    """Filters: equipment_id, user_id, setting, source, action, date_from, date_to (YYYY-MM-DD), q; page, page_size."""
    if not _is_main_admin(request.user):
        return _denied()
    p = request.query_params
    qs = EquipmentConfigAuditLog.objects.all()
    if (eid := _int(p.get("equipment_id"))) is not None:
        qs = qs.filter(equipment_id=eid)
    if (uid := _int(p.get("user_id"))) is not None:
        qs = qs.filter(changed_by_id=uid)
    if str(p.get("system") or "").lower() in ("1", "true", "yes"):
        qs = qs.filter(is_system=True)
    if setting := (p.get("setting") or "").strip():
        qs = qs.filter(Q(setting__iexact=setting) | Q(setting_label__iexact=setting))
    if (src := (p.get("source") or "").strip().upper()) in ConfigChangeSource.values:
        qs = qs.filter(source=src)
    if (act := (p.get("action") or "").strip().upper()) in ConfigChangeAction.values:
        qs = qs.filter(action=act)
    if d := parse_date(str(p.get("date_from") or "")):
        qs = qs.filter(changed_at__date__gte=d)
    if d := parse_date(str(p.get("date_to") or "")):
        qs = qs.filter(changed_at__date__lte=d)
    if q := (p.get("q") or "").strip():
        qs = qs.filter(
            Q(equipment_name__icontains=q)
            | Q(equipment_code__icontains=q)
            | Q(setting__icontains=q)
            | Q(setting_label__icontains=q)
            | Q(previous_value__icontains=q)
            | Q(new_value__icontains=q)
            | Q(changed_by_label__icontains=q)
            | Q(object_label__icontains=q)
        )
    page_size = min(max(_int(p.get("page_size")) or PAGE_SIZE_DEFAULT, 1), PAGE_SIZE_MAX)
    page = max(_int(p.get("page")) or 1, 1)
    total = qs.count()
    start = (page - 1) * page_size
    rows = [_row(log) for log in qs.order_by("-changed_at", "-id")[start : start + page_size]]
    return Response(
        {
            "count": total,
            "page": page,
            "page_size": page_size,
            "num_pages": max((total + page_size - 1) // page_size, 1),
            "results": rows,
            "sources": [{"value": v, "label": str(lbl)} for v, lbl in ConfigChangeSource.choices],
            "actions": [{"value": v, "label": str(lbl)} for v, lbl in ConfigChangeAction.choices],
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_config_audit_detail(request, pk: int):
    if not _is_main_admin(request.user):
        return _denied()
    log = EquipmentConfigAuditLog.objects.filter(pk=pk).first()
    if log is None:
        return Response({"error": "Audit record not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(_row(log, detail=True))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_config_audit_facets(request):
    """Choices for the filters: equipment and people that appear in the trail, and the settings changed."""
    if not _is_main_admin(request.user):
        return _denied()
    equipment = (
        EquipmentConfigAuditLog.objects.order_by("equipment_name")
        .values("equipment_id", "equipment_code", "equipment_name")
        .distinct()
    )
    users = (
        EquipmentConfigAuditLog.objects.filter(changed_by__isnull=False)
        .order_by("changed_by_label")
        .values("changed_by_id", "changed_by_label")
        .distinct()
    )
    settings_ = (
        EquipmentConfigAuditLog.objects.order_by("setting_label").values("setting", "setting_label").distinct()
    )
    return Response(
        {
            "equipment": [
                {"id": e["equipment_id"], "code": e["equipment_code"], "name": e["equipment_name"]} for e in equipment
            ],
            "users": [{"id": u["changed_by_id"], "name": u["changed_by_label"]} for u in users],
            "settings": [{"value": s["setting"], "label": s["setting_label"]} for s in settings_],
        }
    )
