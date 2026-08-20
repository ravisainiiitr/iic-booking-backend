"""Main Administrator APIs for identity, degree, department mapping, HoD, student lifecycle."""

from __future__ import annotations

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.identity.extract import normalize_label
from iic_booking.users.identity.hod import HodError, assign_hod, disable_hod
from iic_booking.users.identity.lifecycle import LifecycleError, approve_extension, request_six_month_extension
from iic_booking.users.identity.service import UserIdentityService
from iic_booking.users.models import Department, User, UserType
from iic_booking.users.models.channel_i_identity import (
    ChannelIDepartmentMapping,
    ChannelIIdentityProfile,
    DegreeClassificationKind,
    HeadOfDepartmentAssignment,
    StudentDegreeClassification,
    StudentValidityExtension,
    StudentValiditySource,
)
from iic_booking.users.models.wallet_credit_facility import WalletCreditFacility, WalletCreditFacilityStatus


def _admin(user) -> bool:
    return getattr(user, "user_type", None) == UserType.ADMIN


def _faculty(user) -> bool:
    return getattr(user, "user_type", None) == UserType.FACULTY


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def identity_dashboard(request):
    if not _admin(request.user):
        return Response({"error": "Forbidden.", "code": "FORBIDDEN"}, status=403)
    today = timezone.localdate()
    students = User.objects.filter(user_type__in={UserType.STUDENT, UserType.INDIVIDUAL_STUDENT})
    unmapped = ChannelIDepartmentMapping.objects.filter(
        Q(internal_department__isnull=True) | Q(active=False)
    ).count()
    unknown_degrees = ChannelIIdentityProfile.objects.exclude(student_degree_name="").count()
    classified = set(
        StudentDegreeClassification.objects.filter(active=True).values_list(
            "channel_i_degree_name_normalized", flat=True
        )
    )
    unknown_count = 0
    for name in ChannelIIdentityProfile.objects.exclude(student_degree_name="").values_list(
        "student_degree_name", flat=True
    ):
        if normalize_label(name) not in classified:
            unknown_count += 1
    unresolved = ChannelIIdentityProfile.objects.filter(
        validity_source=StudentValiditySource.UNRESOLVED, has_student_payload=True
    ).count()
    active_hods = HeadOfDepartmentAssignment.objects.filter(active=True).count()
    outstanding = WalletCreditFacility.objects.filter(
        status__in={
            WalletCreditFacilityStatus.CREDITED,
            WalletCreditFacilityStatus.PARTIALLY_SETTLED,
        }
    )
    from django.db.models import Sum

    out_sum = outstanding.aggregate(s=Sum("outstanding_amount"))["s"] or 0
    return Response(
        {
            "students_active": students.filter(is_active=True, force_inactive=False).count(),
            "students_expired": students.filter(force_inactive=True).count(),
            "students_expiring": ChannelIIdentityProfile.objects.filter(
                derived_end_date__isnull=False,
                derived_end_date__lte=today,
            ).count(),
            "departments_unmapped": unmapped,
            "unknown_degrees": unknown_count,
            "users_unresolved": unresolved,
            "active_hods": active_hods,
            "extension_requests": StudentValidityExtension.objects.filter(status="SUBMITTED").count(),
            "wallet_credit_outstanding": str(out_sum),
            "wallet_credit_overdue": outstanding.filter(due_date__lt=today).count(),
        }
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def degree_classification_list(request):
    if not _admin(request.user):
        return Response({"error": "Forbidden.", "code": "FORBIDDEN"}, status=403)
    if request.method == "GET":
        rows = StudentDegreeClassification.objects.all().order_by("channel_i_degree_name")
        return Response(
            {
                "results": [
                    {
                        "id": r.id,
                        "channel_i_degree_name": r.channel_i_degree_name,
                        "normalized_degree_name": r.normalized_degree_name,
                        "classification": r.classification,
                        "active": r.active,
                    }
                    for r in rows
                ]
            }
        )
    name = (request.data.get("channel_i_degree_name") or "").strip()
    if not name:
        return Response({"error": "channel_i_degree_name is required."}, status=400)
    classification = request.data.get("classification") or DegreeClassificationKind.UNDERGRADUATE
    obj, created = StudentDegreeClassification.objects.update_or_create(
        channel_i_degree_name_normalized=normalize_label(name),
        defaults={
            "channel_i_degree_name": name,
            "normalized_degree_name": (request.data.get("normalized_degree_name") or name).strip(),
            "classification": classification,
            "active": request.data.get("active", True),
            "updated_by": request.user,
            "created_by": request.user,
        },
    )
    return Response({"id": obj.id, "created": created}, status=201 if created else 200)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def department_mapping_list(request):
    if not _admin(request.user):
        return Response({"error": "Forbidden.", "code": "FORBIDDEN"}, status=403)
    if request.method == "GET":
        unmapped_only = str(request.GET.get("unmapped") or "") in {"1", "true", "yes"}
        qs = ChannelIDepartmentMapping.objects.select_related("internal_department").order_by(
            "channel_i_department_name"
        )
        if unmapped_only:
            qs = qs.filter(Q(internal_department__isnull=True) | Q(active=False))
        q = (request.GET.get("q") or "").strip()
        if q:
            qs = qs.filter(channel_i_department_name__icontains=q)
        return Response(
            {
                "results": [
                    {
                        "id": r.id,
                        "channel_i_department_name": r.channel_i_department_name,
                        "internal_department_id": r.internal_department_id,
                        "internal_department_name": r.internal_department.name if r.internal_department_id else None,
                        "active": r.active,
                        "status": r.status,
                    }
                    for r in qs[:300]
                ]
            }
        )
    name = (request.data.get("channel_i_department_name") or "").strip()
    if not name:
        return Response({"error": "channel_i_department_name is required."}, status=400)
    dept_id = request.data.get("internal_department_id")
    dept = Department.objects.filter(pk=dept_id).first() if dept_id else None
    obj, created = ChannelIDepartmentMapping.objects.update_or_create(
        channel_i_department_name_normalized=normalize_label(name),
        defaults={
            "channel_i_department_name": name,
            "internal_department": dept,
            "active": request.data.get("active", True),
            "updated_by": request.user,
            "created_by": request.user,
        },
    )
    return Response({"id": obj.id, "status": obj.status, "created": created}, status=201 if created else 200)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def hod_assignment_list(request):
    if not _admin(request.user):
        return Response({"error": "Forbidden.", "code": "FORBIDDEN"}, status=403)
    if request.method == "GET":
        qs = HeadOfDepartmentAssignment.objects.select_related("user", "department").order_by("-created_at")
        return Response(
            {
                "results": [
                    {
                        "id": a.id,
                        "user_id": a.user_id,
                        "user_email": a.user.email,
                        "user_name": a.user.name,
                        "department_id": a.department_id,
                        "department_name": a.department.name,
                        "active": a.active,
                        "effective_from": a.effective_from.isoformat() if a.effective_from else None,
                        "effective_to": a.effective_to.isoformat() if a.effective_to else None,
                    }
                    for a in qs[:200]
                ]
            }
        )
    user = get_object_or_404(User, pk=request.data.get("user_id"))
    department = get_object_or_404(Department, pk=request.data.get("department_id"))
    try:
        assignment = assign_hod(department=department, user=user, actor=request.user)
    except HodError as exc:
        return Response({"error": exc.message, "code": exc.code}, status=exc.status)
    return Response({"id": assignment.id}, status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def hod_assignment_disable(request, assignment_id: int):
    if not _admin(request.user):
        return Response({"error": "Forbidden.", "code": "FORBIDDEN"}, status=403)
    assignment = get_object_or_404(HeadOfDepartmentAssignment, pk=assignment_id)
    try:
        disable_hod(assignment=assignment, actor=request.user)
    except HodError as exc:
        return Response({"error": exc.message, "code": exc.code}, status=exc.status)
    return Response({"ok": True})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def student_lifecycle_list(request):
    if not _admin(request.user):
        return Response({"error": "Forbidden.", "code": "FORBIDDEN"}, status=403)
    qs = ChannelIIdentityProfile.objects.filter(has_student_payload=True).select_related("user")[:200]
    results = []
    for p in qs:
        view = UserIdentityService.view(p.user)
        results.append(
            {
                "user_id": p.user_id,
                "email": p.user.email,
                "name": p.user.name,
                "classification": view.classification,
                "degree": view.degree_name,
                "channel_i_department": view.channel_i_department_name,
                "internal_department": view.internal_department_name,
                "start_date": p.student_start_date.isoformat() if p.student_start_date else None,
                "channel_i_end_date": p.student_end_date.isoformat() if p.student_end_date else None,
                "derived_end_date": view.validity.derived_end_date.isoformat()
                if view.validity.derived_end_date
                else None,
                "validity_source": view.validity.validity_source,
                "is_active": p.user.is_active,
                "force_inactive": p.user.force_inactive,
            }
        )
    return Response({"results": results})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def identity_my_hod(request):
    from iic_booking.users.identity.service import UserEligibilityService

    hod = UserEligibilityService.get_valid_hod(request.user)
    view = UserIdentityService.view(request.user)
    payload = {
        "classification": view.classification,
        "is_undergraduate": view.is_undergraduate,
        "department_status": view.department_status,
        "internal_department_name": view.internal_department_name,
        "hod": None,
        "message": None,
    }
    if view.department_status == "UNMAPPED":
        payload["message"] = (
            "Your institutional department is not yet mapped in the portal. Please contact the administrator."
        )
    if hod:
        payload["hod"] = {
            "id": hod.user_id,
            "name": hod.user.name,
            "email": hod.user.email,
            "department": hod.department.name,
            "has_wallet": hasattr(hod.user, "wallet"),
        }
    return Response(payload)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def validity_extension_list(request):
    if request.method == "GET":
        if _admin(request.user):
            qs = StudentValidityExtension.objects.select_related("student", "requested_by").order_by("-created_at")
        elif _faculty(request.user):
            qs = StudentValidityExtension.objects.filter(requested_by=request.user).order_by("-created_at")
        else:
            return Response({"error": "Forbidden.", "code": "FORBIDDEN"}, status=403)
        return Response(
            {
                "results": [
                    {
                        "id": e.id,
                        "student_email": e.student.email,
                        "previous_expiry": e.previous_expiry.isoformat(),
                        "requested_expiry": e.requested_expiry.isoformat(),
                        "approved_expiry": e.approved_expiry.isoformat() if e.approved_expiry else None,
                        "status": e.status,
                        "reason": e.reason,
                    }
                    for e in qs[:200]
                ]
            }
        )
    if not _faculty(request.user):
        return Response({"error": "Forbidden.", "code": "FORBIDDEN"}, status=403)
    student = get_object_or_404(User, pk=request.data.get("student_id"))
    try:
        ext = request_six_month_extension(
            student=student,
            faculty=request.user,
            reason=request.data.get("reason") or "",
        )
    except LifecycleError as exc:
        return Response({"error": exc.message, "code": exc.code}, status=exc.status)
    return Response({"id": ext.id, "requested_expiry": ext.requested_expiry.isoformat()}, status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def validity_extension_approve(request, extension_id: int):
    if not _admin(request.user):
        return Response({"error": "Forbidden.", "code": "FORBIDDEN"}, status=403)
    ext = get_object_or_404(StudentValidityExtension, pk=extension_id)
    try:
        ext = approve_extension(extension=ext, admin=request.user, reason=request.data.get("reason") or "")
    except LifecycleError as exc:
        return Response({"error": exc.message, "code": exc.code}, status=exc.status)
    return Response({"id": ext.id, "status": ext.status, "approved_expiry": ext.approved_expiry.isoformat()})
