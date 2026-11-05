"""Records and serves the OIC request audit trail.

``record_booking_event`` runs inside ``create_booking_event``, the path every booking event takes, in the same
transaction: a request and its audit row are saved together or not at all. Refused attempts are written by
``record_refusal`` at each booking route. Changes to the equipment's own settings are in the equipment configuration
audit trail.
"""

from __future__ import annotations

import csv

from django.db.models import Q
from django.http import HttpResponse
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .oic_request_audit_models import OicRequestAuditAction, OicRequestAuditLog

PAGE_SIZE_DEFAULT = 25
PAGE_SIZE_MAX = 100
CSV_MAX_ROWS = 20000


def _display_name(user) -> str:
    from iic_booking.users.display import get_user_display_name

    return get_user_display_name(user) or str(getattr(user, "email", "") or "")


def _client_ip(request) -> str:
    if request is None:
        return ""
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return forwarded or request.META.get("REMOTE_ADDR") or ""


def _request_fields(request) -> dict:
    if request is None:
        return {}
    return {"ip_address": _client_ip(request)[:64], "user_agent": (request.META.get("HTTP_USER_AGENT") or "")[:512]}


def _actor_fields(actor) -> dict:
    if actor is None or not getattr(actor, "is_authenticated", False):
        return {}
    department = getattr(actor, "department", None)
    return {
        "actor": actor,
        "actor_label": _display_name(actor)[:255],
        "actor_email": str(getattr(actor, "email", "") or "")[:255],
        "actor_role": str(getattr(actor, "user_type", "") or "")[:64],
        "actor_department": str(getattr(department, "name", "") or "")[:255],
    }


def _equipment_fields(equipment) -> dict:
    department = getattr(equipment, "internal_department", None)
    return {
        "equipment_id": equipment.pk,
        "equipment_code": equipment.code or "",
        "equipment_name": equipment.name or "",
        "department_id": getattr(equipment, "internal_department_id", None),
        "department_name": getattr(department, "name", "") or "",
    }


def record_refusal(request, equipment, *, route: str, reason: str) -> OicRequestAuditLog:
    from .booking_audit import current_source

    return OicRequestAuditLog.objects.create(
        **_equipment_fields(equipment),
        **_actor_fields(getattr(request, "user", None)),
        **_request_fields(request),
        action=OicRequestAuditAction.REFUSED,
        route=route,
        detail=reason,
        source=current_source()[1],
    )


def record_booking_event(event) -> OicRequestAuditLog | None:
    """Copy a booking event on OIC-only equipment into the audit trail. Other equipment is left alone."""
    from .booking_audit import AUDIT_KEY, current_request, current_source
    from .models import BookingEventType

    booking = event.booking
    equipment = booking.equipment
    if not getattr(equipment, "oic_requests_only", False):
        return None
    audit = (event.metadata or {}).get(AUDIT_KEY) or {}
    created = event.event_type == BookingEventType.CREATED
    return OicRequestAuditLog.objects.create(
        **_equipment_fields(equipment),
        **_actor_fields(event.created_by),
        **_request_fields(current_request()),
        action=OicRequestAuditAction.RAISED if created else OicRequestAuditAction.EVENT,
        booking_id=booking.pk,
        booking_reference=(booking.virtual_booking_id or str(booking.pk))[:64],
        event_type=event.event_type or "",
        previous_status=event.previous_status or "",
        new_status=event.new_status or booking.status or "",
        detail=event.comment or "",
        is_system=event.created_by_id is None,
        source=str(audit.get("source_label") or current_source()[1])[:128],
    )


