"""SRIC wallet recharge API: faculty see their own SRIC credits and can refresh; the Main Administrator reviews."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users import sric_wallet_recharge as svc
from iic_booking.users.models import Department, User, UserType
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.sric_wallet_recharge import (
    CREDITABLE_STATUSES,
    SricReceiverMapping,
    SricWalletMailMessage,
    SricWalletRecharge,
    SricWalletRechargeSettings,
    SricWalletRechargeStatus,
)

S = SricWalletRechargeStatus


class IsMainAdministrator(permissions.BasePermission):
    def has_permission(self, request, view):
        from iic_booking.users.admin_wallet_ledger import is_main_admin

        return is_main_admin(request.user)


def _money(value) -> str | None:
    return f"{value:.2f}" if value is not None else None


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _name(user) -> str:
    from iic_booking.users.display import get_user_display_name

    return (get_user_display_name(user) or user.email) if user else ""


def serialize_row(rec: SricWalletRecharge, *, admin: bool) -> dict:
    data = {
        "id": rec.pk,
        "reference": rec.reference,
        "project_number": rec.project_number,
        "ledger_id": rec.ledger_id,
        "receiver_code": rec.receiver_code,
        "receiver_label": svc.receiver_label(rec),
        "department_id": rec.department_id,
        "department_name": rec.department.name if rec.department else "",
        "amount": _money(rec.amount),
        "amount_raw": rec.amount_raw,
        "financial_year": rec.financial_year,
        "status": rec.status,
        "status_display": rec.get_status_display(),
        "review_reason": rec.review_reason,
        "review_message": rec.review_message,
        "credited_at": _iso(rec.credited_at),
        "balance_after": _money(rec.balance_after) if rec.status == S.CREDITED else None,
        "email_date": _iso(rec.message.received_at) if rec.message_id and rec.message else None,
        "created_at": _iso(rec.created_at),
        "fund_receipt_verified": rec.fund_receipt_verified,
    }
    if not admin:
        return data
    data.update(
        {
            "pi_name": rec.pi_name,
            "employee_id": rec.employee_id,
            "row_number": rec.row_number,
            "origin_verified": rec.origin_verified,
            "auth_verdict": rec.message.auth_verdict if rec.message_id and rec.message else "",
            "matched_user": (
                {
                    "id": rec.matched_user.pk,
                    "name": _name(rec.matched_user),
                    "email": rec.matched_user.email,
                    "emp_id": rec.matched_user.emp_id or "",
                }
                if rec.matched_user
                else None
            ),
            "sub_wallet_id": rec.sub_wallet_id,
            "wallet_transaction_id": rec.wallet_transaction_id,
            "credited_by_name": _name(rec.credited_by) if rec.credited_by_id else ("System (auto-credit)" if rec.credited_at else ""),
            "confirmation_sent_at": _iso(rec.confirmation_sent_at),
            "duplicate_of_id": rec.duplicate_of_id,
            "duplicate_of_reference": rec.duplicate_of.reference if rec.duplicate_of_id and rec.duplicate_of else "",
            "fund_receipt_verified_by_name": _name(rec.fund_receipt_verified_by) if rec.fund_receipt_verified_by_id else "",
            "fund_receipt_verified_at": _iso(rec.fund_receipt_verified_at),
            "fund_receipt_verification_remarks": rec.fund_receipt_verification_remarks,
            "rejection_reason": rec.rejection_reason,
            "rejected_at": _iso(rec.rejected_at),
            "rejected_by_name": _name(rec.rejected_by) if rec.rejected_by_id else "",
            "can_credit": rec.status in CREDITABLE_STATUSES and not svc._missing_ledger(rec.ledger_id),
            "can_reject": rec.status in CREDITABLE_STATUSES,
            "can_verify": rec.status == S.CREDITED,
            "history": rec.history or [],
        }
    )
    return data


def _rows_qs():
    return SricWalletRecharge.objects.select_related(
        "message",
        "matched_user",
        "department",
        "receiver_mapping",
        "credited_by",
        "fund_receipt_verified_by",
        "rejected_by",
        "duplicate_of",
    )


def _error(exc: svc.SricRechargeError) -> Response:
    body = {"error": exc.message, "code": exc.code}
    if exc.retry_after:
        body["retry_after"] = exc.retry_after
    resp = Response(body, status=exc.status)
    if exc.retry_after:
        resp["Retry-After"] = str(exc.retry_after)
    return resp


# --- Faculty / users ------------------------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_sric_recharges(request):
    rows = _rows_qs().filter(matched_user=request.user).exclude(status=S.DUPLICATE).order_by("-created_at")[:20]
    return Response({**svc.portal_info(), "results": [serialize_row(r, admin=False) for r in rows]})


def _refresh_response(request, *, admin: bool, per_user_seconds: int) -> Response:
    try:
        result = svc.refresh(request.user, per_user_seconds=per_user_seconds)
    except svc.SricRechargeError as exc:
        return _error(exc)
    scan = result["scan"]
    mine = result["rows"]
    credited = [r for r in mine if r.status == S.CREDITED]
    if scan.get("status") == "disabled":
        message = "Reading SRIC recharge emails is switched off at the moment."
    elif scan.get("status") == "not_configured":
        message = "The portal mailbox is not configured."
    elif scan.get("status") == "error":
        message = "Could not reach the portal mailbox. Please try again in a few minutes."
    elif credited:
        message = "Credited " + "; ".join(
            f"₹{r.amount:,.2f} (ledger {r.ledger_id})" for r in credited
        )
    elif mine:
        message = f"{len(mine)} new SRIC recharge(s) received; they are being checked before credit."
    elif scan.get("status") == "already_running":
        message = "A sync is already running. Your list will update in a moment."
    else:
        message = "No new recharges."
    body = {
        "status": scan.get("status"),
        "debounced": bool(scan.get("debounced")),
        "message": message,
        "results": [serialize_row(r, admin=admin) for r in mine],
    }
    if admin:
        body["scan"] = scan
    return Response(body)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def refresh_my_sric_recharges(request):
    if not request.user.can_have_wallet():
        return Response({"error": "Only wallet owners can sync SRIC recharges."}, status=status.HTTP_403_FORBIDDEN)
    return _refresh_response(request, admin=False, per_user_seconds=svc.USER_REFRESH_SECONDS)


# --- Main Administrator ---------------------------------------------------------------------------------------


def _day(value, *, end: bool = False):
    d = parse_date(value or "")
    if not d:
        return None
    dt = timezone.make_aware(datetime.combine(d, time.min))
    return dt + timedelta(days=1) if end else dt


def filter_rows(qs, params):
    status_ = (params.get("status") or "").strip()
    if status_ in S.values:
        qs = qs.filter(status=status_)
    fy = (params.get("financial_year") or "").strip()
    if fy:
        qs = qs.filter(financial_year=fy)
    receiver = (params.get("receiver") or "").strip().upper()
    if receiver:
        qs = qs.filter(receiver_code=receiver)
    verified = (params.get("verified") or "").strip().lower()
    if verified in ("1", "true", "yes", "verified"):
        qs = qs.filter(fund_receipt_verified=True)
    elif verified in ("0", "false", "no", "not_verified"):
        qs = qs.filter(fund_receipt_verified=False)
    start = _day(params.get("date_from"))
    if start:
        qs = qs.filter(created_at__gte=start)
    end = _day(params.get("date_to"), end=True)
    if end:
        qs = qs.filter(created_at__lt=end)
    search = (params.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(ledger_id__icontains=search)
            | Q(project_number__icontains=search)
            | Q(employee_id__icontains=search)
            | Q(pi_name__icontains=search)
            | Q(matched_user__email__icontains=search)
        )
    return qs


@api_view(["GET"])
@permission_classes([IsMainAdministrator])
def admin_list(request):
    params = request.query_params
    qs = filter_rows(_rows_qs(), params).order_by("-created_at", "-id")
    try:
        page = max(1, int(params.get("page") or 1))
        size = min(200, max(1, int(params.get("page_size") or 25)))
    except ValueError:
        page, size = 1, 25
    total = qs.count()
    rows = list(qs[(page - 1) * size : page * size])
    counts = dict(SricWalletRecharge.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
    years = list(
        SricWalletRecharge.objects.order_by("-financial_year").values_list("financial_year", flat=True).distinct()
    )
    config = SricWalletRechargeSettings.get_singleton()
    return Response(
        {
            "results": [serialize_row(r, admin=True) for r in rows],
            "count": total,
            "page": page,
            "page_size": size,
            "status_counts": counts,
            "financial_years": years,
            "receivers": [{"code": m.code, "label": m.label} for m in SricReceiverMapping.objects.all()],
            "credited_total": _money(qs.filter(status=S.CREDITED).aggregate(t=Sum("amount"))["t"] or 0),
            "scan_enabled": config.scan_enabled,
            "auto_credit_enabled": config.auto_credit_enabled,
            "last_scan_at": _iso(config.last_scan_at),
            "last_scan_result": config.last_scan_result or {},
        }
    )


def _row_or_404(row_id):
    return _rows_qs().filter(pk=row_id).first()


@api_view(["POST"])
@permission_classes([IsMainAdministrator])
def admin_credit(request, row_id: int):
    rec = _row_or_404(row_id)
    if rec is None:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    user = None
    mapping = None
    if request.data.get("user_id"):
        user = User.objects.filter(pk=request.data.get("user_id"), is_active=True).first()
        if user is None:
            return Response({"error": "Select an active user.", "code": "USER_NOT_FOUND"}, status=400)
    if request.data.get("receiver_code"):
        mapping = svc.receiver_mapping(str(request.data.get("receiver_code")))
        if mapping is None or mapping.department_id is None:
            return Response({"error": "Select a receiver that has a department sub-wallet.", "code": "RECEIVER_NOT_FOUND"}, status=400)
    try:
        rec, created = svc.credit_row(rec.pk, actor=request.user, user=user, mapping=mapping, note=str(request.data.get("note") or "")[:500])
    except svc.SricRechargeError as exc:
        return _error(exc)
    if not created:
        return Response({"error": "This row is already credited.", "code": "ALREADY_CREDITED", "row": serialize_row(_row_or_404(rec.pk), admin=True)}, status=409)
    return Response({"message": f"Credited ₹{rec.amount:,.2f}.", "row": serialize_row(_row_or_404(rec.pk), admin=True)})


@api_view(["POST"])
@permission_classes([IsMainAdministrator])
def admin_credit_ready(request):
    ids = request.data.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return Response({"error": "Select the rows to credit."}, status=400)
    ready = list(
        SricWalletRecharge.objects.filter(pk__in=[int(i) for i in ids if str(i).isdigit()][:200], status=S.AWAITING_CREDIT).values_list("pk", flat=True)
    )
    credited, errors = 0, []
    for pk in ready:
        try:
            _, created = svc.credit_row(pk, actor=request.user, note="bulk credit")
            credited += int(created)
        except svc.SricRechargeError as exc:
            errors.append({"id": pk, "error": exc.message})
    return Response({"credited": credited, "skipped": len(ids) - len(ready), "errors": errors})


@api_view(["POST"])
@permission_classes([IsMainAdministrator])
def admin_reject(request, row_id: int):
    if _row_or_404(row_id) is None:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    try:
        rec = svc.reject_row(row_id, actor=request.user, reason=str(request.data.get("reason") or ""))
    except svc.SricRechargeError as exc:
        return _error(exc)
    return Response({"message": "Row rejected.", "row": serialize_row(_row_or_404(rec.pk), admin=True)})


@api_view(["POST"])
@permission_classes([IsMainAdministrator])
def admin_verify(request, row_id: int):
    if _row_or_404(row_id) is None:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    verified = request.data.get("verified", True)
    if isinstance(verified, str):
        verified = verified.strip().lower() not in ("0", "false", "no", "off", "")
    try:
        rec = svc.set_verification(row_id, actor=request.user, verified=bool(verified), remarks=str(request.data.get("remarks") or ""))
    except svc.SricRechargeError as exc:
        return _error(exc)
    return Response({"message": "Fund receipt verified." if rec.fund_receipt_verified else "Marked as not verified.", "row": serialize_row(_row_or_404(rec.pk), admin=True)})


@api_view(["POST"])
@permission_classes([IsMainAdministrator])
def admin_refresh(request):
    return _refresh_response(request, admin=True, per_user_seconds=15)


@api_view(["GET"])
@permission_classes([IsMainAdministrator])
def admin_user_lookup(request):
    q = (request.query_params.get("q") or "").strip()
    if len(q) < 2:
        return Response({"results": []})
    users = {u.pk: u for u in svc.match_employee(q)}
    for u in User.objects.filter(is_active=True).filter(Q(email__icontains=q) | Q(name__icontains=q) | Q(emp_id__icontains=q)).order_by("name")[:15]:
        users.setdefault(u.pk, u)
    rows = User.objects.filter(pk__in=list(users)).select_related("department").order_by("name")[:20]
    return Response(
        {
            "results": [
                {
                    "id": u.pk,
                    "name": _name(u),
                    "email": u.email,
                    "emp_id": u.emp_id or "",
                    "user_type": u.user_type,
                    "is_faculty": u.user_type == UserType.FACULTY,
                    "department_name": u.department.name if u.department_id else "",
                }
                for u in rows
            ]
        }
    )


SETTINGS_FIELDS = (
    "scan_enabled",
    "auto_credit_enabled",
    "sender_email",
    "attachment_name",
    "auto_credit_max_amount",
    "trusted_authserv_ids",
    "require_internal_relay",
    "trusted_relay_ranges",
    "gateway_marker_header",
    "gateway_marker_value",
    "confirmation_cc_emails",
    "review_alert_emails",
)


def _settings_payload() -> dict:
    config = SricWalletRechargeSettings.get_singleton()
    data = {f: getattr(config, f) for f in SETTINGS_FIELDS}
    data["auto_credit_max_amount"] = _money(config.auto_credit_max_amount)
    data["last_scan_at"] = _iso(config.last_scan_at)
    data["last_scan_result"] = config.last_scan_result or {}
    data["mappings"] = [
        {
            "id": m.pk,
            "code": m.code,
            "label": m.label,
            "department_id": m.department_id,
            "department_name": m.department.name if m.department else "",
            "is_active": m.is_active,
        }
        for m in SricReceiverMapping.objects.select_related("department").order_by("code")
    ]
    data["departments"] = [
        {"id": d.pk, "name": d.name, "code": d.code}
        for d in Department.objects.filter(department_type=DepartmentType.INTERNAL).order_by("name")
    ]
    data["recent_messages"] = [
        {
            "id": m.pk,
            "received_at": _iso(m.received_at),
            "processed_at": _iso(m.processed_at),
            "status": m.status,
            "status_display": m.get_status_display(),
            "row_count": m.row_count,
            "authenticated": m.authenticated,
            "auth_verdict": m.auth_verdict,
            "attachment_name": m.attachment_name,
        }
        for m in SricWalletMailMessage.objects.order_by("-processed_at")[:10]
    ]
    return data


@api_view(["GET", "PATCH"])
@permission_classes([IsMainAdministrator])
def admin_settings(request):
    if request.method == "PATCH":
        from decimal import Decimal, InvalidOperation

        config = SricWalletRechargeSettings.get_singleton()
        changed = []
        for field in SETTINGS_FIELDS:
            if field not in request.data:
                continue
            value = request.data.get(field)
            if field in ("scan_enabled", "auto_credit_enabled", "require_internal_relay"):
                value = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
            elif field == "auto_credit_max_amount":
                if value in (None, ""):
                    value = None
                else:
                    try:
                        value = Decimal(str(value).replace(",", "")).quantize(Decimal("0.01"))
                    except InvalidOperation:
                        return Response({"error": "Enter a valid auto-credit limit."}, status=400)
                    if value <= 0:
                        return Response({"error": "The auto-credit limit must be more than zero."}, status=400)
            else:
                value = str(value or "").strip()
                if field in ("sender_email", "attachment_name") and not value:
                    return Response({"error": "Sender address and attachment name cannot be empty."}, status=400)
            setattr(config, field, value)
            changed.append(field)
        if changed:
            config.updated_by = request.user
            config.save(update_fields=changed + ["updated_by", "updated_at"])
    return Response(_settings_payload())


@api_view(["POST"])
@permission_classes([IsMainAdministrator])
def admin_mappings(request):
    """Create or update a receiver mapping (by id or code)."""
    code = str(request.data.get("code") or "").strip().upper()
    label = str(request.data.get("label") or "").strip()
    mapping = None
    if request.data.get("id"):
        mapping = SricReceiverMapping.objects.filter(pk=request.data.get("id")).first()
        if mapping is None:
            return Response({"error": "Not found."}, status=404)
    if mapping is None:
        if not code or not label:
            return Response({"error": "Enter the Receiver Project code and the receiver type."}, status=400)
        if SricReceiverMapping.objects.filter(code=code).exists():
            return Response({"error": "This Receiver Project code is already mapped."}, status=400)
        mapping = SricReceiverMapping(code=code)
    if label:
        mapping.label = label[:120]
    if code and code != mapping.code:
        if SricReceiverMapping.objects.filter(code=code).exclude(pk=mapping.pk).exists():
            return Response({"error": "This Receiver Project code is already mapped."}, status=400)
        mapping.code = code
    if "department_id" in request.data:
        dept_id = request.data.get("department_id")
        if dept_id in (None, ""):
            mapping.department = None
        else:
            dept = Department.objects.filter(pk=dept_id, department_type=DepartmentType.INTERNAL).first()
            if dept is None:
                return Response({"error": "Select an internal department."}, status=400)
            mapping.department = dept
    if "is_active" in request.data:
        mapping.is_active = bool(request.data.get("is_active"))
    mapping.updated_by = request.user
    mapping.save()
    return Response(_settings_payload())
