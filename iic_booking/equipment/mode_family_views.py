"""Multi-mode equipment page API (OIC, Main Administrator and permitted Department Administrators)."""
from __future__ import annotations

from datetime import date, datetime, time as time_cls

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .mode_family_service import (
    FamilyChangeError,
    access_scope,
    can_manage_equipment,
    link_mode_for_schedule,
    manageable_equipment_qs,
    mode_candidates,
    remove_mode,
    set_family_modes,
    unlink_mode_if_unscheduled,
)
from .models import Equipment, EquipmentModeSchedule, ModeAvailability, ModeScheduleBehavior

DEFAULT_UNAVAILABLE_LABEL = "Mode not scheduled"
DEFAULT_EXCLUSIVE_LABEL = "Alternate mode active"
DEFAULT_GREY = "#9ca3af"

_FORBIDDEN = {"error": "Only the Main Administrator or an Officer In Charge can configure multi-mode equipment."}


def _serialize_schedule(sched: EquipmentModeSchedule) -> dict:
    return {
        "id": sched.id,
        "parent_equipment_id": sched.parent_equipment_id,
        "mode_equipment_id": sched.mode_equipment_id,
        "mode_equipment_code": getattr(sched.mode_equipment, "code", None),
        "mode_equipment_name": getattr(sched.mode_equipment, "name", None),
        "start_date": sched.start_date.isoformat() if sched.start_date else None,
        "end_date": sched.end_date.isoformat() if sched.end_date else None,
        "always": not sched.has_dates(),
        "start_time": sched.start_time.strftime("%H:%M") if sched.start_time else None,
        "end_time": sched.end_time.strftime("%H:%M") if sched.end_time else None,
        "weekdays": list(sched.weekdays or []),
        "behavior": sched.behavior,
        "behavior_display": sched.get_behavior_display(),
        "unavailable_label": sched.unavailable_label or DEFAULT_UNAVAILABLE_LABEL,
        "unavailable_color": sched.unavailable_color or DEFAULT_GREY,
        "exclusive_blocked_label": sched.exclusive_blocked_label or DEFAULT_EXCLUSIVE_LABEL,
        "exclusive_blocked_color": sched.exclusive_blocked_color or DEFAULT_GREY,
        "created_at": sched.created_at.isoformat() if sched.created_at else None,
        "updated_at": sched.updated_at.isoformat() if sched.updated_at else None,
    }


def _equipment_row(eq: Equipment) -> dict:
    row = {
        "equipment_id": eq.equipment_id,
        "code": eq.code,
        "name": eq.name,
        "status": eq.status,
        "department_id": eq.internal_department_id,
        "department_name": getattr(eq.internal_department, "name", None) if eq.internal_department_id else None,
    }
    if eq.parent_equipment_id:
        row["mode_availability"] = eq.mode_availability
    return row


def _schedule_sort_key(s: EquipmentModeSchedule):
    # Newest first, with schedules that have no dates (always) at the top.
    return (s.start_date is None, s.start_date or date.min, s.end_date or date.max, s.id)


def _serialize_family(base: Equipment) -> dict:
    modes = sorted(base.mode_children.all(), key=lambda c: (c.code or "", c.name or ""))
    schedules = sorted(base.mode_schedules.all(), key=_schedule_sort_key, reverse=True)
    today = timezone.localdate()
    current = {}
    for s in schedules:
        if s.end_date is None or s.end_date >= today:
            current[s.mode_equipment_id] = current.get(s.mode_equipment_id, 0) + 1
    children = []
    for c in modes:
        row = _equipment_row(c)
        row["current_schedule_count"] = current.get(c.equipment_id, 0)
        children.append(row)
    return {
        "parent_equipment_id": base.equipment_id,
        "parent_code": base.code,
        "parent_name": base.name,
        "parent_status": base.status,
        "department_id": base.internal_department_id,
        "department_name": getattr(base.internal_department, "name", None) if base.internal_department_id else None,
        "children": children,
        "schedules": [_serialize_schedule(s) for s in schedules],
    }


