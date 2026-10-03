"""Wallet Payment Modes: department matrix, email recipients, direct wallet recharge (grants + recharge)."""

from __future__ import annotations

from datetime import datetime, time

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import permissions, status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users import wallet_payment_modes as svc
from iic_booking.users.mobile_sessions import client_ip
from iic_booking.users.models.department import Department, DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet_payment_modes import (
    DepartmentModeState,
    WalletDirectRecharge,
    WalletDirectRechargeGrant,
    WalletDirectRechargeMode,
    WalletModeOption,
    WalletPaymentModeAuditEvent,
)

User = get_user_model()

SCHEMA_PENDING_RESPONSE = {
    "error": "These settings are being installed. Try again in a few minutes.",
    "code": "SCHEMA_PENDING",
}


class IsMainAdministrator(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return bool(getattr(user, "is_staff", False) or getattr(user, "user_type", None) == UserType.ADMIN)


def _schema_pending() -> Response:
    return Response(SCHEMA_PENDING_RESPONSE, status=status.HTTP_503_SERVICE_UNAVAILABLE)


def _error(exc: svc.DirectRechargeError) -> Response:
    return Response({"error": exc.message, "code": exc.code, **exc.extra}, status=exc.status)


OPTION_META = [
    {
        "key": WalletModeOption.PROJECT_GRANT,
        "label": "Recharge via Project Grant",
        "description": "Faculty fund a department sub-wallet from an active project. Requests go to the SRIC Office.",
    },
    {
        "key": WalletModeOption.DIRECT_CASH,
        "label": "Direct Cash Deposit / Bank Transfer",
        "description": "Users deposit cash or transfer funds at the SRIC Bill Section and share the transaction number.",
    },
    {
        "key": WalletModeOption.ONLINE_GATEWAY,
        "label": "Online payment gateway",
        "description": "Instant sub-wallet recharge through Razorpay (card, UPI, net banking). Convenience fee applies.",
    },
    {
        "key": WalletModeOption.PEER_TRANSFER,
        "label": "Transfer within the same department",
        "description": "Faculty move funds from their sub-wallet to another eligible wallet under the same department grant.",
    },
    {
        "key": WalletModeOption.CREDIT,
        "label": "Credit Limit",
        "description": "Faculty request temporary wallet credit, approved by the Main Administrator within the caps.",
    },
    {
        "key": WalletModeOption.DIRECT_RECHARGE,
        "label": "Direct wallet recharge",
        "description": "The Main Administrator or a designated person adds funds directly to a selected wallet.",
    },
]


def _recipient_row(row) -> dict:
    return {
        "option": row.option,
        "department_id": row.department_id,
        "department_name": row.department.name if row.department_id else None,
        "to": list(row.to_recipients or []),
        "cc": list(row.cc_recipients or []),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": getattr(row.updated_by, "email", None),
    }


@api_view(["GET"])
@permission_classes([IsMainAdministrator])
def admin_wallet_payment_modes_overview(request):
    """Masters, department matrix, recipient configuration and option metadata in one payload."""
    masters = svc.master_states()
    states = svc.department_state_rows()
    departments = []
    for dept in svc.recharge_departments():
        row = states.get(dept.pk, {})
        dept_states = {o: row.get(o, DepartmentModeState.INHERIT) for o in svc.DEPARTMENT_STATE_OPTIONS}
        dept_states[WalletModeOption.CREDIT] = "enabled" if dept.enable_wallet_credit else "disabled"
        effective = {
            o: bool(masters[o]) and (
                dept.enable_wallet_credit if o == WalletModeOption.CREDIT else dept_states[o] != DepartmentModeState.DISABLED
            )
            for o in svc.OPTIONS
        }
        departments.append(
            {"id": dept.pk, "name": dept.name, "code": dept.code or "", "states": dept_states, "effective": effective}
        )
    return Response(
        {
            "schema_ready": svc.schema_ready(),
            "masters": masters,
            "options": OPTION_META,
            "departments": departments,
            "roles": [{"key": k, "label": v[0], "description": v[1]} for k, v in svc.ROLES.items()],
            "builtin_recipients": {o: {"to": v[0], "cc": v[1]} for o, v in svc.BUILTIN_RECIPIENTS.items()},
            "recipient_notes": svc.RECIPIENT_NOTES,
            "to_required_options": sorted(svc.TO_REQUIRED_OPTIONS),
            "recipients": [_recipient_row(r) for r in svc.recipient_rows()],
            "disabled_message": svc.AWAITING_APPROVAL_MESSAGE,
        }
    )


@api_view(["PATCH", "POST"])
@permission_classes([IsMainAdministrator])
def admin_wallet_payment_modes_departments(request):
    changes = request.data.get("changes")
    if not isinstance(changes, list) or not changes:
        return Response({"error": "Send a non-empty list of changes."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        applied = svc.set_department_states(changes, actor=request.user, ip=client_ip(request))
    except svc.SchemaPending:
        return _schema_pending()
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"applied": applied})


def _validated_recipients(request):
    option = request.data.get("option")
    if option not in svc.OPTIONS:
        return None, Response({"error": "Unknown option."}, status=status.HTTP_400_BAD_REQUEST)
    to, to_errors = svc.normalize_recipients(request.data.get("to") or [])
    cc, cc_errors = svc.normalize_recipients(request.data.get("cc") or [])
    errors = {}
    if to_errors:
        errors["to"] = to_errors
    if cc_errors:
        errors["cc"] = cc_errors
    if option in svc.TO_REQUIRED_OPTIONS:
        if not to:
            errors.setdefault("to", []).append("Add at least one recipient: To receives the Approve / Decline links.")
        if "role:wallet_owner" in to:
            errors.setdefault("to", []).append("The wallet owner cannot receive the Approve / Decline links.")
    cc = [c for c in cc if c not in to]
    if errors:
        return None, Response({"error": "Fix the recipient list.", "errors": errors}, status=status.HTTP_400_BAD_REQUEST)
    return (option, to, cc), None


@api_view(["PUT", "DELETE"])
@permission_classes([IsMainAdministrator])
def admin_wallet_payment_modes_recipients(request):
    ip = client_ip(request)
    if request.method == "DELETE":
        option = request.query_params.get("option") or request.data.get("option")
        if option not in svc.OPTIONS:
            return Response({"error": "Unknown option."}, status=status.HTTP_400_BAD_REQUEST)
        dept = request.query_params.get("department_id") or request.data.get("department_id")
        try:
            removed = svc.reset_recipients(option, dept, actor=request.user, ip=ip)
        except svc.SchemaPending:
            return _schema_pending()
        return Response({"removed": removed})

    cleaned, error = _validated_recipients(request)
    if error is not None:
        return error
    option, to, cc = cleaned
    try:
        row = svc.save_recipients(option, request.data.get("department_id"), to, cc, actor=request.user, ip=ip)
    except svc.SchemaPending:
        return _schema_pending()
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(_recipient_row(row))


@api_view(["GET"])
@permission_classes([IsMainAdministrator])
def admin_wallet_payment_modes_recipients_preview(request):
    option = request.query_params.get("option")
    if option not in svc.OPTIONS:
        return Response({"error": "Unknown option."}, status=status.HTTP_400_BAD_REQUEST)
    dept = request.query_params.get("department_id") or None
    resolved = svc.resolve_recipients(option, department=dept)
    return Response(
        {
            "option": option,
            "department_id": dept,
            "source": resolved.source,
            "to": resolved.configured_to,
            "cc": resolved.configured_cc,
        }
    )


@api_view(["GET"])
@permission_classes([IsMainAdministrator])
def admin_wallet_payment_modes_audit(request):
    try:
        rows = list(WalletPaymentModeAuditEvent.objects.select_related("actor")[:100])
    except svc.SCHEMA_ERRORS:
        rows = []
    return Response(
        {
            "events": [
                {
                    "id": e.pk,
                    "actor": getattr(e.actor, "email", None),
                    "action": e.action,
                    "target": e.target,
                    "before": e.before,
                    "after": e.after,
                    "ip_address": e.ip_address,
                    "created_at": e.created_at.isoformat(),
                }
                for e in rows
            ]
        }
    )


@api_view(["GET"])
@permission_classes([IsMainAdministrator])
def admin_wallet_payment_modes_user_search(request):
    from iic_booking.users.display import get_user_display_name

    q = (request.query_params.get("q") or "").strip()
    if len(q) < 2:
        return Response({"results": []})
    users = (
        User.objects.filter(is_active=True)
        .filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(emp_id__icontains=q))
        .select_related("department")
        .order_by("name")[:20]
    )
    return Response(
        {
            "results": [
                {
                    "id": u.pk,
                    "name": get_user_display_name(u) or u.email,
                    "email": u.email,
                    "user_type": u.user_type,
                    "department_name": u.department.name if u.department_id else "",
                }
                for u in users
            ]
        }
    )


