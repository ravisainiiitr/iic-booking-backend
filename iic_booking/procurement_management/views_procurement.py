"""Procurement records, small purchases, invoices, variance review, payments and record documents."""

from __future__ import annotations

import json

from django.db import transaction
from django.db.models import Q
from rest_framework.response import Response

from . import access, documents, invoices, purchases
from . import constants as c
from . import serializers as s
from .api import data_of, paginate, parse_bool, parse_int, pm_api, req_reason
from .errors import ProcurementError, not_found
from .models import Invoice, ProcurementRecord

P = c.OfficePermission


def records_q(scope) -> Q:
    extra = access.lab_staff_equipment_q(scope)
    mine = Q(purchase_request__requested_by=scope.user)
    return access.visible_department_wide_q(scope, extra=(extra | mine) if extra is not None else mine)


def _records_qs():
    return ProcurementRecord.objects.select_related(
        "department", "laboratory", "equipment", "purchase_request", "category", "selected_vendor", "created_by"
    ).filter(is_archived=False)


def get_record(request, pk) -> ProcurementRecord:
    return access.get_visible(_records_qs(), request.pm_scope, pk, records_q(request.pm_scope))


def record_response(request, rec, status=200) -> Response:
    rec = _records_qs().get(pk=rec.pk)
    cfg = access.get_config(rec.department_id)
    blockers = purchases.completion_blockers(rec, cfg) if rec.status not in purchases.CLOSED_RECORD else []
    return Response(s.procurement_record(rec, detail=True, blockers=blockers), status=status)


def _payload(request) -> tuple[dict, list]:
    """JSON body, or multipart with a ``payload`` JSON field plus ``files`` (mobile bill capture)."""
    files = request.FILES.getlist("files") if hasattr(request, "FILES") else []
    raw = request.data.get("payload") if hasattr(request.data, "get") else None
    if raw:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            raise ProcurementError("payload must be JSON.", code="invalid")
        if not isinstance(data, dict):
            raise ProcurementError("payload must be an object.", code="invalid")
        return data, files
    return data_of(request), files


@pm_api(["GET", "POST"])
def small_purchases(request):
    scope = request.pm_scope
    if request.method == "POST":
        data, files = _payload(request)
        rec = purchases.record_small_purchase(scope, data, files, request=request)
        return record_response(request, rec, status=201)
    qs = _records_qs().filter(records_q(scope), is_small_purchase=True)
    return _filtered(request, qs)


def _filtered(request, qs):
    p = request.query_params
    dept = parse_int(p.get("department_id"), "department_id")
    if dept:
        qs = qs.filter(department_id=dept)
    statuses = [x for x in (p.get("status") or "").split(",") if x]
    if statuses:
        qs = qs.filter(status__in=statuses)
    if p.get("financial_year"):
        qs = qs.filter(financial_year=p["financial_year"])
    if p.get("origin"):
        qs = qs.filter(origin=p["origin"])
    term = (p.get("q") or "").strip()
    if term:
        qs = qs.filter(Q(number__icontains=term) | Q(title__icontains=term) | Q(invoices__invoice_number__icontains=term)).distinct()
    return paginate(request, qs.order_by("-created_at"), s.procurement_record)


@pm_api(["GET"])
def records(request):
    qs = _records_qs().filter(records_q(request.pm_scope))
    if request.query_params.get("small") not in (None, ""):
        qs = qs.filter(is_small_purchase=parse_bool(request.query_params["small"]))
    return _filtered(request, qs)


@pm_api(["GET"])
def record_detail(request, pk: int):
    return record_response(request, get_record(request, pk))


@pm_api(["POST"])
def record_complete(request, pk: int):
    scope = request.pm_scope
    rec = get_record(request, pk)
    if not scope.dept_wide(rec.department_id):
        raise not_found()
    with transaction.atomic():
        blockers = purchases.try_complete(scope, rec, request=request)
    if blockers:
        raise ProcurementError("The record cannot be completed yet.", code="not_complete", blockers=blockers)
    return record_response(request, rec)


def _can_attach(scope, rec) -> bool:
    if scope.dept_wide(rec.department_id) and (scope.permissions(rec.department_id) - {P.REPORTS}):
        return True
    return bool(rec.purchase_request_id and rec.purchase_request.requested_by_id == scope.user.pk)


