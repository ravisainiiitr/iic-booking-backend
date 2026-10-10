"""Purchase requests, approval actions, approval inbox and private documents."""

from __future__ import annotations

from django.db.models import Q
from django.http import FileResponse
from rest_framework.response import Response

from . import access, documents, requests_service, workflow
from . import constants as c
from . import serializers as s
from .api import data_of, paginate, parse_bool, parse_day, parse_int, parse_money, pm_api, req_reason, req_str
from .errors import ProcurementError, not_found
from .models import ItemCategory, ProcurementDocument, PurchaseRequest, RequestTypeConfig
from .small_purchase import evaluate

RS = c.RequestStatus


def _base_qs():
    return PurchaseRequest.objects.select_related(
        "department", "laboratory", "equipment", "request_type", "category", "requested_by"
    ).filter(is_archived=False)


def _get_request(request, pk) -> PurchaseRequest:
    scope = request.pm_scope
    return access.get_visible(_base_qs(), scope, pk, access.visible_requests_q(scope))


def _detail(request, r) -> Response:
    r = _base_qs().get(pk=r.pk)
    return Response(s.purchase_request(r, detail=True, scope=request.pm_scope))


@pm_api(["GET", "POST"])
def requests_list(request):
    scope = request.pm_scope
    if request.method == "POST":
        data = data_of(request)
        r = requests_service.create_request(scope, data, request=request)
        if parse_bool(data.get("submit", False)):
            r = workflow.submit(scope, r, request=request)
        return Response(s.purchase_request(_base_qs().get(pk=r.pk), detail=True, scope=scope), status=201)
    p = request.query_params
    qs = _base_qs()
    if parse_bool(p.get("pending_for_me", "")):
        qs = qs.filter(workflow.pending_for_me_q(scope))
    else:
        qs = qs.filter(access.visible_requests_q(scope))
    if parse_bool(p.get("mine", "")):
        qs = qs.filter(requested_by=scope.user)
    dept = parse_int(p.get("department_id"), "department_id")
    if dept:
        qs = qs.filter(department_id=dept)
    statuses = [x for x in (p.get("status") or "").split(",") if x]
    if statuses:
        qs = qs.filter(status__in=statuses)
    if p.get("request_type"):
        qs = qs.filter(request_type__code=p["request_type"])
    if p.get("financial_year"):
        qs = qs.filter(financial_year=p["financial_year"])
    eq = parse_int(p.get("equipment_id"), "equipment_id")
    if eq:
        qs = qs.filter(equipment_id=eq)
    for key in ("maintenance_record_id", "disruption_event_id"):
        val = parse_int(p.get(key), key)
        if val:
            qs = qs.filter(**{key: val})
    term = (p.get("q") or "").strip()
    if term:
        qs = qs.filter(Q(number__icontains=term) | Q(title__icontains=term) | Q(lines__description__icontains=term)).distinct()
    return paginate(request, qs.order_by("-created_at"), s.purchase_request)


@pm_api(["GET", "PATCH"])
def request_detail(request, pk: int):
    r = _get_request(request, pk)
    if request.method == "PATCH":
        r = requests_service.update_request(request.pm_scope, r, data_of(request), request=request)
    return _detail(request, r)


@pm_api(["POST"])
def request_action(request, pk: int, action: str):
    scope = request.pm_scope
    r = _get_request(request, pk)
    data = data_of(request)
    comments = req_str(data, "comments", max_len=5000, required=False)
    if action in ("submit", "resubmit"):
        r = workflow.submit(scope, r, comments=comments, request=request)
    elif action == "approve":
        amount = parse_money(data.get("amount"), "amount", required=False)
        r = workflow.approve(scope, r, comments=comments, amount=amount, request=request)
    elif action == "reject":
        r = workflow.reject(scope, r, reason=req_reason(data), request=request)
    elif action == "hold":
        r = workflow.hold(scope, r, reason=req_reason(data), request=request)
    elif action == "resume":
        r = workflow.resume(scope, r, comments=comments, request=request)
    elif action == "cancel":
        r = workflow.cancel(scope, r, reason=req_reason(data), request=request)
    elif action == "stores-review":
        r = workflow.stores_review(
            scope, r, decision=str(data.get("decision") or ""), lines=data.get("lines"), comments=comments, request=request
        )
    elif action == "issue":
        r = workflow.issue(scope, r, comments=comments, request=request)
    else:
        raise not_found("Unknown action.")
    return _detail(request, r)


