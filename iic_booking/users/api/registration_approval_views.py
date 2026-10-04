"""Registration requests (Main Administrator), faculty approvals and programme extension requests."""

from __future__ import annotations

import csv

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from iic_booking.users import registration_approvals as svc
from iic_booking.users.api.token_auth import TokenAuthenticationWithInactivity
from iic_booking.users.models import (
    RegistrationApproval,
    RegistrationApprovalChannel,
    RegistrationApprovalEvent,
    RegistrationApprovalStatus,
    RegistrationExtensionRequest,
    RegistrationExtensionStatus,
    User,
)

LOG_PAGE_SIZE = 50
LOG_CSV_LIMIT = 20000
LIST_PAGE_SIZE = 50


def _error(err: svc.ApprovalError) -> Response:
    return Response({"error": err.message, "code": err.code, **err.extra}, status=err.status)


def _schema_pending() -> Response:
    return Response(
        {
            "error": "Registration approvals are being set up. Try again after the database update.",
            "code": "schema_pending",
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _forbidden() -> Response:
    return Response(
        {"error": "Only the Main Administrator can manage registration requests.", "code": "forbidden"},
        status=status.HTTP_403_FORBIDDEN,
    )


def _admin_guard(request):
    if not svc.is_main_admin(request.user):
        return _forbidden()
    if not svc.schema_ready():
        return _schema_pending()
    return None


def _scoped_user(user_id: int):
    return svc.scoped_users().select_related("department", "supervisor__department").filter(pk=user_id).first()


def _not_found() -> Response:
    return Response({"error": "Registration request not found.", "code": "not_found"}, status=status.HTTP_404_NOT_FOUND)


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Main Administrator
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_registration_requests(request):
    guard = _admin_guard(request)
    if guard:
        return guard
    rows = svc.list_rows(request.query_params)
    page_size = max(1, min(200, _int(request.query_params.get("page_size"), LIST_PAGE_SIZE)))
    page = max(1, _int(request.query_params.get("page"), 1))
    start = (page - 1) * page_size
    return Response(
        {
            "count": len(rows),
            "page": page,
            "page_size": page_size,
            "results": rows[start : start + page_size],
            "summary": svc.summary_counts(),
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_registration_summary(request):
    guard = _admin_guard(request)
    if guard:
        return guard
    return Response(svc.summary_counts())


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_registration_request_detail(request, user_id: int):
    guard = _admin_guard(request)
    if guard:
        return guard
    user = _scoped_user(user_id)
    if user is None:
        return _not_found()
    return Response(svc.serialize_detail(user, request=request))


def _admin_action(request, user_id: int, fn):
    guard = _admin_guard(request)
    if guard:
        return guard
    user = _scoped_user(user_id)
    if user is None:
        return _not_found()
    try:
        message = fn(user, request.data or {})
    except svc.ApprovalError as err:
        return _error(err)
    user.refresh_from_db()
    return Response({"message": message, "request": svc.serialize_detail(user, request=request)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_registration_approve(request, user_id: int):
    def run(user, data):
        svc.admin_decide(user, actor=request.user, approve=True, reason=data.get("reason", ""), request=request)
        return "Approved. The account is now active and the user has been emailed."

    return _admin_action(request, user_id, run)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_registration_reject(request, user_id: int):
    def run(user, data):
        svc.admin_decide(user, actor=request.user, approve=False, reason=data.get("reason", ""), request=request)
        return "Rejected. The reason has been emailed to the user."

    return _admin_action(request, user_id, run)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_registration_forward(request, user_id: int):
    def run(user, data):
        svc.forward(user, actor=request.user, request=request)
        return f"Sent to {svc._name(user.supervisor)} for approval."

    return _admin_action(request, user_id, run)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_registration_remind(request, user_id: int):
    def run(user, data):
        svc.remind(user, actor=request.user, request=request)
        return f"Reminder sent to {svc._name(user.supervisor)}."

    return _admin_action(request, user_id, run)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_registration_change_faculty(request, user_id: int):
    def run(user, data):
        faculty = (
            User.objects.select_related("department").filter(pk=_int(data.get("faculty_id"), 0)).first()
            if data.get("faculty_id")
            else None
        )
        svc.change_faculty(
            user,
            faculty=faculty,
            reason=data.get("reason", ""),
            actor=request.user,
            request=request,
            then_forward=_bool(data.get("forward")),
        )
        return "Faculty changed." + (" The request has been sent to them." if _bool(data.get("forward")) else "")

    return _admin_action(request, user_id, run)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_registration_extend(request, user_id: int):
    def run(user, data):
        ext = svc.admin_grant_extension(
            user, actor=request.user, until=data.get("until"), reason=data.get("reason", ""), request=request
        )
        return f"Access extended until {svc._fmt_date(ext.approved_until)}."

    return _admin_action(request, user_id, run)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_extension_decide(request, ext_id: int):
    guard = _admin_guard(request)
    if guard:
        return guard
    ext = RegistrationExtensionRequest.objects.select_related("user").filter(pk=ext_id).first()
    if ext is None:
        return _not_found()
    data = request.data or {}
    try:
        svc.decide_extension(
            ext,
            actor=request.user,
            decision=data.get("decision", ""),
            until=data.get("until"),
            reason=data.get("reason", ""),
            request=request,
        )
    except svc.ApprovalError as err:
        return _error(err)
    user = User.objects.get(pk=ext.user_id)
    return Response({"message": "Extension decided.", "request": svc.serialize_detail(user, request=request)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_extension_remind(request, ext_id: int):
    guard = _admin_guard(request)
    if guard:
        return guard
    ext = RegistrationExtensionRequest.objects.select_related("user", "faculty").filter(pk=ext_id).first()
    if ext is None:
        return _not_found()
    try:
        svc.remind_extension(ext, actor=request.user, request=request)
    except svc.ApprovalError as err:
        return _error(err)
    return Response({"message": f"Reminder sent to {svc._name(ext.faculty)}."})


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def admin_registration_bulk_forward(request):
    guard = _admin_guard(request)
    if guard:
        return guard
    if request.method == "GET":
        rows = svc.bulk_forward_candidates(True)
        return Response({"count": len(rows), "results": [svc.serialize_row(u) for u in rows[:500]]})
    data = request.data or {}
    if "confirm_count" not in data:
        return Response(
            {"error": "Confirm the number of requests to forward.", "code": "confirm_required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        result = svc.bulk_forward(actor=request.user, request=request, expected_count=_int(data.get("confirm_count"), -1))
    except svc.ApprovalError as err:
        return _error(err)
    return Response({"message": f"Forwarded {result['forwarded']} request(s) to the faculty named.", **result})


def _log_queryset(params):
    qs = RegistrationApprovalEvent.objects.select_related("actor").order_by("-created_at", "-id")
    action = (params.get("action") or "").strip()
    if action:
        qs = qs.filter(action__in=[a for a in action.split(",") if a])
    role = (params.get("actor_role") or "").strip()
    if role:
        qs = qs.filter(actor_role=role)
    channel = (params.get("channel") or "").strip()
    if channel:
        qs = qs.filter(channel=channel)
    if params.get("user_id"):
        qs = qs.filter(user_id=_int(params.get("user_id"), 0))
    start = svc._parse_until(params.get("date_from"))
    end = svc._parse_until(params.get("date_to"))
    if start:
        qs = qs.filter(created_at__date__gte=start)
    if end:
        qs = qs.filter(created_at__date__lte=end)
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(subject_email__icontains=q) | Q(subject_name__icontains=q) | Q(actor_email__icontains=q))
    return qs


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_registration_log(request):
    guard = _admin_guard(request)
    if guard:
        return guard
    qs = _log_queryset(request.query_params)
    if (request.query_params.get("export") or "").lower() == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
        response["Content-Disposition"] = f'attachment; filename="registration-approval-log-{stamp}.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ["Time", "Action", "User", "User email", "Actor", "Actor email", "Role", "Channel", "IP address", "Details"]
        )
        for e in qs[:LOG_CSV_LIMIT]:
            details = "; ".join(f"{k}={v}" for k, v in (e.details or {}).items())
            writer.writerow(
                [
                    timezone.localtime(e.created_at).strftime("%Y-%m-%d %H:%M:%S"),
                    e.get_action_display(),
                    e.subject_name,
                    e.subject_email,
                    svc._name(e.actor) if e.actor_id else "",
                    e.actor_email,
                    e.actor_role,
                    e.channel,
                    e.ip_address or "",
                    details,
                ]
            )
        return response
    page_size = max(1, min(200, _int(request.query_params.get("page_size"), LOG_PAGE_SIZE)))
    page = max(1, _int(request.query_params.get("page"), 1))
    total = qs.count()
    rows = [svc.serialize_event(e) for e in qs[(page - 1) * page_size : page * page_size]]
    return Response(
        {
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": rows,
            "actions": [{"value": v, "label": str(label)} for v, label in RegistrationApprovalEvent.Action.choices],
            "channels": [{"value": v, "label": str(label)} for v, label in RegistrationApprovalChannel.choices],
        }
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def admin_registration_automation(request):
    guard = _admin_guard(request)
    if guard:
        return guard
    if request.method == "POST":
        data = request.data or {}
        enabled = _bool(data.get("enabled"))
        if enabled and (data.get("confirm") or "") != "ENABLE":
            return Response(
                {"error": "Type ENABLE to switch the expiry automation on.", "code": "confirm_required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        svc.set_automation(enabled, actor=request.user, request=request)
    row = svc.policy()
    disabled_with_bookings = []
    now = timezone.now()
    for approval in RegistrationApproval.objects.filter(expiry_set_force_inactive=True).select_related("user")[:200]:
        bookings = [svc.serialize_booking(b) for b in svc.future_bookings(approval.user, now)[:20]]
        if bookings:
            disabled_with_bookings.append(
                {"user_id": approval.user_id, "name": svc._name(approval.user), "email": approval.user.email, "bookings": bookings}
            )
    return Response(
        {
            "enabled": bool(row.expiry_automation_enabled),
            "enabled_at": svc._iso(row.enabled_at),
            "warning_days": list(svc.warning_days(row)),
            "token_valid_days": svc.token_valid_days(),
            "extension_max_months": svc.EXTENSION_MAX_MONTHS,
            "dry_run": svc.dry_run(),
            "disabled_with_future_bookings": disabled_with_bookings,
        }
    )


# ---------------------------------------------------------------------------
# Faculty
# ---------------------------------------------------------------------------


def _faculty_guard(request):
    if not svc.schema_ready():
        return _schema_pending()
    return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def faculty_registration_approvals(request):
    guard = _faculty_guard(request)
    if guard:
        return guard
    me = request.user
    pending = (
        RegistrationApproval.objects.filter(faculty=me, status=RegistrationApprovalStatus.PENDING_FACULTY)
        .select_related("user__department")
        .order_by("forwarded_at")
    )
    decided = (
        RegistrationApproval.objects.filter(faculty=me, decided_by=me)
        .exclude(status=RegistrationApprovalStatus.PENDING_FACULTY)
        .select_related("user__department")
        .order_by("-decided_at")[:20]
    )
    ext_pending = (
        RegistrationExtensionRequest.objects.filter(faculty=me, status=RegistrationExtensionStatus.PENDING)
        .select_related("user__department", "faculty")
        .order_by("created_at")
    )
    ext_decided = (
        RegistrationExtensionRequest.objects.filter(faculty=me, decided_by=me)
        .exclude(status=RegistrationExtensionStatus.PENDING)
        .select_related("user__department", "faculty")
        .order_by("-decided_at")[:20]
    )
    return Response(
        {
            "registrations": [svc.serialize_request_for_faculty(a) for a in pending],
            "extensions": [svc.serialize_extension(e, for_faculty=True) for e in ext_pending],
            "recent_registrations": [svc.serialize_request_for_faculty(a) for a in decided],
            "recent_extensions": [svc.serialize_extension(e) for e in ext_decided],
            "extension_max_months": svc.EXTENSION_MAX_MONTHS,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def faculty_review_token(request):
    guard = _faculty_guard(request)
    if guard:
        return guard
    raw = (request.query_params.get("token") or "").strip()
    try:
        token = svc.resolve_token(raw, request.user, request=request)
    except svc.ApprovalError as err:
        return _error(err)
    channel = RegistrationApprovalChannel.EMAIL_LINK
    if token.approval_id:
        svc.record_view(faculty=request.user, approval=token.approval, channel=channel, request=request)
        return Response({"kind": "registration", "item": svc.serialize_request_for_faculty(token.approval)})
    svc.record_view(faculty=request.user, extension=token.extension, channel=channel, request=request)
    return Response({"kind": "extension", "item": svc.serialize_extension(token.extension, for_faculty=True)})


def _own_or_403(obj, user):
    if obj is None:
        return _not_found()
    if obj.faculty_id != user.pk:
        return Response(
            {"error": "This request is not addressed to you.", "code": "not_your_request"},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def faculty_registration_approval_detail(request, approval_id: int):
    guard = _faculty_guard(request)
    if guard:
        return guard
    approval = RegistrationApproval.objects.select_related("user__department").filter(pk=approval_id).first()
    refusal = _own_or_403(approval, request.user)
    if refusal:
        return refusal
    svc.record_view(faculty=request.user, approval=approval, request=request)
    return Response(svc.serialize_request_for_faculty(approval))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def faculty_registration_approval_decide(request, approval_id: int):
    guard = _faculty_guard(request)
    if guard:
        return guard
    approval = RegistrationApproval.objects.select_related("user").filter(pk=approval_id).first()
    data = request.data or {}
    raw = (data.get("token") or "").strip()
    if not raw:
        refusal = _own_or_403(approval, request.user)
        if refusal:
            return refusal
    elif approval is None:
        return _not_found()
    try:
        approval = svc.faculty_decide(
            approval,
            faculty=request.user,
            decision=data.get("decision", ""),
            reason=data.get("reason", ""),
            disclaimer_accepted=_bool(data.get("disclaimer_accepted")),
            disclaimer_version=data.get("disclaimer_version", ""),
            raw_token=raw,
            request=request,
        )
    except svc.ApprovalError as err:
        return _error(err)
    approved = approval.status == RegistrationApprovalStatus.APPROVED
    return Response(
        {
            "message": (
                "Approved. The account is now active; the user has been emailed and you are copied."
                if approved
                else "Declined. The user has been emailed the reason and can register again; you are copied."
            ),
            "item": svc.serialize_request_for_faculty(approval),
        }
    )


class _OptionalTokenAuthentication(TokenAuthenticationWithInactivity):
    """A stale sign-in on the browser must not block the emailed link; it only identifies who is signed in."""

    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except AuthenticationFailed:
            return None


@api_view(["GET", "POST"])
@authentication_classes([_OptionalTokenAuthentication])
@permission_classes([AllowAny])
def registration_email_decision(request):
    """Approve / Decline buttons in the faculty email. GET shows the request; POST records the decision."""
    if not svc.schema_ready():
        return _schema_pending()
    data = request.data if request.method == "POST" else request.query_params
    raw = (data.get("token") or "").strip()
    viewer = request.user if getattr(request.user, "is_authenticated", False) else None
    try:
        row = svc.resolve_email_decision_token(raw, viewer=viewer, request=request)
    except svc.ApprovalError as err:
        return _error(err)
    if request.method == "GET":
        svc.record_view(
            faculty=row.faculty, approval=row.approval, channel=RegistrationApprovalChannel.EMAIL_LINK, request=request
        )
        return Response({"item": svc.serialize_email_decision(row)})
    decision = (data.get("decision") or "").strip().lower()
    try:
        approval = svc.faculty_decide(
            row.approval,
            faculty=row.faculty,
            decision=decision,
            reason=data.get("reason", ""),
            disclaimer_accepted=_bool(data.get("disclaimer_accepted")),
            disclaimer_version=data.get("disclaimer_version", ""),
            raw_token=raw,
            request=request,
        )
    except svc.ApprovalError as err:
        return _error(err)
    approved = approval.status == RegistrationApprovalStatus.APPROVED
    return Response(
        {
            "decision": "approved" if approved else "declined",
            "message": (
                "Approved. The account is now active and the applicant has been emailed. A copy has been sent to you."
                if approved
                else "Declined. The applicant has been emailed your reason and can register again. "
                "A copy has been sent to you."
            ),
            "account_removed": bool(getattr(approval, "_account_removed", False)),
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def faculty_extension_detail(request, ext_id: int):
    guard = _faculty_guard(request)
    if guard:
        return guard
    ext = RegistrationExtensionRequest.objects.select_related("user__department", "faculty").filter(pk=ext_id).first()
    refusal = _own_or_403(ext, request.user)
    if refusal:
        return refusal
    svc.record_view(faculty=request.user, extension=ext, request=request)
    return Response(svc.serialize_extension(ext, for_faculty=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def faculty_extension_decide(request, ext_id: int):
    guard = _faculty_guard(request)
    if guard:
        return guard
    ext = RegistrationExtensionRequest.objects.select_related("user").filter(pk=ext_id).first()
    data = request.data or {}
    raw = (data.get("token") or "").strip()
    if not raw:
        refusal = _own_or_403(ext, request.user)
        if refusal:
            return refusal
    elif ext is None:
        return _not_found()
    try:
        ext = svc.decide_extension(
            ext,
            actor=request.user,
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
            "message": (
                f"Extension granted until {svc._fmt_date(ext.approved_until)}. The user has been emailed and you are copied."
                if granted
                else "Extension declined. The user has been emailed the reason and you are copied."
            ),
            "item": svc.serialize_extension(ext),
        }
    )


# ---------------------------------------------------------------------------
# Users: programme validity and extension requests
# ---------------------------------------------------------------------------


def _validity_payload(user) -> dict:
    pending = svc.pending_extension(user)
    end = user.program_end_date
    today = timezone.localdate()
    return {
        "name": svc._name(user),
        "programme_validity": svc._iso(end),
        "expired": bool(end and today > end),
        "days_left": (end - today).days if end else None,
        "faculty": svc._person(user.supervisor) if user.supervisor_id else None,
        "can_request_extension": svc.can_request_extension(user),
        "extension_max_until": svc._iso(svc.extension_max_until(user, today)) if end else None,
        "extension_max_months": svc.EXTENSION_MAX_MONTHS,
        "pending_extension": svc.serialize_extension(pending) if pending else None,
    }


@api_view(["GET", "POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def extension_request_by_link(request):
    """Signed link from the expiry email or the sign-in page; the link only identifies the account."""
    if not svc.schema_ready():
        return _schema_pending()
    raw = (request.query_params.get("token") if request.method == "GET" else (request.data or {}).get("token")) or ""
    user = svc.read_user_extension_token(raw.strip())
    if user is None:
        return Response(
            {"error": "This link is not valid or has expired. Sign in again to get a new one.", "code": "token_invalid"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if request.method == "GET":
        return Response(_validity_payload(user))
    channel = (request.data or {}).get("channel") or RegistrationApprovalChannel.EMAIL_LINK
    if channel not in (RegistrationApprovalChannel.EMAIL_LINK, RegistrationApprovalChannel.LOGIN):
        channel = RegistrationApprovalChannel.EMAIL_LINK
    try:
        ext = svc.request_extension(user, reason=(request.data or {}).get("reason", ""), channel=channel, request=request)
    except svc.ApprovalError as err:
        return _error(err)
    return Response(
        {
            "message": _extension_sent_message(ext),
            "validity": _validity_payload(user),
        },
        status=status.HTTP_201_CREATED,
    )


def _extension_sent_message(ext) -> str:
    who = svc._name(ext.faculty) if ext.faculty_id else "the IIC administrator"
    return (
        f"Your extension request has been sent to {who}. An extension is valid for up to six months "
        f"(at most until {svc._fmt_date(ext.max_until)}). You will be emailed when it is decided."
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def my_programme_validity(request):
    if not svc.schema_ready():
        return _schema_pending()
    user = request.user
    if request.method == "GET":
        return Response(_validity_payload(user))
    try:
        ext = svc.request_extension(
            user, reason=(request.data or {}).get("reason", ""), channel=RegistrationApprovalChannel.PORTAL, request=request
        )
    except svc.ApprovalError as err:
        return _error(err)
    return Response({"message": _extension_sent_message(ext), "validity": _validity_payload(user)}, status=status.HTTP_201_CREATED)