# --------------------------------------------------------------------------- read API
def _visible_rows(user):
    """Main Administrator: everything. Department Administrator and OICs: their own departments."""
    from iic_booking.users.models.user_type import UserType

    from .oic_only_equipment import departments_of_oic

    user_type = getattr(user, "user_type", None)
    if user_type == UserType.ADMIN:
        return OicRequestAuditLog.objects.all()
    if user_type == UserType.DEPT_ADMIN and getattr(user, "department_id", None):
        return OicRequestAuditLog.objects.filter(department_id=user.department_id)
    if user_type == UserType.MANAGER:
        departments = departments_of_oic(user)
        if departments:
            return OicRequestAuditLog.objects.filter(department_id__in=departments)
    return None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _filtered(rows, params):
    if (equipment_id := _int(params.get("equipment_id"))) is not None:
        rows = rows.filter(equipment_id=equipment_id)
    if (action := (params.get("action") or "").strip().upper()) in OicRequestAuditAction.values:
        rows = rows.filter(action=action)
    if day := parse_date(str(params.get("date_from") or "")):
        rows = rows.filter(created_at__date__gte=day)
    if day := parse_date(str(params.get("date_to") or "")):
        rows = rows.filter(created_at__date__lte=day)
    if q := (params.get("q") or "").strip():
        rows = rows.filter(
            Q(equipment_name__icontains=q)
            | Q(equipment_code__icontains=q)
            | Q(booking_reference__icontains=q)
            | Q(actor_label__icontains=q)
            | Q(actor_email__icontains=q)
            | Q(detail__icontains=q)
        )
    return rows.order_by("-created_at", "-id")


def _row(log: OicRequestAuditLog) -> dict:
    return {
        "id": log.pk,
        "created_at": log.created_at.isoformat(),
        "action": log.action,
        "action_display": log.get_action_display(),
        "equipment_id": log.equipment_id,
        "equipment_code": log.equipment_code,
        "equipment_name": log.equipment_name,
        "department_name": log.department_name,
        "booking_id": log.booking_id,
        "booking_reference": log.booking_reference,
        "event_type": log.event_type,
        "previous_status": log.previous_status,
        "new_status": log.new_status,
        "route": log.route,
        "detail": log.detail,
        "actor_id": log.actor_id,
        "actor_label": log.actor_label or ("System" if log.is_system else ""),
        "actor_email": log.actor_email,
        "actor_role": log.actor_role,
        "actor_department": log.actor_department,
        "is_system": log.is_system,
        "source": log.source,
        "ip_address": log.ip_address,
        "user_agent": log.user_agent,
    }


CSV_COLUMNS = [
    ("When", "created_at"), ("Action", "action_display"), ("Equipment", "equipment_name"), ("Code", "equipment_code"),
    ("Department", "department_name"), ("Booking", "booking_reference"), ("Event", "event_type"),
    ("Previous status", "previous_status"), ("New status", "new_status"), ("Route", "route"), ("Detail", "detail"),
    ("By", "actor_label"), ("Email", "actor_email"), ("Role", "actor_role"), ("Their department", "actor_department"),
    ("Source", "source"), ("IP address", "ip_address"), ("Browser", "user_agent"),
]


def _csv_cell(value) -> str:
    """Stop a spreadsheet treating user-supplied text as a formula."""
    text = str(value if value is not None else "")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oic_request_audit_list(request):
    """Filters: equipment_id, action, date_from, date_to (YYYY-MM-DD), q; page, page_size; export=csv."""
    rows = _visible_rows(request.user)
    if rows is None:
        return Response(
            {"error": "The OIC request audit is available to the Main Administrator, Department Administrators and Officers In-charge."},
            status=status.HTTP_403_FORBIDDEN,
        )
    params = request.query_params
    rows = _filtered(rows, params)
    if (params.get("export") or "").lower() == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="oic-request-audit.csv"'
        response.write("﻿")
        writer = csv.writer(response)
        writer.writerow([label for label, _ in CSV_COLUMNS])
        for log in rows[:CSV_MAX_ROWS]:
            row = _row(log)
            writer.writerow([_csv_cell(row[key]) for _, key in CSV_COLUMNS])
        return response
    page_size = min(max(_int(params.get("page_size")) or PAGE_SIZE_DEFAULT, 1), PAGE_SIZE_MAX)
    page = max(_int(params.get("page")) or 1, 1)
    total = rows.count()
    start = (page - 1) * page_size
    return Response(
        {
            "count": total,
            "page": page,
            "page_size": page_size,
            "num_pages": max((total + page_size - 1) // page_size, 1),
            "results": [_row(log) for log in rows[start : start + page_size]],
            "actions": [{"value": v, "label": str(label)} for v, label in OicRequestAuditAction.choices],
        }
    )