def _parse_when(value, *, end_of_day: bool = False):
    if not value:
        return None
    dt = parse_datetime(str(value))
    if dt is None:
        d = parse_date(str(value))
        if d is None:
            return None
        dt = datetime.combine(d, time.max if end_of_day else time.min)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


@api_view(["GET", "POST"])
@permission_classes([IsMainAdministrator])
def admin_direct_recharge_grants(request):
    if request.method == "GET":
        try:
            grants = list(
                WalletDirectRechargeGrant.objects.select_related("user", "department", "granted_by", "revoked_by")
                .annotate(recharge_count=Count("recharges"))
                .order_by("-created_at")[:300]
            )
        except svc.SCHEMA_ERRORS:
            return Response({"grants": [], "schema_ready": False})
        return Response({"grants": [svc.serialize_grant(g) for g in grants], "schema_ready": True})

    data = request.data
    errors: dict[str, str] = {}
    grantee = User.objects.filter(pk=svc._department_id(data.get("user_id")), is_active=True).first()
    if grantee is None:
        errors["user_id"] = "Select an active user."
    valid_from = _parse_when(data.get("valid_from")) or timezone.now()
    valid_until = _parse_when(data.get("valid_until"), end_of_day=True)
    if valid_until is None:
        errors["valid_until"] = "Enter until when the permission is valid."
    elif valid_until <= valid_from:
        errors["valid_until"] = "Valid until must be after valid from."
    elif valid_until <= timezone.now():
        errors["valid_until"] = "Valid until must be in the future."
    department = None
    if data.get("department_id"):
        department = Department.objects.filter(
            pk=svc._department_id(data.get("department_id")), department_type=DepartmentType.INTERNAL
        ).first()
        if department is None:
            errors["department_id"] = "Department was not found."
    cap = None
    if data.get("max_amount_per_transaction") not in (None, ""):
        try:
            cap = svc._money(data.get("max_amount_per_transaction"))
        except svc.DirectRechargeError as exc:
            errors["max_amount_per_transaction"] = exc.message
    reason = str(data.get("reason") or "").strip()
    if len(reason) < 3:
        errors["reason"] = "Give the reason for this permission."
    if errors:
        return Response({"error": "Fix the highlighted fields.", "errors": errors}, status=status.HTTP_400_BAD_REQUEST)
    try:
        grant = WalletDirectRechargeGrant.objects.create(
            user=grantee,
            valid_from=valid_from,
            valid_until=valid_until,
            department=department,
            max_amount_per_transaction=cap,
            reason=reason[:2000],
            granted_by=request.user,
        )
    except svc.SCHEMA_ERRORS:
        return _schema_pending()
    svc.record_audit(
        request.user,
        "direct_recharge_grant_created",
        f"grant:{grant.pk}",
        {},
        {
            "user_id": grantee.pk,
            "valid_from": valid_from.isoformat(),
            "valid_until": valid_until.isoformat(),
            "department_id": department.pk if department else None,
            "max_amount_per_transaction": str(cap) if cap is not None else None,
        },
        client_ip(request),
    )
    svc.notify_grantee(grant)
    return Response(svc.serialize_grant(grant), status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsMainAdministrator])