@pm_api(["GET", "POST"])
def record_documents(request, pk: int):
    scope = request.pm_scope
    rec = get_record(request, pk)
    if request.method == "GET":
        return Response({"results": [s.document(d) for d in rec.documents.filter(is_archived=False).select_related("uploaded_by")]})
    if not _can_attach(scope, rec):
        raise not_found()
    doc_type = str(request.data.get("doc_type") or c.DocumentType.OTHER)
    if doc_type == c.DocumentType.OFFLINE_APPROVAL:
        raise ProcurementError("Offline approvals are recorded on the request.", code="invalid_choice")
    links = {"procurement_record": rec}
    inv_id = parse_int(request.data.get("invoice_id"), "invoice_id")
    if inv_id:
        inv = rec.invoices.filter(pk=inv_id, is_archived=False).first()
        if inv is None:
            raise ProcurementError("Unknown bill.", code="invalid", field="invoice_id")
        links["invoice"] = inv
    with transaction.atomic():
        doc = documents.create_document(
            scope, rec.department, request.FILES.get("file"), doc_type=doc_type, links=links,
            description=str(request.data.get("description") or ""),
            page_group=documents.parse_page_group(request.data.get("page_group")),
            page_number=parse_int(request.data.get("page_number"), "page_number") or 1, request=request,
        )
        purchases.try_complete(scope, rec, request=request)
    return Response(s.document(doc), status=201)


@pm_api(["POST"])
def record_invoices(request, pk: int):
    """Add a bill to an existing record (procurement workspace / additional bills)."""
    scope = request.pm_scope
    rec = get_record(request, pk)
    scope.require_perm(rec.department_id, P.INVOICES)
    if rec.status in purchases.CLOSED_RECORD:
        raise ProcurementError("This record is closed.", code="closed")
    data, files = _payload(request)
    with transaction.atomic():
        inv = invoices.record_invoice(scope, rec, data, request=request)
        if rec.status not in (c.ProcurementRecordStatus.INVOICED,):
            rec.status = c.ProcurementRecordStatus.INVOICED
        if c.ProcurementStep.INVOICE not in (rec.completed_steps or []):
            rec.completed_steps = [*(rec.completed_steps or []), c.ProcurementStep.INVOICE.value]
        rec.save(update_fields=["status", "completed_steps", "updated_at"])
        purchases.attach_bills(scope, rec, inv, files, request=request)
        purchases.try_complete(scope, rec, request=request)
    return record_response(request, rec, status=201)


@pm_api(["POST"])
def start_from_request(request, pk: int):
    from . import workspace
    from .views_requests import _get_request

    r = _get_request(request, pk)
    rec = workspace.start_from_request(request.pm_scope, r, request=request)
    return record_response(request, rec, status=201)


@pm_api(["POST"])
def start_from_requirements(request):
    from . import workspace

    rec = workspace.start_from_requirements(request.pm_scope, data_of(request), request=request)
    return record_response(request, rec, status=201)


@pm_api(["POST"])
def record_step(request, pk: int, step: str):
    from . import workspace

    rec = workspace.update_step(request.pm_scope, get_record(request, pk), step.upper(), data_of(request), request=request)
    return record_response(request, rec)


@pm_api(["GET", "POST"])
def record_quotations(request, pk: int):
    from . import workspace
    from .exports import table_response

    scope = request.pm_scope
    rec = get_record(request, pk)
    if request.method == "POST":
        workspace.add_quotation(scope, rec, data_of(request), request=request)
        return record_response(request, rec, status=201)
    rows = workspace.comparative(rec)
    fmt = request.query_params.get("export")
    if fmt:
        headers = ["Rank", "Vendor", "Amount", "GST", "Total", "Compliance", "Delivery", "Warranty", "Lowest compliant", "Selected"]
        body = [[r["rank"], r["vendor"], r["amount"], r["gst_amount"], r["total_amount"], r["compliance"], r["delivery_period"],
                 r["warranty"], "Yes" if r["is_lowest_compliant"] else "", "Yes" if r["is_selected"] else ""] for r in rows]
        return table_response(fmt, f"Comparative statement {rec.number}", headers, body, subtitle=rec.title)
    return Response({"results": rows})


@pm_api(["POST"])
def record_cancel(request, pk: int):
    from . import workspace

    rec = workspace.cancel_record(request.pm_scope, get_record(request, pk), req_reason(data_of(request)), request=request)
    return record_response(request, rec)


def _get_invoice(request, pk) -> Invoice:
    scope = request.pm_scope
    inv = Invoice.objects.select_related("procurement_record", "vendor", "recorded_by").filter(pk=pk, is_archived=False).first()
    if inv is None:
        raise not_found()
    get_record(request, inv.procurement_record_id)
    if inv.department_id not in scope.department_ids():
        raise not_found()
    return inv


@pm_api(["GET"])
def invoice_detail(request, pk: int):
    return Response(s.invoice(_get_invoice(request, pk), detail=True))


@pm_api(["POST"])
def invoice_variance_review(request, pk: int):
    inv = invoices.review_variance(request.pm_scope, _get_invoice(request, pk), note=req_reason(data_of(request), "note"), request=request)
    return Response(s.invoice(Invoice.objects.get(pk=inv.pk), detail=True))


@pm_api(["POST"])
def invoice_payment(request, pk: int):
    inv = invoices.record_payment(request.pm_scope, _get_invoice(request, pk), data_of(request), request=request)
    return Response(s.invoice(Invoice.objects.get(pk=inv.pk), detail=True))
