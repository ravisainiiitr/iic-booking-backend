"""Programme / employment end date: the user's own status, the supervisor overview, one-click extension
decisions from email, and Main Administrator report / extension."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from iic_booking.users import account_lifecycle as lc
from iic_booking.users import registration_approvals as svc
from iic_booking.users.api.registration_approval_views import (
    _OptionalTokenAuthentication,
    _bool,
    _error,
    _extension_sent_message,
    _schema_pending,
)
from iic_booking.users.models import RegistrationApprovalChannel, RegistrationExtensionStatus, User
from iic_booking.users.models.user_type import UserType


def _not_in_scope() -> Response:
    return Response(
        {"error": "Programme end dates apply to IITR students, post-docs, research associates and IITR Startups.", "code": "not_in_scope"},
        status=status.HTTP_404_NOT_FOUND,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_account_lifecycle(request):
    data = lc.status_payload(request.user)
    if data is None:
        return Response({"in_scope": False})
    return Response({"in_scope": True, **data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def my_extension_request(request):
    if not lc.category_for(request.user):
        return _not_in_scope()
    if not svc.schema_ready():
        return _schema_pending()
    try:
        ext = svc.request_extension(
            request.user,
            reason=(request.data or {}).get("reason", ""),
            channel=RegistrationApprovalChannel.PORTAL,
            request=request,
        )
    except svc.ApprovalError as err:
        return _error(err)
    return Response(
        {"message": _extension_sent_message(ext), "status": lc.status_payload(request.user)},
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def my_end_date(request):
    """Post-docs, RAs and startups with no end date on record give it once."""
    try:
        lc.provide_end_date(request.user, (request.data or {}).get("end_date"), request=request)
    except svc.ApprovalError as err:
        return _error(err)
    return Response({"message": "End date recorded.", "status": lc.status_payload(request.user)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def supervisor_lifecycle_overview(request):
    if getattr(request.user, "user_type", None) != UserType.FACULTY:
        return Response({"error": "Only IITR faculty have supervised users.", "code": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
    return Response(lc.supervisor_overview(request.user))


@api_view(["GET", "POST"])
@authentication_classes([_OptionalTokenAuthentication])
@permission_classes([AllowAny])
def extension_email_decision(request):
    """Approve / Decline buttons in the extension email. GET shows the request; POST records the decision."""
    if not svc.schema_ready():
        return _schema_pending()
    data = request.data if request.method == "POST" else request.query_params
    raw = (data.get("token") or "").strip()
    viewer = request.user if getattr(request.user, "is_authenticated", False) else None
    try:
        row = svc.resolve_extension_email_token(raw, viewer=viewer, request=request)
    except svc.ApprovalError as err:
        return _error(err)
    ext = row.extension
    if request.method == "GET":
        svc.record_view(faculty=row.faculty, extension=ext, channel=RegistrationApprovalChannel.EMAIL_LINK, request=request)
        item = svc.serialize_extension(ext, for_faculty=True)
        row_lc = lc.lifecycle_for(ext.user)
        item.update(
            {
                "faculty_name": svc._name(row.faculty),
                "degree_name": (row_lc.degree_name if row_lc else "") or "",
                "end_date_source_note": lc.source_note(row_lc) if row_lc else "",
                "category_label": lc.category_label(lc.category_for(ext.user)),
            }
        )
        return Response({"item": item})
    try:
        ext = svc.decide_extension(
            ext,
            actor=row.faculty,
            decision=data.get("decision", ""),
            until=data.get("until"),
            reason=data.get("reason", ""),
            disclaimer_accepted=_bool(data.get("disclaimer_accepted")),
            disclaimer_version=data.get("disclaimer_version", ""),
            raw_token=raw,
            request=request,
        )
    except svc.ApprovalError as err:
        return _error(err)
    granted = ext.status == RegistrationExtensionStatus.APPROVED
    return Response(
        {
            "decision": "approved" if granted else "declined",
            "message": (
                f"Extended until {svc._fmt_date(ext.approved_until)}. The user has been emailed and you are copied."
                if granted
                else "Declined. The user has been emailed your reason and you are copied."
            ),
        }
    )


def _admin_only(request):
    if not svc.is_main_admin(request.user):
        return Response({"error": "Only the Main Administrator can do this.", "code": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
    return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_lifecycle_report(request):
    refusal = _admin_only(request)
    if refusal:
        return refusal
    return Response(lc.report())


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_user_lifecycle(request, user_id: int):
    refusal = _admin_only(request)
    if refusal:
        return refusal
    user = User.objects.select_related("supervisor__department", "department").filter(pk=user_id).first()
    if user is None:
        return Response({"error": "User not found.", "code": "not_found"}, status=status.HTTP_404_NOT_FOUND)
    data = lc.status_payload(user)
    if data is None:
        return _not_in_scope()
    return Response({"user": lc._person(user), **data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_user_extend(request, user_id: int):
    """Main Administrator extends / overrides directly (same six-month cap); closes any pending request."""
    refusal = _admin_only(request)
    if refusal:
        return refusal
    user = User.objects.select_related("supervisor__department").filter(pk=user_id).first()
    if user is None or not lc.category_for(user):
        return _not_in_scope()
    if not svc.schema_ready():
        return _schema_pending()
    data = request.data or {}
    try:
        ext = svc.admin_grant_extension(user, actor=request.user, until=data.get("until"), reason=data.get("reason", ""), request=request)
    except svc.ApprovalError as err:
        return _error(err)
    lc.forget_cached(user)
    return Response(
        {"message": f"Extended until {svc._fmt_date(ext.approved_until)}.", "status": lc.status_payload(user)}
    )
