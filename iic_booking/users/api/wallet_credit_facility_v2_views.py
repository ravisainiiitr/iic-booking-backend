"""API for administrator-controlled Wallet Credit Facility v2."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet_credit_facility import (
    WalletCreditFacility,
    WalletCreditFacilityStatus,
    WalletCreditPolicy,
)
from iic_booking.users.wallet_credit_facility_v2 import (
    WalletCreditError,
    approve_facility,
    assert_user_may_request_credit,
    build_profile_snapshot,
    create_and_submit_request,
    feature_enabled,
    mark_under_review,
    money,
    post_credit,
    reject_facility,
    repay_from_wallet,
    return_for_clarification,
    user_has_blocking_facility,
)


def _err(exc: WalletCreditError) -> Response:
    return Response({"error": exc.message, "code": exc.code}, status=exc.status)


def _is_main_admin(user) -> bool:
    return getattr(user, "user_type", None) == UserType.ADMIN


def _is_accounts(user) -> bool:
    return getattr(user, "user_type", None) == UserType.FINANCE


def _is_dept_admin(user) -> bool:
    return getattr(user, "user_type", None) == UserType.DEPT_ADMIN


def _serialize_facility(facility: WalletCreditFacility, *, include_profile: bool = False) -> dict:
    data = {
        "id": facility.id,
        "public_reference": facility.public_reference,
        "user_id": facility.user_id,
        "user_email": facility.user.email if facility.user_id else "",
        "user_name": facility.user.name if facility.user_id else "",
        "department_id": facility.department_id,
        "department_name": facility.department.name if facility.department_id else "",
        "requested_amount": str(money(facility.requested_amount)),
        "approved_amount": str(money(facility.approved_amount)) if facility.approved_amount is not None else None,
        "outstanding_amount": str(money(facility.outstanding_amount)),
        "purpose": facility.purpose,
        "remarks": facility.remarks,
        "status": facility.status,
        "requested_at": facility.requested_at.isoformat() if facility.requested_at else None,
        "submitted_at": facility.submitted_at.isoformat() if facility.submitted_at else None,
        "approved_at": facility.approved_at.isoformat() if facility.approved_at else None,
        "approved_by": facility.approved_by.email if facility.approved_by_id else None,
        "approval_reason": facility.approval_reason,
        "rejected_at": facility.rejected_at.isoformat() if facility.rejected_at else None,
        "rejection_reason": facility.rejection_reason,
        "due_date": facility.due_date.isoformat() if facility.due_date else None,
        "credited_at": facility.credited_at.isoformat() if facility.credited_at else None,
        "cleared_at": facility.cleared_at.isoformat() if facility.cleared_at else None,
    }
    if include_profile:
        data["channel_i_profile"] = facility.profile_snapshot or build_profile_snapshot(facility.user)
        data["audit_events"] = [
            {
                "action": e.action,
                "actor": e.actor.email if e.actor_id else None,
                "previous_value": e.previous_value,
                "new_value": e.new_value,
                "reason": e.reason,
                "created_at": e.created_at.isoformat(),
            }
            for e in facility.audit_events.all().order_by("created_at")
        ]
        data["ledger_entries"] = [
            {
                "kind": le.kind,
                "amount": str(money(le.amount)),
                "reference": le.reference,
                "description": le.description,
                "created_at": le.created_at.isoformat(),
            }
            for le in facility.ledger_entries.all().order_by("created_at")
        ]
        data["invoices"] = [
            {
                "invoice_number": inv.invoice_number,
                "status": inv.status,
                "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
                "due_date": inv.due_date.isoformat() if inv.due_date else None,
                "approved_credit": str(money(inv.approved_credit)),
                "amount_settled": str(money(inv.amount_settled)),
                "outstanding_amount": str(money(inv.outstanding_amount)),
            }
            for inv in facility.invoices.all().order_by("-created_at")
        ]
        data["payments"] = [
            {
                "receipt_number": p.receipt_number,
                "amount": str(money(p.amount)),
                "payment_date": p.payment_date.isoformat() if p.payment_date else None,
                "mode": p.mode,
                "utr_or_reference": p.utr_or_reference,
            }
            for p in facility.payments.all().order_by("-created_at")
        ]
    return data


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_credit_v2_summary(request):
    policy = WalletCreditPolicy.get_solo()
    enabled = feature_enabled()
    blocking = user_has_blocking_facility(request.user)
    eligibility = {"allowed": False, "code": "FEATURE_DISABLED", "message": "Feature disabled."}
    if enabled:
        try:
            assert_user_may_request_credit(request.user)
            if blocking:
                eligibility = {
                    "allowed": False,
                    "code": "ACTIVE_CREDIT_EXISTS",
                    "message": (
                        f"You already have outstanding/active credit {blocking.public_reference}."
                    ),
                }
            else:
                eligibility = {"allowed": True, "code": "OK", "message": "Eligible to request credit."}
        except WalletCreditError as exc:
            eligibility = {"allowed": False, "code": exc.code, "message": exc.message}
    wallet_balance = "0.00"
    try:
        from iic_booking.users.models.wallet import Wallet

        w = Wallet.objects.filter(user=request.user).first()
        if w:
            wallet_balance = str(money(w.total_balance))
    except Exception:
        pass
    return Response(
        {
            "feature_enabled": enabled,
            "policy": {
                "max_credit_amount": str(money(policy.max_credit_amount)),
                "min_request_amount": str(money(policy.min_request_amount)),
                "max_outstanding_amount": str(money(policy.max_outstanding_amount)),
                "max_credit_duration_days": policy.max_credit_duration_days,
            },
            "current_wallet_balance": wallet_balance,
            "existing_outstanding_credit": str(money(blocking.outstanding_amount)) if blocking else "0.00",
            "active_facility_reference": blocking.public_reference if blocking else None,
            "eligibility": eligibility,
            "notice": "Credit is subject to Main Administrator approval.",
        }
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def wallet_credit_v2_list_or_create(request):
    if request.method == "GET":
        qs = WalletCreditFacility.objects.filter(user=request.user).order_by("-created_at")
        return Response({"results": [_serialize_facility(f) for f in qs[:100]]})
    if not feature_enabled():
        return Response({"error": "Feature disabled.", "code": "FEATURE_DISABLED"}, status=403)
    try:
        facility = create_and_submit_request(
            user=request.user,
            requested_amount=request.data.get("requested_amount"),
            purpose=request.data.get("purpose") or "",
            remarks=request.data.get("remarks") or "",
            department_id=request.data.get("department_id"),
        )
    except WalletCreditError as exc:
        return _err(exc)
    return Response(_serialize_facility(facility, include_profile=True), status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_credit_v2_detail(request, facility_id: int):
    facility = get_object_or_404(WalletCreditFacility, pk=facility_id)
    if facility.user_id != request.user.id and not (
        _is_main_admin(request.user) or _is_accounts(request.user) or _is_dept_admin(request.user)
    ):
        return Response({"error": "Not found.", "code": "NOT_FOUND"}, status=404)
    if _is_dept_admin(request.user) and facility.user.department_id != request.user.department_id:
        return Response({"error": "Not found.", "code": "NOT_FOUND"}, status=404)
    return Response(_serialize_facility(facility, include_profile=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_credit_v2_repay(request, facility_id: int):
    facility = get_object_or_404(WalletCreditFacility, pk=facility_id)
    try:
        payment = repay_from_wallet(
            facility=facility,
            actor=request.user,
            amount=request.data.get("amount"),
            mode=request.data.get("mode") or "wallet_debit",
            utr_or_reference=request.data.get("utr_or_reference") or "",
            remarks=request.data.get("remarks") or "",
        )
    except WalletCreditError as exc:
        return _err(exc)
    except ValueError as exc:
        return Response({"error": str(exc), "code": "WALLET_ERROR"}, status=400)
    facility.refresh_from_db()
    return Response(
        {
            "payment": {
                "receipt_number": payment.receipt_number,
                "amount": str(money(payment.amount)),
            },
            "facility": _serialize_facility(facility),
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_wallet_credit_v2_list(request):
    if not (_is_main_admin(request.user) or _is_accounts(request.user) or _is_dept_admin(request.user)):
        return Response({"error": "Forbidden.", "code": "FORBIDDEN"}, status=403)
    qs = WalletCreditFacility.objects.select_related("user", "department").order_by("-created_at")
    status_q = (request.GET.get("status") or "").strip()
    if status_q:
        qs = qs.filter(status=status_q)
    if _is_dept_admin(request.user) and not _is_main_admin(request.user):
        qs = qs.filter(user__department_id=request.user.department_id)
    emp = (request.GET.get("employee_id") or "").strip()
    if emp:
        qs = qs.filter(user__emp_id=emp)
    user_q = (request.GET.get("user") or "").strip()
    if user_q:
        from django.db.models import Q

        qs = qs.filter(Q(user__email__icontains=user_q) | Q(user__name__icontains=user_q))
    return Response(
        {
            "counts": {
                s: WalletCreditFacility.objects.filter(status=s).count()
                for s in WalletCreditFacilityStatus.values
            },
            "results": [_serialize_facility(f) for f in qs[:200]],
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_wallet_credit_v2_detail(request, facility_id: int):
    if not (_is_main_admin(request.user) or _is_accounts(request.user) or _is_dept_admin(request.user)):
        return Response({"error": "Forbidden.", "code": "FORBIDDEN"}, status=403)
    facility = get_object_or_404(
        WalletCreditFacility.objects.select_related("user", "department", "approved_by"),
        pk=facility_id,
    )
    if _is_dept_admin(request.user) and not _is_main_admin(request.user):
        if facility.user.department_id != request.user.department_id:
            return Response({"error": "Not found.", "code": "NOT_FOUND"}, status=404)
    if _is_main_admin(request.user) and facility.status == WalletCreditFacilityStatus.SUBMITTED:
        try:
            facility = mark_under_review(facility, request.user)
        except WalletCreditError:
            pass
    return Response(_serialize_facility(facility, include_profile=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_wallet_credit_v2_approve(request, facility_id: int):
    facility = get_object_or_404(WalletCreditFacility, pk=facility_id)
    due_raw = request.data.get("due_date")
    due_date = None
    if due_raw:
        due_date = datetime.strptime(str(due_raw)[:10], "%Y-%m-%d").date()
    approved_amount = request.data.get("approved_amount")
    if approved_amount in (None, ""):
        approved_amount = facility.requested_amount
    try:
        facility = approve_facility(
            facility=facility,
            actor=request.user,
            approved_amount=approved_amount,
            due_date=due_date,
            reason=request.data.get("reason") or request.data.get("approval_reason") or "",
        )
        if request.data.get("post_credit") in (True, "true", "1", 1):
            facility = post_credit(facility=facility, actor=request.user)
    except WalletCreditError as exc:
        return _err(exc)
    return Response(_serialize_facility(facility, include_profile=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_wallet_credit_v2_reject(request, facility_id: int):
    facility = get_object_or_404(WalletCreditFacility, pk=facility_id)
    try:
        facility = reject_facility(
            facility=facility,
            actor=request.user,
            reason=request.data.get("reason") or "",
        )
    except WalletCreditError as exc:
        return _err(exc)
    return Response(_serialize_facility(facility, include_profile=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_wallet_credit_v2_clarification(request, facility_id: int):
    facility = get_object_or_404(WalletCreditFacility, pk=facility_id)
    try:
        facility = return_for_clarification(
            facility=facility,
            actor=request.user,
            reason=request.data.get("reason") or "",
        )
    except WalletCreditError as exc:
        return _err(exc)
    return Response(_serialize_facility(facility, include_profile=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_wallet_credit_v2_post_credit(request, facility_id: int):
    facility = get_object_or_404(WalletCreditFacility, pk=facility_id)
    try:
        facility = post_credit(facility=facility, actor=request.user)
    except WalletCreditError as exc:
        return _err(exc)
    return Response(_serialize_facility(facility, include_profile=True))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_credit_v2_invoice_pdf(request, facility_id: int):
    facility = get_object_or_404(WalletCreditFacility, pk=facility_id)
    if facility.user_id != request.user.id and not (
        _is_main_admin(request.user) or _is_accounts(request.user)
    ):
        return Response({"error": "Not found.", "code": "NOT_FOUND"}, status=404)
    inv = facility.invoices.exclude(status="CANCELLED").order_by("-id").first()
    if not inv:
        return Response({"error": "No invoice.", "code": "NO_INVOICE"}, status=404)
    from django.http import HttpResponse
    from iic_booking.users.wallet_credit_invoice_pdf import build_credit_invoice_pdf

    pdf = build_credit_invoice_pdf(inv)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{inv.invoice_number}.pdf"'
    return resp


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_wallet_credit_v2_reconcile(request):
    if not (_is_main_admin(request.user) or _is_accounts(request.user)):
        return Response({"error": "Forbidden.", "code": "FORBIDDEN"}, status=403)
    from iic_booking.users.wallet_credit_facility_v2 import reconcile_facility

    rows = [reconcile_facility(f) for f in WalletCreditFacility.objects.all().order_by("id")[:500]]
    return Response(
        {
            "total": len(rows),
            "inconsistent": sum(1 for r in rows if not r["consistent"]),
            "results": rows,
            "read_only": True,
        }
    )