"""API for external users' wallet recharge against an offline payment (see ``iic_booking.users.offline_recharge``)."""

from __future__ import annotations

from django.db.models import Count, Q
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users import offline_recharge as svc
from iic_booking.users.models.offline_recharge import OfflinePaymentMode, OfflineReconciliationStatus
from iic_booking.users.models.wallet import WalletRechargeMode, WalletRechargeRequest, WalletRechargeRequestStatus


def _error(exc: svc.OfflineRechargeError) -> Response:
    body = {"error": exc.message, "code": exc.code}
    if exc.field:
        body["field"] = exc.field
    return Response(body, status=exc.status)


def _own_qs(user):
    return (
        WalletRechargeRequest.objects.filter(
            user=user, recharge_mode=WalletRechargeMode.OFFLINE_PAYMENT, is_deleted=False
        )
        .select_related("user", "department", "offline_payment", "offline_payment__receipt", "processed_by")
        .order_by("-created_at")
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def offline_recharge_collection(request):
    """GET: the signed-in user's offline payment recharges. POST (multipart): submit one with its receipt."""
    if request.method == "GET":
        rows = [svc.serialize(r, viewer=request.user) for r in _own_qs(request.user)[:200]]
        return Response(
            {
                "available": svc.feature_open_for(request.user),
                "payment_modes": [{"value": v, "label": str(label)} for v, label in OfflinePaymentMode.choices],
                "max_receipt_mb": svc.MAX_RECEIPT_BYTES // (1024 * 1024),
                "requests": rows,
            }
        )
    upload = request.FILES.get("receipt_file") or request.FILES.get("file")
    try:
        result = svc.submit(request.user, request.data, upload)
    except svc.OfflineRechargeError as exc:
        return _error(exc)
    return Response(
        {
            "request": svc.serialize(result.request, viewer=request.user),
            "created": result.created,
            "message": (
                f"Request {result.request.transaction_number} submitted. It will be credited to your wallet once approved."
                if result.created
                else "This request was already submitted."
            ),
        },
        status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def offline_recharge_detail(request, request_id: int):
    """One request with its audit trail: to its owner, or to an approver whose scope covers it."""
    req = (
        _own_qs(request.user).filter(pk=request_id).first()
        or svc.scoped_queryset(request.user).filter(pk=request_id).first()
    )
    if req is None:
        return Response({"error": "Request not found.", "code": "not_found"}, status=status.HTTP_404_NOT_FOUND)
    return Response({"request": svc.serialize(req, viewer=request.user, include_audit=True)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def offline_recharge_cancel(request, request_id: int):
    try:
        req = svc.cancel(request_id, request.user)
    except svc.OfflineRechargeError as exc:
        return _error(exc)
    return Response({"request": svc.serialize(req, viewer=request.user), "message": "Request cancelled."})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def offline_recharge_queue(request):
    """Approvers: requests in their scope, filtered by state, with a count per state."""
    role = svc.approver_role(request.user)
    if not role:
        return Response({"error": "Only approvers can view offline payment recharges.", "code": "forbidden"}, status=403)
    base = svc.scoped_queryset(request.user)
    state = (request.query_params.get("state") or "pending").strip().lower()
    q = (request.query_params.get("q") or "").strip()
    qs = svc.filter_by_state(base, state) if state != "all" else base
    if q:
        qs = qs.filter(
            Q(user__email__icontains=q)
            | Q(user__name__icontains=q)
            | Q(offline_payment__transaction_reference__icontains=q.replace(" ", ""))
        )
    counts = base.aggregate(
        pending=Count("pk", filter=Q(status=WalletRechargeRequestStatus.PENDING)),
        approved=Count(
            "pk",
            filter=Q(
                status=WalletRechargeRequestStatus.APPROVED,
                offline_payment__reconciliation_status=OfflineReconciliationStatus.AWAITING,
            ),
        ),
        mismatch=Count("pk", filter=Q(offline_payment__reconciliation_status=OfflineReconciliationStatus.MISMATCH)),
        reconciled=Count("pk", filter=Q(offline_payment__reconciliation_status=OfflineReconciliationStatus.RECONCILED)),
        rejected=Count("pk", filter=Q(status=WalletRechargeRequestStatus.REJECTED)),
    )
    return Response(
        {
            "role": role,
            "state": state,
            "counts": counts,
            "requests": [svc.serialize(r, viewer=request.user) for r in qs[:300]],
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def offline_recharge_approve(request, request_id: int):
    try:
        req = svc.approve(
            request_id, request.user, note=request.data.get("note"), approved_amount=request.data.get("approved_amount")
        )
    except svc.OfflineRechargeError as exc:
        return _error(exc)
    return Response({"request": svc.serialize(req, viewer=request.user), "message": "Approved and credited to the wallet."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def offline_recharge_reject(request, request_id: int):
    try:
        req = svc.reject(request_id, request.user, note=request.data.get("note"))
    except svc.OfflineRechargeError as exc:
        return _error(exc)
    return Response({"request": svc.serialize(req, viewer=request.user), "message": "Rejected."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def offline_recharge_reconcile(request, request_id: int):
    try:
        req = svc.reconcile(
            request_id,
            request.user,
            outcome=request.data.get("outcome"),
            reference=request.data.get("reference") or "",
            credit_date=request.data.get("credit_date"),
            note=request.data.get("note") or "",
        )
    except svc.OfflineRechargeError as exc:
        return _error(exc)
    return Response({"request": svc.serialize(req, viewer=request.user), "message": "Reconciliation saved."})