@pm_api(["POST"])
def request_offline_hod(request, pk: int):
    scope = request.pm_scope
    r = _get_request(request, pk)
    data = request.data
    r = workflow.offline_hod_decision(
        scope,
        r,
        decision=str(data.get("decision") or "").upper(),
        upload=request.FILES.get("file"),
        approver_name=req_str(data, "approver_name"),
        approver_designation=req_str(data, "approver_designation"),
        approval_date=parse_day(data.get("approval_date"), "approval_date", required=True),
        reference=req_str(data, "reference", required=False),
        comments=req_str(data, "comments", max_len=5000, required=False),
        amount=parse_money(data.get("amount"), "amount", required=False),
        request=request,
    )
    return _detail(request, r)


@pm_api(["GET", "POST"])
def request_documents(request, pk: int):
    scope = request.pm_scope
    r = _get_request(request, pk)
    if request.method == "GET":
        docs = r.documents.filter(is_archived=False).select_related("uploaded_by")
        return Response({"results": [s.document(d) for d in docs]})
    if not (r.requested_by_id == scope.user.pk or scope.dept_wide(r.department_id) or scope.is_oic_for(r.equipment_id)):
        raise not_found()
    if r.status in (RS.CANCELLED, RS.COMPLETED):
        raise ProcurementError("Documents cannot be added to a closed request.", code="closed")
    doc_type = str(request.data.get("doc_type") or c.DocumentType.QUOTATION)
    if doc_type == c.DocumentType.OFFLINE_APPROVAL:
        raise ProcurementError("Record offline approvals through the offline HOD decision.", code="invalid_choice")
    doc = documents.create_document(
        scope, r.department, request.FILES.get("file"), doc_type=doc_type, links={"purchase_request": r},
        description=str(request.data.get("description") or ""),
        page_group=documents.parse_page_group(request.data.get("page_group")),
        page_number=parse_int(request.data.get("page_number"), "page_number") or 1, request=request,
    )
    return Response(s.document(doc), status=201)


@pm_api(["GET"])
def approvals_inbox(request):
    scope = request.pm_scope
    qs = _base_qs().filter(workflow.pending_for_me_q(scope)).order_by("submitted_at", "id")
    return paginate(request, qs, s.purchase_request)


@pm_api(["GET"])
def small_purchase_check(request):
    """Live hint for forms: is this total eligible for direct small purchase in the department?"""
    scope = request.pm_scope
    p = request.query_params
    dept = access.pick_department(scope, p.get("department_id"))
    cfg = access.require_config(dept)
    total = parse_money(p.get("amount"), "amount")
    cat = ItemCategory.objects.filter(department=dept, pk=parse_int(p.get("category_id"), "category_id")).first() if p.get("category_id") else None
    rt = RequestTypeConfig.objects.filter(department=dept, pk=parse_int(p.get("request_type_id"), "request_type_id")).first() if p.get("request_type_id") else None
    return Response(evaluate(cfg, total, category=cat, request_type=rt).as_dict())


def _get_document(request, pk) -> ProcurementDocument:
    scope = request.pm_scope
    doc = (
        ProcurementDocument.objects.select_related("asset", "amc_record")
        .filter(pk=pk, department_id__in=scope.department_ids())
        .first()
    )
    if doc is None or not documents.can_view(scope, doc):
        raise not_found()
    return doc


@pm_api(["GET"])
def document_download(request, pk: int):
    doc = _get_document(request, pk)
    try:
        fh = doc.file.open("rb")
    except Exception:
        raise not_found("File not available.")
    disposition = "inline" if parse_bool(request.query_params.get("inline", "")) else "attachment"
    resp = FileResponse(fh, content_type=doc.content_type, as_attachment=disposition == "attachment", filename=doc.original_name)
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store"
    return resp


@pm_api(["POST"])
def document_archive(request, pk: int):
    doc = _get_document(request, pk)
    doc = documents.archive_document(request.pm_scope, doc, req_reason(data_of(request)), request=request)
    return Response(s.document(doc))