def _parse_department_filter(request):
    raw = (request.query_params.get("department_id") or "").strip()
    if not raw or raw.lower() in ("all", "none", "null"):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oic_multi_mode_list(request):
    """
    Families (base instrument + modes + schedules) the user may manage. Main Administrator sees all
    equipment and can filter by ``department_id``; an OIC sees only equipment they are OIC of.
    """
    scope = access_scope(request.user)
    if scope is None:
        return Response(_FORBIDDEN, status=status.HTTP_403_FORBIDDEN)

    managed = manageable_equipment_qs(request.user).select_related("internal_department")
    departments = {}
    for eq in managed.filter(parent_equipment__isnull=True).exclude(internal_department__isnull=True):
        departments[eq.internal_department_id] = eq.internal_department.name
    department_id = _parse_department_filter(request)
    if department_id is not None:
        managed = managed.filter(internal_department_id=department_id)

    bases = (
        managed.filter(parent_equipment__isnull=True)
        .filter(Q(mode_children__isnull=False) | Q(enable_multi_mode=True))
        .distinct()
        .prefetch_related(
            "mode_children",
            "mode_children__internal_department",
            "mode_schedules",
            "mode_schedules__mode_equipment",
        )
        .order_by("code", "name")
    )
    base_candidates = [
        _equipment_row(eq) for eq in managed.filter(parent_equipment__isnull=True).order_by("code", "name")
    ]
    linkable = list(
        managed.filter(parent_equipment__isnull=True, enable_multi_mode=False, mode_children__isnull=True)
        .order_by("code")
        .values("equipment_id", "code", "name")
    )

    return Response(
        {
            "scope": scope,
            "multi_mode_enabled": True,
            "department_id": department_id,
            "departments": [
                {"id": did, "name": name} for did, name in sorted(departments.items(), key=lambda kv: kv[1] or "")
            ],
            "families": [_serialize_family(b) for b in bases],
            "base_candidates": base_candidates,
            "linkable_equipment": linkable,
            "behaviors": [
                {"value": ModeScheduleBehavior.PARALLEL, "label": "Yes, at the same time (parallel)"},
                {"value": ModeScheduleBehavior.EXCLUSIVE, "label": "No, only this mode (exclusive)"},
            ],
            "availability_choices": [{"value": v, "label": label} for v, label in ModeAvailability.choices],
        }
    )


