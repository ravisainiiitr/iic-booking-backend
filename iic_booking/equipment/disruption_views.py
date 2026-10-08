"""Disruption history API (OIC incl. substitute: their equipment; Department Administrator: their department;
Main Administrator: everything).

GET    /api/equipments/disruptions/                      list + summary cards (filters below)
GET    /api/equipments/disruptions/attention/            open count / reason-missing count for the dashboard
GET    /api/equipments/disruptions/<id>/                 detail with slots, timeline and service reports
PATCH  /api/equipments/disruptions/<id>/                 reason, reason_category, action_taken
POST   /api/equipments/disruptions/<id>/delete/          soft delete with optional ``reason`` (same scope as view)
POST   /api/equipments/disruptions/<id>/restore/         Main Administrator; list deleted with ``show_deleted=1``
POST   /api/equipments/disruptions/<id>/service-report/  multipart ``file`` (PDF, image, Word; 20 MB)
GET    /api/equipments/disruptions/<id>/service-report/<report_id>/   authenticated download
GET    /api/equipments/slot-status-changes/              general slot status change log
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.decorators import parser_classes
from rest_framework.decorators import permission_classes
from rest_framework.parsers import FormParser
from rest_framework.parsers import JSONParser
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

from .disruption_service import REASON_CATEGORIES
from .disruption_service import _log_edit
from .disruption_service import active_events_q
from .disruption_service import clean_reason_category
from .disruption_service import clean_text
from .disruption_service import event_duration_hours
from .disruption_service import event_is_open
from .disruption_service import reason_category_label

SERVICE_REPORT_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
SERVICE_REPORT_MAGIC = {
    "application/pdf": (b"%PDF-",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
    "application/msword": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04",),
}
ORDERING = {
    "start_at": "start_at",
    "end_at": "end_at",
    "equipment": "equipment__name",
    "type": "disruption_type",
    "bookings_affected": "bookings_affected",
    "slots_affected": "slots_affected",
    "started_at": "started_at",
}


def service_report_max_bytes() -> int:
    return int(getattr(settings, "DISRUPTION_REPORT_MAX_MB", 20)) * 1024 * 1024


def disruption_equipment_ids(user):
    """None = every equipment; list = allowed ids; raises PermissionError for other roles."""
    from .models import Equipment
    from .reports import get_equipment_ids_managed_by_oic

    ut = getattr(user, "user_type", None)
    if ut == UserType.ADMIN:
        return None
    if ut == UserType.MANAGER:
        return list(get_equipment_ids_managed_by_oic(user.id))
    if ut == UserType.DEPT_ADMIN:
        from iic_booking.users.rbac import get_user_department_scope_id

        dept_id = get_user_department_scope_id(user)
        if not dept_id:
            return []
        return list(Equipment.objects.filter(internal_department_id=dept_id).values_list("equipment_id", flat=True))
    raise PermissionError


def _forbidden():
    return Response(
        {"error": "Only the Main Administrator, Department Administrators and Officers In-charge can view disruptions."},
        status=status.HTTP_403_FORBIDDEN,
    )


def _scoped_events(user, *, deleted: bool = False):
    from .models import DisruptionEvent

    ids = disruption_equipment_ids(user)
    qs = DisruptionEvent.objects.select_related(
        "equipment", "equipment__internal_department", "started_by", "ended_by"
    ).filter(is_deleted=deleted)
    if deleted:
        qs = qs.select_related("deleted_by")
    if ids is not None:
        qs = qs.filter(equipment_id__in=ids)
    return qs


def _parse_date(raw):
    try:
        return datetime.strptime(str(raw).strip()[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _day_start(d):
    return timezone.make_aware(datetime.combine(d, time.min))


def _int_list(raw) -> list[int]:
    out = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _truthy(raw) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def apply_filters(qs, params, now=None):
    now = now or timezone.now()
    date_from = _parse_date(params.get("date_from"))
    date_to = _parse_date(params.get("date_to"))
    if date_from:
        qs = qs.filter(Q(end_at__isnull=True) | Q(end_at__gte=_day_start(date_from)))
    if date_to:
        qs = qs.filter(start_at__lt=_day_start(date_to + timedelta(days=1)))
    equipment_ids = _int_list(",".join(params.getlist("equipment")) if hasattr(params, "getlist") else params.get("equipment"))
    if equipment_ids:
        qs = qs.filter(equipment_id__in=equipment_ids)
    department_ids = _int_list(params.get("department"))
    if department_ids:
        qs = qs.filter(equipment__internal_department_id__in=department_ids)
    types = [t.strip().upper() for t in str(params.get("type") or "").split(",") if t.strip()]
    if types:
        qs = qs.filter(disruption_type__in=types)
    state = str(params.get("status") or "").strip().lower()
    if state == "open":
        qs = qs.filter(active_events_q(now))
    elif state == "closed":
        qs = qs.exclude(active_events_q(now))
    if _truthy(params.get("reason_missing")):
        qs = qs.filter(reason="", reason_category="")
    if _truthy(params.get("action_missing")):
        qs = qs.filter(action_taken="")
    source = str(params.get("source") or "").strip().upper()
    if source:
        qs = qs.filter(source=source)
    scope = str(params.get("scope") or "").strip().upper()
    if scope:
        qs = qs.filter(scope=scope)
    search = str(params.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(equipment__name__icontains=search)
            | Q(equipment__code__icontains=search)
            | Q(reason__icontains=search)
            | Q(action_taken__icontains=search)
            | Q(started_by__name__icontains=search)
            | Q(ended_by__name__icontains=search)
        )
    return qs


def _user_name(user) -> str:
    if user is None:
        return ""
    from iic_booking.users.display import get_user_display_name

    return get_user_display_name(user, fallback_to_email=False) or "Staff"


def serialize_event(event, *, now=None, links=None, reports=None) -> dict:
    from .models import DisruptionScope, DisruptionSource, DisruptionType

    now = now or timezone.now()
    eq = event.equipment
    dept = getattr(eq, "internal_department", None)
    hours = event_duration_hours(event, links=links, now=now)
    report_rows = reports if reports is not None else []
    row = {
        "id": event.pk,
        "equipment_id": event.equipment_id,
        "equipment_name": getattr(eq, "name", "") or "",
        "equipment_code": getattr(eq, "code", "") or "",
        "department_id": getattr(eq, "internal_department_id", None),
        "department_name": getattr(dept, "name", "") or "",
        "disruption_type": event.disruption_type,
        "disruption_type_display": str(dict(DisruptionType.choices).get(event.disruption_type, event.disruption_type)),
        "scope": event.scope,
        "scope_display": str(dict(DisruptionScope.choices).get(event.scope, event.scope)),
        "source": event.source,
        "source_display": str(dict(DisruptionSource.choices).get(event.source, event.source)),
        "start_at": event.start_at,
        "end_at": event.ended_at if event.scope == DisruptionScope.EQUIPMENT else event.end_at,
        "duration_hours": round(hours, 2),
        "slots_affected": event.slots_affected if event.scope == DisruptionScope.SLOTS else None,
        "bookings_affected": event.bookings_affected,
        "reason": event.reason,
        "reason_category": event.reason_category,
        "reason_category_display": reason_category_label(event.disruption_type, event.reason_category),
        "reason_missing": not (event.reason or event.reason_category),
        "action_taken": event.action_taken,
        "action_missing": not event.action_taken,
        "started_at": event.started_at,
        "started_by_name": _user_name(event.started_by),
        "ended_at": event.ended_at,
        "ended_by_name": _user_name(event.ended_by),
        "status": "OPEN" if event_is_open(event, now) else "CLOSED",
        "backfilled": event.backfilled,
        "service_reports": [
            {
                "id": r.pk,
                "name": r.original_name,
                "size_bytes": r.size_bytes,
                "uploaded_at": r.uploaded_at,
                "url": f"/api/equipments/disruptions/{event.pk}/service-report/{r.pk}/",
            }
            for r in report_rows
        ],
    }
    if event.is_deleted:
        row.update(
            {
                "is_deleted": True,
                "deleted_at": event.deleted_at,
                "deleted_by_name": _user_name(event.deleted_by),
                "delete_reason": event.delete_reason,
            }
        )
    return row


def _links_and_reports(events):
    from .models import DisruptionEventSlot, DisruptionServiceReport

    ids = [e.pk for e in events]
    links: dict[int, list] = {i: [] for i in ids}
    for link in DisruptionEventSlot.objects.filter(event_id__in=ids).only(
        "event_id", "start_datetime", "end_datetime", "released_at"
    ):
        links[link.event_id].append(link)
    reports: dict[int, list] = {i: [] for i in ids}
    for r in DisruptionServiceReport.objects.filter(event_id__in=ids):
        reports[r.event_id].append(r)
    return links, reports


def _summary(qs, now) -> dict:
    from .models import DisruptionType

    events = list(qs.select_related(None).prefetch_related(None).only("id", "scope", "start_at", "end_at", "ended_at", "disruption_type", "equipment_id"))
    links, _ = _links_and_reports(events) if events else ({}, {})
    by_type = {k: {"count": 0, "hours": 0.0} for k, _ in DisruptionType.choices}
    total_hours = 0.0
    open_now = 0
    for e in events:
        h = event_duration_hours(e, links=links.get(e.pk, []), now=now)
        total_hours += h
        bucket = by_type.setdefault(e.disruption_type, {"count": 0, "hours": 0.0})
        bucket["count"] += 1
        bucket["hours"] += h
        if event_is_open(e, now):
            open_now += 1
    return {
        "total": len(events),
        "total_hours": round(total_hours, 2),
        "open_now": open_now,
        "reason_missing": qs.filter(reason="", reason_category="").count(),
        "by_type": [
            {
                "type": k,
                "label": str(dict(DisruptionType.choices).get(k, k)),
                "count": v["count"],
                "hours": round(v["hours"], 2),
            }
            for k, v in by_type.items()
        ],
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def disruption_list(request):
    from .models import DisruptionSource, DisruptionType

    params = request.query_params
    is_admin = getattr(request.user, "user_type", None) == UserType.ADMIN
    deleted = is_admin and _truthy(params.get("show_deleted"))
    try:
        qs = _scoped_events(request.user, deleted=deleted)
    except PermissionError:
        return _forbidden()
    now = timezone.now()
    qs = apply_filters(qs, params, now)

    ordering = str(params.get("ordering") or "-start_at").strip()
    desc = ordering.startswith("-")
    field = ORDERING.get(ordering.lstrip("-"), "start_at")
    qs = qs.order_by(f"-{field}" if desc else field, "-id")

    total = qs.count()
    if params.get("limit") is not None or params.get("offset") is not None:
        try:
            limit = max(1, min(500, int(params.get("limit") or 50)))
            offset = max(0, int(params.get("offset") or 0))
        except ValueError:
            limit, offset = 50, 0
        page, page_size = offset // limit + 1, limit
    else:
        try:
            page_size = max(1, min(200, int(params.get("page_size") or 25)))
            page = max(1, int(params.get("page") or 1))
        except ValueError:
            page_size, page = 25, 1
        offset = (page - 1) * page_size
        limit = page_size
    events = list(qs[offset : offset + limit])
    links, reports = _links_and_reports(events)
    results = [serialize_event(e, now=now, links=links[e.pk], reports=reports[e.pk]) for e in events]
    for index, row in enumerate(results, start=offset + 1):
        row["s_no"] = index
    payload = {
        "count": total,
        "page": page,
        "page_size": page_size,
        "results": results,
        "types": [{"value": k, "label": str(v)} for k, v in DisruptionType.choices],
        "sources": [{"value": k, "label": str(v)} for k, v in DisruptionSource.choices],
        "reason_categories": {
            t: [{"value": k, "label": v} for k, v in cats] for t, cats in REASON_CATEGORIES.items()
        },
        "can_filter_department": is_admin,
        "can_delete": True,
        "can_view_deleted": is_admin,
        "show_deleted": deleted,
    }
    if not _truthy(params.get("no_summary")):
        payload["summary"] = _summary(apply_filters(_scoped_events(request.user, deleted=deleted), params, now), now)
    if _truthy(params.get("with_options")):
        payload.update(_filter_options(request.user))
    return Response(payload)


def _filter_options(user) -> dict:
    """Equipment (and, for the Main Administrator, departments) the user may filter by."""
    from .models import Equipment

    ids = disruption_equipment_ids(user)
    eq_qs = Equipment.objects.all() if ids is None else Equipment.objects.filter(equipment_id__in=ids)
    rows = list(
        eq_qs.order_by("name").values(
            "equipment_id", "name", "code", "internal_department_id", "internal_department__name"
        )
    )
    out = {
        "equipment_options": [
            {"id": r["equipment_id"], "name": r["name"] or "", "code": r["code"] or "", "department_id": r["internal_department_id"]}
            for r in rows
        ]
    }
    if getattr(user, "user_type", None) == UserType.ADMIN:
        depts = {r["internal_department_id"]: r["internal_department__name"] for r in rows if r["internal_department_id"]}
        out["department_options"] = [
            {"id": k, "name": v or ""} for k, v in sorted(depts.items(), key=lambda kv: (kv[1] or "").lower())
        ]
    return out


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def disruption_attention(request):
    """Dashboard banner counts. Roles without access get zeros (no 403 noise on every dashboard load)."""
    try:
        qs = _scoped_events(request.user)
    except PermissionError:
        return Response({"enabled": False, "open_now": 0, "reason_missing": 0})
    now = timezone.now()
    return Response(
        {
            "enabled": True,
            "open_now": qs.filter(active_events_q(now)).count(),
            "reason_missing": qs.filter(reason="", reason_category="").count(),
        }
    )


def _get_event(request, pk):
    try:
        qs = _scoped_events(request.user)
    except PermissionError:
        return None, _forbidden()
    event = qs.filter(pk=pk).first()
    if event is None:
        return None, Response({"error": "Disruption not found."}, status=status.HTTP_404_NOT_FOUND)
    return event, None


def _detail_payload(event) -> dict:
    from .models import DisruptionEventEdit

    now = timezone.now()
    links, reports = _links_and_reports([event])
    data = serialize_event(event, now=now, links=links[event.pk], reports=reports[event.pk])
    data["slots"] = [
        {
            "start_datetime": link.start_datetime,
            "end_datetime": link.end_datetime,
            "released_at": link.released_at,
        }
        for link in sorted(links[event.pk], key=lambda x: x.start_datetime)[:500]
    ]
    data["timeline"] = [
        {
            "kind": e.kind,
            "field": e.field,
            "old_value": e.old_value,
            "new_value": e.new_value,
            "note": e.note,
            "by": _user_name(e.edited_by),
            "at": e.edited_at,
        }
        for e in DisruptionEventEdit.objects.filter(event=event).select_related("edited_by")
    ]
    data["reason_categories"] = [
        {"value": k, "label": v} for k, v in REASON_CATEGORIES.get(event.disruption_type, [])
    ]
    return data


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
def disruption_detail(request, pk: int):
    event, error = _get_event(request, pk)
    if error:
        return error
    if request.method == "GET":
        return Response(_detail_payload(event))

    data = request.data or {}
    now = timezone.now()
    user = request.user
    with transaction.atomic():
        if "reason" in data or "reason_category" in data:
            reason = clean_text(data.get("reason")) if "reason" in data else event.reason
            category = (
                clean_reason_category(event.disruption_type, data.get("reason_category"))
                if "reason_category" in data
                else event.reason_category
            )
            if reason != event.reason:
                _log_edit(event, "reason", user, field_name="reason", old=event.reason, new=reason)
            if category != event.reason_category:
                _log_edit(event, "reason", user, field_name="reason_category", old=event.reason_category, new=category)
            if reason != event.reason or category != event.reason_category:
                event.reason, event.reason_category = reason, category
                event.reason_updated_at, event.reason_updated_by = now, user
        if "action_taken" in data:
            action = clean_text(data.get("action_taken"))
            if action != event.action_taken:
                _log_edit(event, "action", user, field_name="action_taken", old=event.action_taken, new=action)
                event.action_taken = action
                event.action_updated_at, event.action_updated_by = now, user
        event.save()
    return Response(_detail_payload(event))


DELETE_REASON_MAX_LENGTH = 500


def _event_for_change(request, pk, *, deleted: bool):
    """Event in the user's scope for delete / restore; 403 when it exists outside the scope."""
    from .models import DisruptionEvent

    try:
        event = _scoped_events(request.user, deleted=deleted).filter(pk=pk).first()
    except PermissionError:
        return None, _forbidden()
    if event is not None:
        return event, None
    if DisruptionEvent.objects.filter(pk=pk, is_deleted=deleted).exists():
        return None, Response(
            {"error": "You can only delete disruptions of equipment you manage."}, status=status.HTTP_403_FORBIDDEN
        )
    return None, Response({"error": "Disruption not found."}, status=status.HTTP_404_NOT_FOUND)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
