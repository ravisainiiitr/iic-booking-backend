"""API for department-configured faculty credit facility (Dept Admin / Admin)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.department_faculty_credit_facility import (
    get_or_create_settings,
    is_eligible_for_new_facility,
    serialize_available_row,
    serialize_facility_row,
    update_settings,
)
from iic_booking.users.models import Department, UserType
from iic_booking.users.models.department_faculty_credit_facility import (
    FacultyDepartmentCreditFacility,
    FacultyDepartmentCreditFacilityAuditLog,
)
from iic_booking.users.models.wallet import SubWallet, Wallet

User = get_user_model()


def _resolve_department(request) -> tuple[Department | None, Response | None]:
    """Dept Admin → own department; Admin may pass department_id."""
    user = request.user
    ut = str(getattr(user, "user_type", "") or "").lower()
    if ut == UserType.ADMIN:
        raw = request.query_params.get("department_id") or request.data.get("department_id")
        if not raw:
            return None, Response(
                {"error": "department_id is required for Admin."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            dept = Department.objects.get(pk=int(raw))
        except (Department.DoesNotExist, TypeError, ValueError):
            return None, Response({"error": "Department not found."}, status=status.HTTP_404_NOT_FOUND)
        return dept, None

    if ut == UserType.DEPT_ADMIN:
        if not user.department_id:
            return None, Response(
                {"error": "Your account is not assigned to a department."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return user.department, None

    return None, Response(
        {"error": "Only Department Administrators or Institute Admins can manage this facility."},
        status=status.HTTP_403_FORBIDDEN,
    )


def _serialize_settings(settings) -> dict:
    return {
        "department_id": settings.department_id,
        "department_name": settings.department.name if settings.department_id else "",
        "enabled": settings.enabled,
        "joining_date_cutoff": (
            settings.joining_date_cutoff.isoformat() if settings.joining_date_cutoff else None
        ),
        "max_credit_limit": str(settings.max_credit_limit),
        "updated_at": settings.updated_at.isoformat() if settings.updated_at else None,
        "updated_by_email": settings.updated_by.email if settings.updated_by_id else None,
    }


@api_view(["GET", "PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def department_faculty_credit_facility_settings_view(request):
    dept, err = _resolve_department(request)
    if err:
        return err

    if request.method == "GET":
        settings = get_or_create_settings(dept.id)
        return Response(_serialize_settings(settings))

    enabled = request.data.get("enabled")
    if enabled is None:
        enabled = get_or_create_settings(dept.id).enabled
    else:
        enabled = bool(enabled)

    cutoff_raw = request.data.get("joining_date_cutoff", ...)
    settings = get_or_create_settings(dept.id)
    if cutoff_raw is ...:
        cutoff = settings.joining_date_cutoff
    elif cutoff_raw in (None, ""):
        cutoff = None
    else:
        try:
            cutoff = datetime.strptime(str(cutoff_raw)[:10], "%Y-%m-%d").date()
        except ValueError:
            return Response(
                {"error": "joining_date_cutoff must be YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    limit_raw = request.data.get("max_credit_limit", ...)
    if limit_raw is ...:
        limit = settings.max_credit_limit
    else:
        try:
            limit = Decimal(str(limit_raw))
        except (InvalidOperation, TypeError, ValueError):
            return Response(
                {"error": "max_credit_limit must be a valid amount."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if limit < 0:
            return Response(
                {"error": "max_credit_limit cannot be negative."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    updated = update_settings(
        department_id=dept.id,
        enabled=enabled,
        joining_date_cutoff=cutoff,
        max_credit_limit=limit,
        actor=request.user,
    )
    return Response(_serialize_settings(updated))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def department_faculty_credit_facility_faculty_list_view(request):
    dept, err = _resolve_department(request)
    if err:
        return err

    settings = get_or_create_settings(dept.id)
    include_available = str(request.query_params.get("include_available", "1")).lower() not in (
        "0",
        "false",
        "no",
    )

    rows: list[dict] = []
    facilities = (
        FacultyDepartmentCreditFacility.objects.filter(department_id=dept.id)
        .select_related("user", "department")
        .order_by("-availed_at", "-id")
    )
    user_ids = [f.user_id for f in facilities]
    balances = {
        sw.wallet.user_id: sw.balance
        for sw in SubWallet.objects.filter(
            department_id=dept.id, wallet__user_id__in=user_ids
        ).select_related("wallet")
    }
    availed_user_ids = set()
    for f in facilities:
        availed_user_ids.add(f.user_id)
        rows.append(serialize_facility_row(f, balance=balances.get(f.user_id)))

    if include_available and settings.enabled:
        faculty_qs = User.objects.filter(user_type=UserType.FACULTY).order_by("name", "email")
        home = list(faculty_qs.filter(department_id=dept.id))
        wallet_user_ids = set(
            Wallet.objects.filter(sub_wallets__department_id=dept.id).values_list("user_id", flat=True)
        )
        extras = list(faculty_qs.filter(id__in=wallet_user_ids).exclude(department_id=dept.id))
        candidates = {u.id: u for u in home + extras}

        for uid, u in candidates.items():
            if uid in availed_user_ids:
                continue
            sw = SubWallet.objects.filter(wallet__user_id=uid, department_id=dept.id).first()
            bal = Decimal(str(sw.balance)) if sw is not None else Decimal("0.00")
            if is_eligible_for_new_facility(u, dept, balance=bal, sub=sw):
                rows.append(serialize_available_row(u, dept, settings, bal))

    return Response({"department_id": dept.id, "results": rows, "count": len(rows)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def department_faculty_credit_facility_audit_list_view(request):
    dept, err = _resolve_department(request)
    if err:
        return err

    try:
        limit = min(int(request.query_params.get("limit", 100)), 500)
    except (TypeError, ValueError):
        limit = 100

    logs = (
        FacultyDepartmentCreditFacilityAuditLog.objects.filter(department_id=dept.id)
        .select_related("faculty_user", "actor", "facility")
        .order_by("-created_at", "-id")[:limit]
    )
    results = [
        {
            "id": log.id,
            "event_type": log.event_type,
            "event_type_display": log.get_event_type_display(),
            "message": log.message,
            "metadata": log.metadata or {},
            "faculty_user_id": log.faculty_user_id,
            "faculty_email": log.faculty_user.email if log.faculty_user_id else None,
            "faculty_name": (log.faculty_user.name or log.faculty_user.email) if log.faculty_user_id else None,
            "actor_email": log.actor.email if log.actor_id else None,
            "facility_id": log.facility_id,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]
    return Response({"department_id": dept.id, "results": results, "count": len(results)})