def _family_detail(base: Equipment, user) -> dict:
    base = (
        Equipment.objects.select_related("internal_department")
        .prefetch_related("mode_children", "mode_schedules", "mode_schedules__mode_equipment")
        .get(pk=base.pk)
    )
    candidates = []
    for eq in mode_candidates(base, user):
        candidates.append(
            {
                "equipment_id": eq.equipment_id,
                "code": eq.code,
                "name": eq.name,
                "is_mode": eq.parent_equipment_id == base.equipment_id,
                "mode_availability": eq.mode_availability,
            }
        )
    managed_ids = set(manageable_equipment_qs(user).values_list("equipment_id", flat=True))
    family = _serialize_family(base)
    for child in family["children"]:
        child["can_manage"] = child["equipment_id"] in managed_ids
        if not any(c["equipment_id"] == child["equipment_id"] for c in candidates):
            candidates.append(
                {
                    "equipment_id": child["equipment_id"],
                    "code": child["code"],
                    "name": child["name"],
                    "is_mode": True,
                    "mode_availability": child.get("mode_availability"),
                    "locked": True,
                }
            )
    return {"family": family, "candidates": candidates}


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def oic_multi_mode_family(request, base_id: int):
    """
    GET: the family of ``base_id`` and which equipment can be ticked as its modes.
    PUT: replace its modes. Body: {"modes": [{"equipment_id": 12, "mode_availability": "ALWAYS"}]}.
    Removing a mode with upcoming bookings or current/future schedules is refused (409).
    """
    if access_scope(request.user) is None:
        return Response(_FORBIDDEN, status=status.HTTP_403_FORBIDDEN)
    try:
        base = Equipment.objects.get(pk=base_id)
    except Equipment.DoesNotExist:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    if not can_manage_equipment(request.user, base.equipment_id):
        return Response({"error": "You can only set up modes for equipment you manage."}, status=status.HTTP_403_FORBIDDEN)
    if base.parent_equipment_id:
        return Response(
            {"error": f"{base.code} is a mode of another instrument and cannot be a base."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if request.method == "PUT":
        data = request.data or {}
        raw = data.get("modes", data.get("mode_equipment_ids"))
        if raw is None:
            return Response({"error": "modes is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = set_family_modes(base, raw, request.user)
        except FamilyChangeError as exc:
            body = {"error": exc.message}
            body.update(exc.details)
            return Response(body, status=exc.status_code)
        payload = _family_detail(base, request.user)
        payload["changes"] = {
            "added": result.added,
            "removed": result.removed,
            "availability_changed": result.availability_changed,
        }
        return Response(payload)

    return Response(_family_detail(base, request.user))


def _parse_optional_time(raw):
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    if not s:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    try:
        parts = s.split(":")
        return time_cls(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (TypeError, ValueError, IndexError):
        return None


def _apply_schedule_fields(sched: EquipmentModeSchedule, data) -> None:
    if "unavailable_label" in data:
        sched.unavailable_label = (str(data.get("unavailable_label") or "").strip() or DEFAULT_UNAVAILABLE_LABEL)[:120]
    if "unavailable_color" in data:
        sched.unavailable_color = (str(data.get("unavailable_color") or "").strip() or DEFAULT_GREY)[:20]
    if "exclusive_blocked_label" in data:
        sched.exclusive_blocked_label = (
            str(data.get("exclusive_blocked_label") or "").strip() or DEFAULT_EXCLUSIVE_LABEL
        )[:120]
    if "exclusive_blocked_color" in data:
        sched.exclusive_blocked_color = (str(data.get("exclusive_blocked_color") or "").strip() or DEFAULT_GREY)[:20]
    if "start_time" in data:
        sched.start_time = _parse_optional_time(data.get("start_time"))
    if "end_time" in data:
        sched.end_time = _parse_optional_time(data.get("end_time"))
    if "weekdays" in data:
        sched.weekdays = EquipmentModeSchedule.normalize_weekdays(data.get("weekdays"))


def _parse_behavior(raw):
    behavior = str(raw or ModeScheduleBehavior.PARALLEL).strip().upper()
    if behavior not in (ModeScheduleBehavior.PARALLEL, ModeScheduleBehavior.EXCLUSIVE):
        raise ValueError("behavior must be PARALLEL or EXCLUSIVE.")
    return behavior


def _validation_error_response(exc: DjangoValidationError) -> Response:
    detail = exc.message_dict if hasattr(exc, "message_dict") else {"error": exc.messages}
    first = next((msgs[0] for msgs in detail.values() if msgs), "Invalid schedule.")
    return Response({"error": first, "errors": detail}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def oic_multi_mode_schedule_create(request):
    """
    Create a mode schedule. Body: parent_equipment_id, mode_equipment_id, optional start_date/end_date
    (both blank = always available), behavior, optional weekdays (0=Mon..6=Sun), start_time/end_time and
    label/colour overrides. Eligible equipment that is not yet a mode of the base becomes one.
    """
    if access_scope(request.user) is None:
        return Response(_FORBIDDEN, status=status.HTTP_403_FORBIDDEN)

    data = request.data or {}
    try:
        parent_id = int(data.get("parent_equipment_id"))
        mode_id = int(data.get("mode_equipment_id"))
    except (TypeError, ValueError):
        return Response(
            {"error": "parent_equipment_id and mode_equipment_id are required integers."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not can_manage_equipment(request.user, parent_id):
        return Response({"error": "Permission denied for parent equipment."}, status=status.HTTP_403_FORBIDDEN)
    if not can_manage_equipment(request.user, mode_id):
        return Response({"error": "Permission denied for mode equipment."}, status=status.HTTP_403_FORBIDDEN)

    try:
        parent = Equipment.objects.get(pk=parent_id)
        mode = Equipment.objects.get(pk=mode_id)
    except Equipment.DoesNotExist:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        start_d = _parse_optional_date(data.get("start_date"))
        end_d = _parse_optional_date(data.get("end_date"))
    except ValueError:
        return Response({"error": "start_date and end_date must be YYYY-MM-DD or blank."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        behavior = _parse_behavior(data.get("behavior"))
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    try:
        with transaction.atomic():
            linked = link_mode_for_schedule(parent, mode, request.user)
            sched = EquipmentModeSchedule(
                parent_equipment=parent,
                mode_equipment=mode,
                start_date=start_d,
                end_date=end_d,
                behavior=behavior,
                created_by=request.user,
                unavailable_label=DEFAULT_UNAVAILABLE_LABEL,
                unavailable_color=DEFAULT_GREY,
                exclusive_blocked_label=DEFAULT_EXCLUSIVE_LABEL,
                exclusive_blocked_color=DEFAULT_GREY,
            )
            _apply_schedule_fields(sched, data)
            sched.full_clean()
            sched.save()
    except FamilyChangeError as exc:
        return Response({"error": exc.message, **exc.details}, status=exc.status_code)
    except (TypeError, ValueError):
        return Response({"error": "Repeat days must be weekdays 0 (Monday) to 6 (Sunday)."}, status=status.HTTP_400_BAD_REQUEST)
    except DjangoValidationError as exc:
        return _validation_error_response(exc)
    return Response({"schedule": _serialize_schedule(sched), "mode_linked": linked}, status=status.HTTP_201_CREATED)


def _parse_optional_date(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    return date.fromisoformat(s[:10]) if s else None


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def oic_multi_mode_schedule_detail(request, schedule_id: int):
    """
    Update or delete a mode schedule. Blank start_date and end_date mean always available. Changing the
    mode to eligible equipment links it; a mode left without current or future schedules (and with no
    upcoming bookings) is unlinked.
    """
    if access_scope(request.user) is None:
        return Response(_FORBIDDEN, status=status.HTTP_403_FORBIDDEN)
    try:
        sched = EquipmentModeSchedule.objects.select_related("parent_equipment", "mode_equipment").get(pk=schedule_id)
    except EquipmentModeSchedule.DoesNotExist:
        return Response({"error": "Schedule not found."}, status=status.HTTP_404_NOT_FOUND)
    if not can_manage_equipment(request.user, sched.parent_equipment_id):
        return Response({"error": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)

    parent_id = sched.parent_equipment_id
    old_mode_id = sched.mode_equipment_id
    if request.method == "DELETE":
        with transaction.atomic():
            sched.delete()
            unlinked = unlink_mode_if_unscheduled(parent_id, old_mode_id, request.user)
        return Response({"message": "Schedule deleted.", "mode_unlinked": unlinked})

    data = request.data or {}
    for key in ("start_date", "end_date"):
        if key in data:
            try:
                setattr(sched, key, _parse_optional_date(data.get(key)))
            except ValueError:
                return Response({"error": f"Invalid {key}."}, status=status.HTTP_400_BAD_REQUEST)
    if "behavior" in data:
        try:
            sched.behavior = _parse_behavior(data.get("behavior"))
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    new_mode = None
    if "mode_equipment_id" in data:
        try:
            mode_id = int(data.get("mode_equipment_id"))
        except (TypeError, ValueError):
            return Response({"error": "Invalid mode_equipment_id."}, status=status.HTTP_400_BAD_REQUEST)
        if not can_manage_equipment(request.user, mode_id):
            return Response({"error": "Permission denied for mode equipment."}, status=status.HTTP_403_FORBIDDEN)
        try:
            new_mode = Equipment.objects.get(pk=mode_id)
        except Equipment.DoesNotExist:
            return Response({"error": "Mode equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    linked = unlinked = False
    try:
        with transaction.atomic():
            if new_mode is not None and new_mode.pk != old_mode_id:
                linked = link_mode_for_schedule(sched.parent_equipment, new_mode, request.user)
                sched.mode_equipment = new_mode
            _apply_schedule_fields(sched, data)
            sched.full_clean()
            sched.save()
            if sched.mode_equipment_id != old_mode_id:
                unlinked = unlink_mode_if_unscheduled(parent_id, old_mode_id, request.user)
    except FamilyChangeError as exc:
        return Response({"error": exc.message, **exc.details}, status=exc.status_code)
    except (TypeError, ValueError):
        return Response({"error": "Repeat days must be weekdays 0 (Monday) to 6 (Sunday)."}, status=status.HTTP_400_BAD_REQUEST)
    except DjangoValidationError as exc:
        return _validation_error_response(exc)
    return Response({"schedule": _serialize_schedule(sched), "mode_linked": linked, "mode_unlinked": unlinked})


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def oic_multi_mode_remove_mode(request, base_id: int, mode_id: int):
    """Stop ``mode_id`` being a mode of ``base_id``: deletes its current/future schedules. Refused (409) with upcoming bookings."""
    if access_scope(request.user) is None:
        return Response(_FORBIDDEN, status=status.HTTP_403_FORBIDDEN)
    base = Equipment.objects.filter(pk=base_id).first()
    mode = Equipment.objects.filter(pk=mode_id).first()
    if base is None or mode is None:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    try:
        deleted = remove_mode(base, mode, request.user)
    except FamilyChangeError as exc:
        return Response({"error": exc.message, **exc.details}, status=exc.status_code)
    payload = _family_detail(base, request.user)
    payload["schedules_deleted"] = deleted
    return Response(payload)