def disruption_delete(request, pk: int):
    """Soft delete: hidden from history, exports, reports and slot annotations. Slots, bookings and the slot
    status change log are not changed."""
    from .dept_admin_actions import record_staff_action

    event, error = _event_for_change(request, pk, deleted=False)
    if error:
        return error
    reason = clean_text((request.data or {}).get("reason"), DELETE_REASON_MAX_LENGTH)
    now = timezone.now()
    was_open = event_is_open(event, now)
    with transaction.atomic():
        event.is_deleted = True
        event.deleted_at = now
        event.deleted_by = request.user
        event.delete_reason = reason
        event.save(update_fields=["is_deleted", "deleted_at", "deleted_by", "delete_reason", "updated_at"])
        _log_edit(event, "deleted", request.user, note=reason[:255])
    record_staff_action(
        request.user,
        "disruption_deleted",
        equipment_id=event.equipment_id,
        disruption_id=event.pk,
        disruption_type=event.disruption_type,
        was_open=was_open,
        with_reason=bool(reason),
    )
    return Response({"id": event.pk, "deleted": True, "was_open": was_open})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
def disruption_restore(request, pk: int):
    """Main Administrator only: bring a deleted disruption back into history and reports."""
    from .dept_admin_actions import record_staff_action

    if getattr(request.user, "user_type", None) != UserType.ADMIN:
        return Response(
            {"error": "Only the Main Administrator can restore deleted disruptions."}, status=status.HTTP_403_FORBIDDEN
        )
    event, error = _event_for_change(request, pk, deleted=True)
    if error:
        return error
    with transaction.atomic():
        event.is_deleted = False
        event.deleted_at = None
        event.deleted_by = None
        event.delete_reason = ""
        event.save(update_fields=["is_deleted", "deleted_at", "deleted_by", "delete_reason", "updated_at"])
        _log_edit(event, "restored", request.user)
    record_staff_action(request.user, "disruption_restored", equipment_id=event.equipment_id, disruption_id=event.pk)
    return Response({"id": event.pk, "deleted": False})


