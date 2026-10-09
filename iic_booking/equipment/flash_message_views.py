"""Equipment flash messages API (OIC incl. active temporary OIC: their equipment; Department Administrator:
their department; Main Administrator: everything). Viewers read live messages from the equipment detail
payload (``flash_messages``).

GET    /api/equipments/flash-messages/                 list (filters: equipment, status, department, search)
POST   /api/equipments/flash-messages/                 create
GET    /api/equipments/flash-messages/<id>/            detail with audit history
PATCH  /api/equipments/flash-messages/<id>/            edit
POST   /api/equipments/flash-messages/<id>/end/        end now
POST   /api/equipments/flash-messages/<id>/extend/     ``days`` (1-30) or ``end_at``
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Case
from django.db.models import IntegerField
from django.db.models import Q
from django.db.models import Value
from django.db.models import When
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.decorators import parser_classes
from rest_framework.decorators import permission_classes
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

from .flash_message_service import LINK_LABEL_MAX_CHARS
from .flash_message_service import MAX_DURATION_DAYS
from .flash_message_service import MAX_START_AHEAD_DAYS
from .flash_message_service import MESSAGE_MAX_CHARS
from .flash_message_service import actor_role
from .flash_message_service import audience_user_type_choices
from .flash_message_service import clean_flash_message
from .flash_message_service import clean_link
from .flash_message_service import clean_user_types
from .flash_message_service import flash_equipment_ids
from .flash_message_service import message_status
from .flash_message_service import parse_when
from .flash_message_service import schedule_error
from .rich_text import rich_text_to_plain

STATUS_LABELS = {"LIVE": "Live", "SCHEDULED": "Scheduled", "EXPIRED": "Expired", "OFF": "Off"}
AUDITED_FIELDS = (
    "message", "tone", "start_at", "end_at", "is_active", "audience", "audience_user_types", "show_on_modes",
    "link_url", "link_label",
)


def _forbidden():
    return Response(
        {"error": "Only Officers In-charge, Department Administrators and the Main Administrator can manage flash messages."},
        status=status.HTTP_403_FORBIDDEN,
    )


def _scoped(user):
    from .models import EquipmentFlashMessage

    ids = flash_equipment_ids(user)
    qs = EquipmentFlashMessage.objects.select_related(
        "equipment", "equipment__internal_department", "created_by", "updated_by"
    )
    if ids is not None:
        qs = qs.filter(equipment_id__in=ids)
    return qs


def _status_q(state: str, now) -> Q | None:
    return {
        "off": Q(is_active=False),
        "expired": Q(is_active=True, end_at__lte=now),
        "scheduled": Q(is_active=True, start_at__gt=now, end_at__gt=now),
        "live": Q(is_active=True, start_at__lte=now, end_at__gt=now),
    }.get(state)


def _int_list(raw) -> list[int]:
    return [int(p) for p in str(raw or "").split(",") if p.strip().isdigit()]


def _name(user) -> str:
    if user is None:
        return ""
    from iic_booking.users.display import get_user_display_name

    return get_user_display_name(user, fallback_to_email=False) or "Staff"


def serialize_message(msg, now=None) -> dict:
    from .models import FlashAudience, FlashTone

    now = now or timezone.now()
    eq = msg.equipment
    dept = getattr(eq, "internal_department", None)
    state = message_status(msg, now)
    return {
        "id": msg.pk,
        "equipment_id": msg.equipment_id,
        "equipment_name": getattr(eq, "name", "") or "",
        "equipment_code": getattr(eq, "code", "") or "",
        "equipment_has_modes": bool(getattr(eq, "enable_multi_mode", False)),
        "department_name": getattr(dept, "name", "") or "",
        "message": msg.message,
        "message_plain": rich_text_to_plain(msg.message),
        "tone": msg.tone,
        "tone_display": str(dict(FlashTone.choices).get(msg.tone, msg.tone)),
        "start_at": msg.start_at,
        "end_at": msg.end_at,
        "is_active": msg.is_active,
        "status": state,
        "status_display": STATUS_LABELS[state],
        "audience": msg.audience,
        "audience_display": str(dict(FlashAudience.choices).get(msg.audience, msg.audience)),
        "audience_user_types": list(msg.audience_user_types or []),
        "show_on_modes": msg.show_on_modes,
        "link_url": msg.link_url,
        "link_label": msg.link_label,
        "created_by_name": _name(msg.created_by),
        "updated_by_name": _name(msg.updated_by),
        "created_at": msg.created_at,
        "updated_at": msg.updated_at,
    }


def _options(user) -> dict:
    from .models import Equipment, FlashAudience, FlashTone

    ids = flash_equipment_ids(user)
    eq_qs = Equipment.objects.all() if ids is None else Equipment.objects.filter(equipment_id__in=ids)
    rows = eq_qs.order_by("name").values(
        "equipment_id", "name", "code", "internal_department_id", "internal_department__name", "enable_multi_mode",
        "parent_equipment_id",
    )
    return {
        "equipment_options": [
            {
                "id": r["equipment_id"],
                "name": r["name"] or "",
                "code": r["code"] or "",
                "department_id": r["internal_department_id"],
                "department_name": r["internal_department__name"] or "",
                "has_modes": bool(r["enable_multi_mode"]),
                "is_mode": bool(r["parent_equipment_id"]),
            }
            for r in rows
        ],
        "tones": [{"value": k, "label": str(v)} for k, v in FlashTone.choices],
        "audiences": [{"value": k, "label": str(v)} for k, v in FlashAudience.choices],
        "user_types": [{"value": k, "label": v} for k, v in audience_user_type_choices()],
        "limits": {
            "message_max_chars": MESSAGE_MAX_CHARS,
            "max_duration_days": MAX_DURATION_DAYS,
            "max_start_ahead_days": MAX_START_AHEAD_DAYS,
            "link_label_max_chars": LINK_LABEL_MAX_CHARS,
        },
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
def flash_message_list(request):
    try:
        qs = _scoped(request.user)
    except PermissionError:
        return _forbidden()
    if request.method == "POST":
        return _create(request)

    params = request.query_params
    now = timezone.now()
    equipment_ids = _int_list(params.get("equipment"))
    if equipment_ids:
        qs = qs.filter(equipment_id__in=equipment_ids)
    department_ids = _int_list(params.get("department"))
    if department_ids:
        qs = qs.filter(equipment__internal_department_id__in=department_ids)
    search = str(params.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(equipment__name__icontains=search) | Q(equipment__code__icontains=search) | Q(message__icontains=search)
        )
    base = qs
    summary = {
        key: base.filter(_status_q(key, now)).count() for key in ("live", "scheduled", "expired", "off")
    }
    state_q = _status_q(str(params.get("status") or "").strip().lower(), now)
    if state_q is not None:
        qs = qs.filter(state_q)
    qs = qs.annotate(
        _rank=Case(
            When(_status_q("live", now), then=Value(0)),
            When(_status_q("scheduled", now), then=Value(1)),
            When(_status_q("off", now), then=Value(2)),
            default=Value(3),
            output_field=IntegerField(),
        )
    ).order_by("_rank", "-start_at", "-id")
    total = qs.count()
    try:
        page_size = max(1, min(200, int(params.get("page_size") or 25)))
        page = max(1, int(params.get("page") or 1))
    except ValueError:
        page_size, page = 25, 1
    rows = [serialize_message(m, now) for m in qs[(page - 1) * page_size : page * page_size]]
    payload = {"count": total, "page": page, "page_size": page_size, "results": rows, "summary": summary}
    if str(params.get("with_options") or "").lower() in ("1", "true", "yes"):
        payload.update(_options(request.user))
        payload["can_filter_department"] = getattr(request.user, "user_type", None) == UserType.ADMIN
    return Response(payload)


def _validate(data, *, instance=None, equipment=None, now=None):
    """Return (fields, error). ``fields`` holds only the keys sent (all of them when creating)."""
    from .models import FlashAudience, FlashTone

    now = now or timezone.now()
    creating = instance is None
    out: dict = {}

    if creating or "message" in data:
        html, error = clean_flash_message(data.get("message"))
        if error:
            return None, error
        out["message"] = html
    if creating or "tone" in data:
        tone = str(data.get("tone") or FlashTone.INFO).strip().upper()
        if tone not in FlashTone.values:
            return None, "Choose a tone: Info, Notice, Important or Success."
        out["tone"] = tone
    if creating or "start_at" in data or "end_at" in data:
        start = parse_when(data.get("start_at")) if "start_at" in data else (instance.start_at if instance else None)
        end = parse_when(data.get("end_at")) if "end_at" in data else (instance.end_at if instance else None)
        if start is False or end is False:
            return None, "Enter the start and end as a date and time."
        start = start or (instance.start_at if instance and "start_at" not in data else now)
        if end is None:
            return None, "Choose when the message should end."
        error = schedule_error(start, end, now, creating=creating or (end != instance.end_at))
        if error:
            return None, error
        out["start_at"], out["end_at"] = start, end
    if creating or "is_active" in data:
        out["is_active"] = bool(data.get("is_active", True))
    if creating or "audience" in data or "audience_user_types" in data:
        audience = str(data.get("audience") or (instance.audience if instance else FlashAudience.ALL)).strip().upper()
        if audience not in FlashAudience.values:
            return None, "Choose who should see the message."
        types = clean_user_types(data.get("audience_user_types")) if audience == FlashAudience.USER_TYPES else []
        if audience == FlashAudience.USER_TYPES and not types:
            return None, "Choose at least one user type."
        out["audience"], out["audience_user_types"] = audience, types
    if creating or "show_on_modes" in data:
        eq = equipment or instance.equipment
        out["show_on_modes"] = bool(data.get("show_on_modes")) and bool(getattr(eq, "enable_multi_mode", False))
    if creating or "link_url" in data or "link_label" in data:
        link, error = clean_link(
            data.get("link_url", instance.link_url if instance else ""),
            data.get("link_label", instance.link_label if instance else ""),
        )
        if error:
            return None, error
        out["link_url"], out["link_label"] = link
    return out, None


def _audit_value(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, str):
        return value[:400]
    return value


def _audit(msg, action, user, changes=None):
    from .dept_admin_actions import record_staff_action
    from .models import EquipmentFlashMessageAudit

    EquipmentFlashMessageAudit.objects.create(
        flash_message=msg,
        equipment_id_snapshot=msg.equipment_id,
        action=action,
        actor=user,
        actor_role=actor_role(user, msg.equipment_id),
        changes=changes or {},
    )
    record_staff_action(
        user, f"flash_message_{action}", equipment_id=msg.equipment_id, flash_message_id=msg.pk,
        fields=sorted((changes or {}).keys()),
    )


def _create(request):
    from .models import Equipment, EquipmentFlashMessage

    data = request.data or {}
    raw = str(data.get("equipment") or "").strip()
    equipment = Equipment.objects.filter(pk=int(raw)).first() if raw.isdigit() else None
    if equipment is None:
        return Response({"error": "Choose the equipment."}, status=status.HTTP_400_BAD_REQUEST)
    ids = flash_equipment_ids(request.user)
    if ids is not None and equipment.pk not in ids:
        return Response(
            {"error": "You can add flash messages only for equipment you manage."}, status=status.HTTP_403_FORBIDDEN
        )
    fields, error = _validate(data, equipment=equipment)
    if error:
        return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        msg = EquipmentFlashMessage.objects.create(
            equipment=equipment, created_by=request.user, updated_by=request.user, **fields
        )
        changes = {k: [None, _audit_value(v)] for k, v in fields.items()}
        source = str(data.get("duplicated_from") or "").strip()
        if source.isdigit():
            changes["duplicated_from"] = [None, int(source)]
        _audit(msg, "created", request.user, changes)
    msg = _scoped(request.user).get(pk=msg.pk)
    return Response(serialize_message(msg), status=status.HTTP_201_CREATED)


def _get(request, pk):
    from .models import EquipmentFlashMessage

    try:
        msg = _scoped(request.user).filter(pk=pk).first()
    except PermissionError:
        return None, _forbidden()
    if msg is not None:
        return msg, None
    if EquipmentFlashMessage.objects.filter(pk=pk).exists():
        return None, Response(
            {"error": "You can manage flash messages only for equipment you manage."}, status=status.HTTP_403_FORBIDDEN
        )
    return None, Response({"error": "Flash message not found."}, status=status.HTTP_404_NOT_FOUND)


def _detail(msg) -> dict:
    data = serialize_message(msg)
    data["history"] = [
        {
            "action": a.action,
            "actor_name": _name(a.actor),
            "actor_role": a.actor_role,
            "changes": a.changes,
            "at": a.at,
        }
        for a in msg.audit_entries.select_related("actor")
    ]
    return data


def _apply(msg, fields, user, action):
    changes = {}
    for key, value in fields.items():
        old = getattr(msg, key)
        if old != value:
            changes[key] = [_audit_value(old), _audit_value(value)]
            setattr(msg, key, value)
    if not changes:
        return False
    with transaction.atomic():
        msg.updated_by = user
        msg.save()
        _audit(msg, action, user, changes)
    return True


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
def flash_message_detail(request, pk: int):
    msg, error = _get(request, pk)
    if error:
        return error
    if request.method == "PATCH":
        fields, message = _validate(request.data or {}, instance=msg)
        if message:
            return Response({"error": message}, status=status.HTTP_400_BAD_REQUEST)
        _apply(msg, fields, request.user, "updated")
    return Response(_detail(msg))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
def flash_message_end(request, pk: int):
    msg, error = _get(request, pk)
    if error:
        return error
    now = timezone.now()
    if msg.end_at <= now:
        return Response({"error": "This message has already ended."}, status=status.HTTP_400_BAD_REQUEST)
    _apply(msg, {"start_at": min(msg.start_at, now), "end_at": now}, request.user, "ended")
    return Response(_detail(msg))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
def flash_message_extend(request, pk: int):
    msg, error = _get(request, pk)
    if error:
        return error
    data = request.data or {}
    now = timezone.now()
    if data.get("end_at"):
        end = parse_when(data.get("end_at"))
        if not end:
            return Response({"error": "Enter the new end as a date and time."}, status=status.HTTP_400_BAD_REQUEST)
    else:
        try:
            days = int(data.get("days") or 0)
        except (TypeError, ValueError):
            days = 0
        if not 1 <= days <= MAX_DURATION_DAYS:
            return Response(
                {"error": f"Extend by 1 to {MAX_DURATION_DAYS} days."}, status=status.HTTP_400_BAD_REQUEST
            )
        end = max(msg.end_at, now) + timedelta(days=days)
    start = msg.start_at
    if msg.end_at <= now and start < now:
        start = now
    message = schedule_error(start, end, now, creating=True)
    if message:
        return Response({"error": message}, status=status.HTTP_400_BAD_REQUEST)
    _apply(msg, {"start_at": start, "end_at": end}, request.user, "extended")
    return Response(_detail(msg))