def admin_direct_recharge_grant_revoke(request, grant_id: int):
    try:
        grant = WalletDirectRechargeGrant.objects.select_related("user", "department").filter(pk=grant_id).first()
    except svc.SCHEMA_ERRORS:
        return _schema_pending()
    if grant is None:
        return Response({"error": "Permission not found."}, status=status.HTTP_404_NOT_FOUND)
    if grant.revoked_at:
        return Response(svc.serialize_grant(grant))
    grant.revoked_at = timezone.now()
    grant.revoked_by = request.user
    grant.revoke_reason = str(request.data.get("reason") or "").strip()[:2000]
    grant.save(update_fields=["revoked_at", "revoked_by", "revoke_reason"])
    svc.record_audit(
        request.user, "direct_recharge_grant_revoked", f"grant:{grant.pk}", {}, {"reason": grant.revoke_reason},
        client_ip(request),
    )
    return Response(svc.serialize_grant(grant))


# ---------------------------------------------------------------------------
# Direct wallet recharge (Main Administrator or a designated person)
# ---------------------------------------------------------------------------


def _require_access(user):
    access = svc.direct_recharge_access(user)
    if access["allowed"]:
        return access, None
    if not access["enabled_globally"]:
        return access, Response(
            {"error": f"Direct wallet recharge: {svc.AWAITING_APPROVAL_MESSAGE}", "code": "DIRECT_RECHARGE_DISABLED"},
            status=status.HTTP_403_FORBIDDEN,
        )
    return access, Response(
        {
            "error": "Only the Main Administrator or a designated person can recharge wallets directly.",
            "code": "NOT_AUTHORISED",
        },
        status=status.HTTP_403_FORBIDDEN,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def direct_recharge_access_view(request):
    access = svc.direct_recharge_access(request.user)
    if access["allowed"]:
        access["departments"] = [
            {"id": d.pk, "name": d.name, "code": d.code or ""}
            for d in svc.recharge_departments()
            if svc.department_allows(WalletModeOption.DIRECT_RECHARGE, d.pk)
            and (access["is_main_admin"] or any(g["department_id"] in (None, d.pk) for g in access["grants"]))
        ]
    return Response(access)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def direct_recharge_wallet_search(request):
    _, denied = _require_access(request.user)
    if denied is not None:
        return denied
    return Response({"results": svc.search_wallet_owners(request.query_params.get("q") or "")})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def direct_recharge_preview(request):
    try:
        return Response(svc.preview_direct_recharge(request.user, request.data))
    except svc.DirectRechargeError as exc:
        return _error(exc)
    except svc.SCHEMA_ERRORS:
        return _schema_pending()


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def direct_recharge_create(request):
    attachment = request.FILES.get("attachment")
    try:
        record, created = svc.perform_direct_recharge(
            actor=request.user,
            data=request.data,
            attachment=attachment,
            ip=client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except svc.DirectRechargeError as exc:
        return _error(exc)
    except svc.SchemaPending:
        return _schema_pending()
    payload = svc.serialize_direct_recharge(record)
    payload["idempotent_replay"] = not created
    return Response(payload, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def direct_recharge_history(request):
    is_admin = svc.is_main_admin(request.user) or getattr(request.user, "is_staff", False)
    try:
        qs = WalletDirectRecharge.objects.select_related("wallet__user", "department", "performed_by")
        if not is_admin:
            qs = qs.filter(performed_by=request.user)
        params = request.query_params
        q = (params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(reference__icontains=q)
                | Q(reference_number__icontains=q)
                | Q(wallet__user__name__icontains=q)
                | Q(wallet__user__email__icontains=q)
                | Q(remarks__icontains=q)
            )
        if params.get("department_id"):
            qs = qs.filter(department_id=svc._department_id(params.get("department_id")))
        if params.get("mode") in WalletDirectRechargeMode.values:
            qs = qs.filter(mode=params.get("mode"))
        if params.get("performed_by"):
            qs = qs.filter(performed_by__email__icontains=params.get("performed_by").strip())
        date_from = parse_date(params.get("date_from") or "")
        date_to = parse_date(params.get("date_to") or "")
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        try:
            limit = max(1, min(int(params.get("limit") or 50), 200))
            offset = max(0, int(params.get("offset") or 0))
        except ValueError:
            limit, offset = 50, 0
        count = qs.count()
        rows = list(qs.order_by("-created_at")[offset : offset + limit])
    except svc.SCHEMA_ERRORS:
        return Response({"results": [], "count": 0, "schema_ready": False})
    return Response({"results": [svc.serialize_direct_recharge(r) for r in rows], "count": count, "schema_ready": True})