def _validate_report(upload):
    if upload is None:
        return None, "Attach a file."
    name = os.path.basename(str(getattr(upload, "name", "") or "report")).replace("\x00", "")[:255] or "report"
    ext = os.path.splitext(name)[1].lower()
    content_type = SERVICE_REPORT_TYPES.get(ext)
    if content_type is None:
        return None, "Upload a PDF, image (JPG, PNG, WebP) or Word document."
    size = getattr(upload, "size", 0) or 0
    if size <= 0:
        return None, "The file is empty."
    if size > service_report_max_bytes():
        return None, f"The file is larger than {service_report_max_bytes() // (1024 * 1024)} MB."
    upload.seek(0)
    head = upload.read(16)
    upload.seek(0)
    if not any(head.startswith(sig) for sig in SERVICE_REPORT_MAGIC[content_type]):
        return None, "The file content does not match its type."
    return (name, content_type, size), None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def disruption_service_report_upload(request, pk: int):
    from .models import DisruptionServiceReport

    event, error = _get_event(request, pk)
    if error:
        return error
    upload = request.FILES.get("file")
    meta, message = _validate_report(upload)
    if message:
        return Response({"error": message}, status=status.HTTP_400_BAD_REQUEST)
    name, content_type, size = meta
    with transaction.atomic():
        report = DisruptionServiceReport(
            event=event,
            original_name=name,
            content_type=content_type,
            size_bytes=size,
            uploaded_by=request.user,
        )
        report.file.save(name, upload, save=False)
        report.save()
        _log_edit(event, "report", request.user, note=f"Service report uploaded: {name}"[:255])
    return Response(_detail_payload(event), status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def disruption_service_report_download(request, pk: int, report_id: int):
    from .models import DisruptionServiceReport

    event, error = _get_event(request, pk)
    if error:
        return error
    report = DisruptionServiceReport.objects.filter(pk=report_id, event=event).first()
    if report is None:
        return Response({"error": "Report not found."}, status=status.HTTP_404_NOT_FOUND)
    try:
        fh = report.file.open("rb")
    except Exception:
        return Response({"error": "File not available."}, status=status.HTTP_404_NOT_FOUND)
    inline = _truthy(request.query_params.get("inline"))
    resp = FileResponse(fh, content_type=report.content_type, as_attachment=not inline, filename=report.original_name)
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store"
    return resp


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def slot_status_change_log(request):
    from .models import SlotStatus, SlotStatusChangeLog

    try:
        ids = disruption_equipment_ids(request.user)
    except PermissionError:
        return _forbidden()
    qs = SlotStatusChangeLog.objects.select_related("equipment", "changed_by")
    if ids is not None:
        qs = qs.filter(equipment_id__in=ids)
    params = request.query_params
    equipment_ids = _int_list(params.get("equipment"))
    if equipment_ids:
        qs = qs.filter(equipment_id__in=equipment_ids)
    new_status = str(params.get("new_status") or "").strip().upper()
    if new_status:
        qs = qs.filter(new_status=new_status)
    date_from = _parse_date(params.get("date_from"))
    date_to = _parse_date(params.get("date_to"))
    if date_from:
        qs = qs.filter(changed_at__gte=_day_start(date_from))
    if date_to:
        qs = qs.filter(changed_at__lt=_day_start(date_to + timedelta(days=1)))
    total = qs.count()
    try:
        page_size = max(1, min(200, int(params.get("page_size") or 25)))
        page = max(1, int(params.get("page") or 1))
    except ValueError:
        page_size, page = 25, 1
    labels = dict(SlotStatus.choices)
    rows = [
        {
            "id": log.pk,
            "equipment_id": log.equipment_id,
            "equipment_name": log.equipment.name or log.equipment.code or "",
            "new_status": log.new_status,
            "new_status_display": str(labels.get(log.new_status, log.new_status)),
            "slot_count": log.slot_count,
            "first_start": log.first_start,
            "last_end": log.last_end,
            "label": log.label,
            "external_reference": log.external_reference,
            "source": log.source,
            "bookings_affected": log.bookings_affected,
            "changed_by_name": _user_name(log.changed_by),
            "changed_at": log.changed_at,
        }
        for log in qs[(page - 1) * page_size : page * page_size]
    ]
    return Response({"count": total, "page": page, "page_size": page_size, "results": rows})
