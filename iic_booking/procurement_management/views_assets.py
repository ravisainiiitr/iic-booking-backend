"""Asset register + transfers, consumable stock ledger, AMC / service records."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone
from rest_framework.response import Response

from . import access, amc, assets, documents, stock
from . import constants as c
from . import serializers as s
from .api import data_of, paginate, parse_bool, parse_int, pm_api
from .errors import ProcurementError, not_found
from .exports import table_response
from .models import AMCServiceRecord, Asset, AssetTransfer, StockBalance, StockTransaction
from .views_procurement import _payload

P = c.OfficePermission


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------
def assets_q(scope) -> Q:
    return access.visible_department_wide_q(scope, extra=access.lab_staff_equipment_q(scope))


def _assets_qs():
    return Asset.objects.select_related(
        "department", "laboratory", "equipment", "category", "procurement_record", "vendor", "custodian", "created_by"
    ).filter(is_archived=False)


def _get_asset(request, pk) -> Asset:
    return access.get_visible(_assets_qs(), request.pm_scope, pk, assets_q(request.pm_scope))


ASSET_EXPORT_HEADERS = [
    "Asset no.", "Description", "Category", "Make", "Model", "Serial no.", "Equipment", "Location", "Custodian",
    "Status", "Purchase date", "Cost", "Funding", "FY", "Procurement record",
]


def _asset_row(a):
    return [
        a.number, a.description, a.category.name, a.make, a.model_number, a.serial_number,
        a.equipment.name if a.equipment_id else "", a.location, (s.user_brief(a.custodian) or {}).get("name", ""),
        a.get_status_display(), s.iso(a.purchase_date) or "", s.m(a.cost), a.funding_type, a.financial_year,
        a.procurement_record.number if a.procurement_record_id else "",
    ]


@pm_api(["GET", "POST"])
def assets_list(request):
    scope = request.pm_scope
    if request.method == "POST":
        created = assets.register(scope, data_of(request), request=request)
        return Response({"results": [s.asset(_assets_qs().get(pk=a.pk)) for a in created]}, status=201)
    p = request.query_params
    qs = _assets_qs().filter(assets_q(scope))
    for key, field in (("department_id", "department_id"), ("equipment_id", "equipment_id"), ("category_id", "category_id"),
                       ("procurement_record_id", "procurement_record_id")):
        val = parse_int(p.get(key), key)
        if val:
            qs = qs.filter(**{field: val})
    statuses = [x for x in (p.get("status") or "").split(",") if x]
    if statuses:
        qs = qs.filter(status__in=statuses)
    if p.get("financial_year"):
        qs = qs.filter(financial_year=p["financial_year"])
    term = (p.get("q") or "").strip()
    if term:
        qs = qs.filter(
            Q(number__icontains=term) | Q(description__icontains=term) | Q(serial_number__icontains=term)
            | Q(asset_tag__icontains=term) | Q(make__icontains=term) | Q(model_number__icontains=term)
        )
    qs = qs.order_by("-created_at")
    fmt = p.get("export")
    if fmt:
        if not any(scope.has_perm(d, P.REPORTS) or scope.has_perm(d, P.ASSETS) for d in scope.department_ids()):
            raise not_found()
        return table_response(fmt, "Asset register", ASSET_EXPORT_HEADERS, [_asset_row(a) for a in qs[:5000]])
    return paginate(request, qs, s.asset)


@pm_api(["GET", "PATCH"])
def asset_detail(request, pk: int):
    a = _get_asset(request, pk)
    if request.method == "PATCH":
        a = assets.update(request.pm_scope, a, data_of(request), request=request)
    return Response(s.asset(_assets_qs().get(pk=a.pk), detail=True))


@pm_api(["POST"])
def asset_status(request, pk: int):
    a = assets.change_status(request.pm_scope, _get_asset(request, pk), data_of(request), request=request)
    return Response(s.asset(_assets_qs().get(pk=a.pk), detail=True))


@pm_api(["POST"])
def asset_documents(request, pk: int):
    scope = request.pm_scope
    a = _get_asset(request, pk)
    scope.require_perm(a.department_id, P.ASSETS)
    files = request.FILES.getlist("files") or ([request.FILES["file"]] if "file" in request.FILES else [])
    if not files:
        raise ProcurementError("Attach a file.", code="file_required", field="file")
    doc_type = request.data.get("doc_type") or c.DocumentType.ASSET_PHOTO
    if doc_type not in (c.DocumentType.ASSET_PHOTO, c.DocumentType.INVOICE, c.DocumentType.INSPECTION_REPORT, c.DocumentType.OTHER):
        raise ProcurementError("Unsupported document type for an asset.", code="invalid_choice", field="doc_type")
    docs = [
        documents.create_document(scope, a.department, f, doc_type=doc_type, links={"asset": a},
                                  description=request.data.get("description") or "", request=request)
        for f in files
    ]
    return Response({"results": [s.document(d) for d in docs]}, status=201)


def _transfers_qs():
    return AssetTransfer.objects.select_related(
        "asset", "from_laboratory", "to_laboratory", "from_equipment", "to_equipment", "from_custodian", "to_custodian",
        "requested_by", "decided_by",
    )


def transfers_q(scope) -> Q:
    return access.visible_department_wide_q(scope, extra=Q(requested_by=scope.user) | (access.lab_staff_equipment_q(scope, "asset__equipment_id") or Q(pk__in=[])))


@pm_api(["GET", "POST"])
def asset_transfers(request, pk: int):
    a = _get_asset(request, pk)
    if request.method == "POST":
        t = assets.request_transfer(request.pm_scope, a, data_of(request), request=request)
        return Response(s.asset_transfer(_transfers_qs().get(pk=t.pk)), status=201)
    return Response({"results": [s.asset_transfer(t) for t in _transfers_qs().filter(asset=a)]})


@pm_api(["GET"])
def transfers_list(request):
    scope = request.pm_scope
    qs = _transfers_qs().filter(transfers_q(scope))
    statuses = [x for x in (request.query_params.get("status") or "").split(",") if x]
    if statuses:
        qs = qs.filter(status__in=statuses)
    dept = parse_int(request.query_params.get("department_id"), "department_id")
    if dept:
        qs = qs.filter(department_id=dept)
    return paginate(request, qs.order_by("-created_at"), s.asset_transfer)


TRANSFER_ACTIONS = {
    "decide": assets.decide_transfer,
    "return": assets.return_transfer,
    "cancel": assets.cancel_transfer,
}


@pm_api(["POST"])
def transfer_action(request, pk: int, action: str):
    scope = request.pm_scope
    t = access.get_visible(_transfers_qs(), scope, pk, transfers_q(scope))
    if action == "complete":
        t = assets.complete_transfer(scope, t, request=request)
    elif action in TRANSFER_ACTIONS:
        t = TRANSFER_ACTIONS[action](scope, t, data_of(request), request=request)
    else:
        raise not_found()
    return Response(s.asset_transfer(_transfers_qs().get(pk=t.pk)))


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------
@pm_api(["GET"])
def stock_balances(request):
    """Balances are visible to anyone with a role in the department (lab staff check availability before raising
    a consumable request); the ledger itself is department-wide only."""
    scope = request.pm_scope
    p = request.query_params
    qs = StockBalance.objects.select_related("item", "laboratory").filter(department_id__in=scope.department_ids())
    dept = parse_int(p.get("department_id"), "department_id")
    if dept:
        qs = qs.filter(department_id=dept)
    item = parse_int(p.get("item_id"), "item_id")
    if item:
        qs = qs.filter(item_id=item)
    if p.get("laboratory_id") == "central":
        qs = qs.filter(laboratory__isnull=True)
    elif p.get("laboratory_id"):
        from .requests_service import _lookup_laboratory

        qs = qs.filter(laboratory=_lookup_laboratory(p["laboratory_id"]))
    if parse_bool(p.get("low") or ""):
        qs = qs.filter(Q(min_level__gt=0, quantity__lt=F("min_level")) | Q(reorder_level__gt=0, quantity__lte=F("reorder_level")))
    term = (p.get("q") or "").strip()
    if term:
        qs = qs.filter(Q(item__name__icontains=term) | Q(item__code__icontains=term))
    return paginate(request, qs.order_by("item__name", "id"), s.stock_balance)


@pm_api(["GET", "POST"])
def stock_transactions(request):
    scope = request.pm_scope
    if request.method == "POST":
        data = data_of(request)
        dept = access.pick_department(scope, data.get("department_id"))
        tx = stock.manual_entry(scope, dept, data, request=request)
        return Response(s.stock_transaction(StockTransaction.objects.select_related("item", "laboratory", "issued_to", "performed_by").get(pk=tx.pk)), status=201)
    p = request.query_params
    wide = [d for d in scope.department_ids() if scope.dept_wide(d)]
    qs = StockTransaction.objects.select_related("item", "laboratory", "issued_to", "performed_by").filter(department_id__in=wide)
    for key in ("department_id", "item_id", "purchase_request_id", "procurement_record_id"):
        val = parse_int(p.get(key), key)
        if val:
            qs = qs.filter(**{key: val})
    if p.get("tx_type"):
        qs = qs.filter(tx_type__in=p["tx_type"].split(","))
    if p.get("date_from"):
        qs = qs.filter(transaction_date__gte=p["date_from"])
    if p.get("date_to"):
        qs = qs.filter(transaction_date__lte=p["date_to"])
    return paginate(request, qs.order_by("-transaction_date", "-id"), s.stock_transaction)


@pm_api(["POST"])
def stock_levels(request):
    scope = request.pm_scope
    data = data_of(request)
    dept = access.pick_department(scope, data.get("department_id"))
    bal = stock.set_levels(scope, dept, data, request=request)
    return Response(s.stock_balance(StockBalance.objects.select_related("item", "laboratory").get(pk=bal.pk)))


# ---------------------------------------------------------------------------
# AMC
# ---------------------------------------------------------------------------
def _amc_qs():
    return AMCServiceRecord.objects.select_related("department", "equipment", "asset", "vendor", "created_by").filter(is_archived=False)


def amc_q(scope) -> Q:
    lab = set(scope.oic_equipment) | set(scope.operator_equipment)
    return access.visible_department_wide_q(scope, extra=Q(equipment_id__in=list(lab)) if lab else None)


@pm_api(["GET", "POST"])
def amc_list(request):
    scope = request.pm_scope
    if request.method == "POST":
        data, files = _payload(request)
        rec = amc.create(scope, data, files, request=request)
        return Response(s.amc_record(_amc_qs().get(pk=rec.pk), detail=True), status=201)
    p = request.query_params
    qs = _amc_qs().filter(amc_q(scope))
    for key in ("department_id", "equipment_id", "asset_id"):
        val = parse_int(p.get(key), key)
        if val:
            qs = qs.filter(**{key: val})
    statuses = [x for x in (p.get("status") or "").split(",") if x]
    if statuses:
        qs = qs.filter(status__in=statuses)
    within = parse_int(p.get("expiring_within"), "expiring_within")
    if within is not None:
        today = timezone.localdate()
        qs = qs.filter(status=c.AMCStatus.ACTIVE, end_date__gte=today, end_date__lte=today + timedelta(days=within))
    return paginate(request, qs.order_by("end_date", "id"), s.amc_record)


@pm_api(["GET", "PATCH"])
def amc_detail(request, pk: int):
    rec = amc.get_visible(request.pm_scope, pk)
    if request.method == "PATCH":
        rec = amc.update(request.pm_scope, rec, data_of(request), request=request)
    return Response(s.amc_record(_amc_qs().get(pk=rec.pk), detail=True))


@pm_api(["POST"])
def amc_renew(request, pk: int):
    rec = amc.get_visible(request.pm_scope, pk)
    data, files = _payload(request)
    new = amc.renew(request.pm_scope, rec, data, files, request=request)
    return Response(s.amc_record(_amc_qs().get(pk=new.pk), detail=True), status=201)


@pm_api(["POST"])
def amc_cancel(request, pk: int):
    rec = amc.cancel(request.pm_scope, amc.get_visible(request.pm_scope, pk), data_of(request), request=request)
    return Response(s.amc_record(_amc_qs().get(pk=rec.pk), detail=True))
