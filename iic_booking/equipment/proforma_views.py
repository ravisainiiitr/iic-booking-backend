"""API for saved proforma invoices: the user's own list with pay / edit / cancel, and the staff reconciliation list."""

import csv
import io

from django.http import HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import proforma_store as store
from .proforma_models import SavedProformaInvoice

STATES = tuple(store.STATE_LABELS)


def _error(exc: store.ProformaError) -> Response:
    return Response(exc.payload(), status=exc.status)


def _filtered(qs, request):
    department = request.query_params.get("department")
    if department and str(department).isdigit():
        qs = qs.filter(department_id=int(department))
    q = (request.query_params.get("q") or "").strip()
    if q:
        from django.db.models import Q

        qs = qs.filter(Q(number__icontains=q) | Q(buyer__name__icontains=q) | Q(buyer__email__icontains=q)
                       | Q(user__email__icontains=q))
    rows = [store.serialize(r) for r in qs[:500]]
    state = (request.query_params.get("state") or "").strip()
    if state in STATES:
        rows = [r for r in rows if r["state"]["key"] == state]
    return rows


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_proformas(request):
    rows = _filtered(store.own_queryset(request.user), request)
    return Response({"results": rows, "states": store.STATE_LABELS})


def _own_or_staff(request, proforma_id):
    record = store.own_queryset(request.user).filter(pk=proforma_id).first()
    if record is None:
        record = store.staff_queryset(request.user).filter(pk=proforma_id).first()
    return record


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def proforma_detail(request, proforma_id: int):
    record = _own_or_staff(request, proforma_id)
    if record is None:
        return Response({"error": "Proforma not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(store.serialize(record, with_history=True))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def proforma_pdf(request, proforma_id: int):
    record = _own_or_staff(request, proforma_id)
    if record is None or not record.pdf:
        return Response({"error": "Proforma not found."}, status=status.HTTP_404_NOT_FOUND)
    resp = HttpResponse(store.pdf_bytes(record), content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{record.number}.pdf"'
    return resp


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def proforma_cancel(request, proforma_id: int):
    try:
        record = store.cancel(request.user, proforma_id, reason=str(request.data.get("reason") or ""))
    except store.ProformaError as exc:
        return _error(exc)
    return Response(store.serialize(record, with_history=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def proforma_revise(request, proforma_id: int):
    """Body: {"items": [{"equipment_id", "input_values", "input_labels_and_values"?}]}. Returns the new proforma."""
    try:
        record = store.revise(request.user, proforma_id, request.data.get("items"))
    except store.ProformaError as exc:
        return _error(exc)
    return Response(store.serialize(record, with_history=True), status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def proforma_pay_online(request, proforma_id: int):
    """Body: {"amount"?}. Returns the Razorpay checkout payload (same shape as /payments/razorpay/create-order/)."""
    from iic_booking.payments.razorpay_service import RazorpayNotConfigured, RazorpayServiceError

    try:
        payload = store.start_online_payment(request.user, proforma_id, request.data.get("amount"))
    except store.ProformaError as exc:
        return _error(exc)
    except RazorpayNotConfigured as exc:
        return Response({"error": str(exc), "code": "gateway_unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except RazorpayServiceError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(payload, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def proforma_record_offline(request, proforma_id: int):
    """Multipart: the offline-recharge fields (amount?, payment_date, payment_mode, transaction_reference, payer_bank,
    payer_account_name, paid_into?, remarks?, client_request_id?) and ``receipt``."""
    from iic_booking.users import offline_recharge

    try:
        req = store.record_offline_payment(request.user, proforma_id, request.data, request.FILES.get("receipt"))
    except store.ProformaError as exc:
        return _error(exc)
    record = SavedProformaInvoice.objects.get(pk=proforma_id)
    return Response({"proforma": store.serialize(record, with_history=True),
                     "request": offline_recharge.serialize(req, viewer=request.user)},
                    status=status.HTTP_201_CREATED)


# --- staff: reconciliation ----------------------------------------------------------------------------------------------

CSV_HEAD = ["Proforma no.", "Issued", "Valid until", "Department", "Buyer", "Buyer email", "Buyer GSTIN", "Taxable",
            "GST", "Total", "State", "Credited", "Pending", "Reconciled", "Outstanding", "Amounts", "Replaces",
            "Replaced by", "Payment kind", "Payment amount", "Payment state", "References"]


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def staff_proformas(request):
    """Main Administrator: all proformas; Department Administrator / Accounts In Charge: their department's.
    ``?export=csv`` gives one row per payment (or one row for a proforma with none) for reconciliation."""
    if not store.staff_role(request.user):
        return Response({"error": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)
    rows = _filtered(store.staff_queryset(request.user), request)
    if (request.query_params.get("export") or "").lower() != "csv":
        return Response({"results": rows, "states": store.STATE_LABELS, "role": store.staff_role(request.user)})
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(CSV_HEAD)
    for r in rows:
        st = r["state"]
        base = [r["number"], r["issued_at"][:10], r["valid_until"], r["department"]["name"], r["buyer"]["name"],
                r["buyer"]["email"], r["buyer"]["gstin"], r["taxable_total"], r["gst_total"], r["total"], st["label"],
                st["credited"], st["pending"], st["reconciled"], st["outstanding"], r["amount_source"], r["replaces"],
                r["replaced_by"]]
        if not r["payments"]:
            out.writerow(base + ["", "", "", ""])
        for p in r["payments"]:
            refs = "; ".join(f"{k}={v}" for k, v in p["references"].items() if v)
            out.writerow(base + [p["kind"], p["amount"], p["state"], refs])
    resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="proforma_invoices.csv"'
    return resp
