"""Dashboard, budget allocations + budget vs actual, tabular reports with exports, auditor drill-down."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, F, Q, Sum
from django.utils import timezone
from rest_framework.response import Response

from . import access, budget, workflow
from . import constants as c
from . import serializers as s
from .api import data_of, paginate, parse_int, pm_api
from .errors import forbidden, not_found
from .exports import table_response
from .fy import fy_label
from .models import (
    AMCServiceRecord,
    Asset,
    AssetTransfer,
    BudgetAllocation,
    Invoice,
    PlanProposal,
    PlanRequirement,
    ProcurementAuditLog,
    ProcurementRecord,
    PurchaseRequest,
    StockBalance,
    StockTransaction,
)
from .reports import REPORTS, Filters

P = c.OfficePermission
RS = c.RequestStatus
PRS = c.ProcurementRecordStatus


def _money_dict(row: dict) -> dict:
    return {k: (s.m(v) if k != "funding_type" and v is not None else v) for k, v in row.items()}


def _bva(dept_id, fy) -> dict:
    data = budget.budget_vs_actual(dept_id, fy)
    return {"financial_year": fy, "rows": [_money_dict(r) for r in data["rows"]], "total": _money_dict(data["total"])}


AGEING_BUCKETS = (("0-3", 0, 3), ("4-7", 4, 7), ("8-15", 8, 15), (">15", 16, None))


def ageing(qs, now=None) -> dict:
    """Pending requests bucketed by days at their current stage (SLA ageing)."""
    now = now or timezone.now()
    out = {label: 0 for label, _, _ in AGEING_BUCKETS}
    oldest = None
    for entered, submitted in qs.values_list("stage_entered_at", "submitted_at"):
        since = entered or submitted
        if since is None:
            continue
        days = (now - since).days
        oldest = days if oldest is None else max(oldest, days)
        for label, lo, hi in AGEING_BUCKETS:
            if days >= lo and (hi is None or days <= hi):
                out[label] += 1
                break
    return {"buckets": out, "oldest_days": oldest}


def _inventory_widgets(scope, dept, cfg, fy, lab_eq) -> dict:
    from . import verification
    from .fy import fy_bounds
    from .models import AssetRegister, ItemEquipmentLink, MaintenanceRecord

    start, end = fy_bounds(fy)
    out: dict = {}
    maint = MaintenanceRecord.objects.filter(department=dept, is_archived=False)
    wide = scope.dept_wide(dept.pk)
    if not wide:
        maint = maint.filter(equipment_id__in=lab_eq)
    fy_maint = list(maint.filter(Q(downtime_start__date__gte=start, downtime_start__date__lte=end) | Q(downtime_start__isnull=True, created_at__date__gte=start)))
    out["maintenance"] = {
        "this_year": len(fy_maint),
        "open_downtime": maint.filter(downtime_start__isnull=False, downtime_end__isnull=True).count(),
        "downtime_hours": round(sum((r.downtime_hours or 0) for r in fy_maint), 1),
        "cost": s.m(sum((r.total_cost for r in fy_maint), 0)),
    }
    if cfg.asset_register_enabled:
        if wide:
            regs = AssetRegister.objects.filter(department=dept, is_archived=False)
            out["registers"] = {
                "count": regs.count(),
                "by_type": _counts(regs, "register_type"),
                "entries": Asset.objects.filter(department=dept, is_archived=False, register__isnull=False).count(),
                "unregistered": Asset.objects.filter(department=dept, is_archived=False, register__isnull=True).count(),
            }
            out["verification"] = verification.department_summary([dept.pk], fy)
        else:
            mine = Asset.objects.filter(department=dept, is_archived=False, equipment_id__in=lab_eq)
            out["my_assets"] = {
                "count": mine.count(),
                "not_verified_this_year": mine.exclude(last_verified_on__gte=start, last_verified_on__lte=end).count(),
            }
    if lab_eq:
        linked = ItemEquipmentLink.objects.filter(department=dept, equipment_id__in=lab_eq, active=True).values_list("item_id", flat=True)
        out["linked_low_stock"] = StockBalance.objects.filter(department=dept, laboratory__isnull=True, item_id__in=list(linked)).filter(
            Q(min_level__gt=0, quantity__lt=F("min_level")) | Q(reorder_level__gt=0, quantity__lte=F("reorder_level"))
        ).count()
    return out


def _counts(qs, field="status") -> dict:
    return {row[field]: row["n"] for row in qs.values(field).annotate(n=Count("id")).order_by(field)}


@pm_api(["GET"])
def dashboard(request):
    """Role-aware summary for one department. Lab staff see their own work; department-wide roles see everything;
    money widgets need ``reports`` or ``budget``."""
    scope = request.pm_scope
    dept = access.pick_department(scope, request.query_params.get("department_id"))
    cfg = access.require_config(dept)
    today = timezone.localdate()
    fy = cfg.current_financial_year or fy_label(today)
    reqs = PurchaseRequest.objects.filter(department=dept, is_archived=False)
    lab_eq = list(scope.lab_equipment)
    horizon = today + timedelta(days=cfg.amc_reminder_days)
    amc_due = AMCServiceRecord.objects.filter(department=dept, status=c.AMCStatus.ACTIVE, end_date__gte=today, end_date__lte=horizon, is_archived=False)
    out = {
        "department": s.department_brief(dept),
        "financial_year": fy,
        "roles": sorted(scope.roles(dept.pk)),
        "my_requests": _counts(reqs.filter(requested_by=scope.user)),
        "pending_approvals": reqs.filter(workflow.pending_for_me_q(scope)).count(),
        "my_queue_ageing": ageing(reqs.filter(workflow.pending_for_me_q(scope))),
        "pending_for_me": [
            {"id": r.pk, "number": r.number, "title": r.title, "status": r.status, "status_label": r.get_status_display(),
             "stage_age_days": s.stage_age_days(r), "estimated_total": s.m(r.estimated_total)}
            for r in reqs.filter(workflow.pending_for_me_q(scope)).order_by("stage_entered_at", "submitted_at")[:10]
        ],
    }
    out.update(_inventory_widgets(scope, dept, cfg, fy, lab_eq))
    roles = scope.roles(dept.pk)
    if c.ModuleRole.OC_STORES in roles:
        bills = Invoice.objects.filter(department=dept, is_archived=False)
        out["stores"] = {
            "pending_review": reqs.filter(status=RS.PENDING_STORES).count(),
            "to_issue": reqs.filter(status=RS.STORES_AVAILABLE).count(),
            "bills_to_forward": bills.filter(forwarded_to_accounts_at__isnull=True).exclude(payment_status=c.PaymentStatus.PAID).count(),
        }
    if c.ModuleRole.ACCOUNTS in roles or scope.has_perm(dept.pk, P.PAYMENTS):
        fwd = Invoice.objects.filter(department=dept, is_archived=False, forwarded_to_accounts_at__isnull=False).exclude(
            payment_status=c.PaymentStatus.PAID
        )
        out["accounts"] = {
            "bills_pending": fwd.count(),
            "amount_pending": s.m(fwd.aggregate(t=Sum(F("total_amount") - F("paid_amount")))["t"] or 0),
            "budget_checks_pending": reqs.filter(status=RS.PENDING_ACCOUNTS).count(),
        }
    if not scope.dept_wide(dept.pk):
        out["amc_expiring"] = amc_due.filter(equipment_id__in=lab_eq).count()
        out["my_requirements"] = _counts(PlanRequirement.objects.filter(department=dept, raised_by=scope.user, is_archived=False))
        return Response(out)
    records = ProcurementRecord.objects.filter(department=dept, is_archived=False)
    bills = Invoice.objects.filter(department=dept, is_archived=False)
    sp = records.filter(is_small_purchase=True, financial_year=fy).exclude(status=PRS.CANCELLED)
    out.update(
        requests_by_status=_counts(reqs),
        records_by_status=_counts(records),
        small_purchases={"count": sp.count(), "total": s.m(Invoice.objects.filter(procurement_record__in=sp, is_archived=False).aggregate(t=Sum("total_amount"))["t"] or 0)},
        open_variance=bills.filter(variance_status__in=(c.VarianceStatus.OFFICE_REVIEW, c.VarianceStatus.REAPPROVAL_REQUIRED)).count(),
        unpaid_bills={
            "count": bills.exclude(payment_status=c.PaymentStatus.PAID).count(),
            "amount": s.m((bills.exclude(payment_status=c.PaymentStatus.PAID).aggregate(t=Sum(F("total_amount") - F("paid_amount")))["t"]) or 0),
        },
        requirements_by_status=_counts(PlanRequirement.objects.filter(department=dept, financial_year=fy, is_archived=False)),
        low_stock=StockBalance.objects.filter(department=dept).filter(
            Q(min_level__gt=0, quantity__lt=F("min_level")) | Q(reorder_level__gt=0, quantity__lte=F("reorder_level"))
        ).count(),
        assets_by_status=_counts(Asset.objects.filter(department=dept, is_archived=False)),
        open_transfers=AssetTransfer.objects.filter(department=dept, status__in=(c.TransferStatus.REQUESTED, c.TransferStatus.APPROVED)).count(),
        amc_expiring=amc_due.count(),
        pending_ageing=ageing(reqs.filter(status__in=list(c.PENDING_STATUSES))),
    )
    if budget.has_budget_visibility(scope, dept.pk):
        out["budget"] = _bva(dept.pk, fy)
    return Response(out)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------
def _allocation(row) -> dict:
    return {
        "id": row.pk,
        "department_id": row.department_id,
        "financial_year": row.financial_year,
        "funding_type": row.funding_type,
        "laboratory": s.lab_brief(row.laboratory),
        "category": {"id": row.category_id, "name": row.category.name} if row.category_id else None,
        "amount": s.m(row.amount),
        "reference": row.reference,
        "remarks": row.remarks,
        "is_archived": row.is_archived,
        "archive_reason": row.archive_reason,
        "created_by": s.user_brief(row.created_by),
        "created_at": s.iso(row.created_at),
    }


def _alloc_qs():
    return BudgetAllocation.objects.select_related("laboratory", "category", "created_by")


@pm_api(["GET", "POST"])
def budgets(request):
    scope = request.pm_scope
    if request.method == "POST":
        data = data_of(request)
        row = budget.create_allocation(scope, access.pick_department(scope, data.get("department_id")), data, request=request)
        return Response(_allocation(_alloc_qs().get(pk=row.pk)), status=201)
    p = request.query_params
    qs = _alloc_qs().filter(budget.allocations_q(scope))
    dept = parse_int(p.get("department_id"), "department_id")
    if dept:
        qs = qs.filter(department_id=dept)
    if p.get("financial_year"):
        qs = qs.filter(financial_year=p["financial_year"])
    if p.get("include_archived") not in ("1", "true"):
        qs = qs.filter(is_archived=False)
    return paginate(request, qs.order_by("-financial_year", "funding_type", "id"), _allocation)


def _get_alloc(request, pk) -> BudgetAllocation:
    return access.get_visible(_alloc_qs(), request.pm_scope, pk, budget.allocations_q(request.pm_scope))


@pm_api(["GET", "PATCH"])
def budget_detail(request, pk: int):
    row = _get_alloc(request, pk)
    if request.method == "PATCH":
        row = budget.update_allocation(request.pm_scope, row, data_of(request), request=request)
    return Response(_allocation(_alloc_qs().get(pk=row.pk)))


@pm_api(["POST"])
def budget_archive(request, pk: int):
    row = budget.archive_allocation(request.pm_scope, _get_alloc(request, pk), data_of(request), request=request)
    return Response(_allocation(_alloc_qs().get(pk=row.pk)))


@pm_api(["GET"])
def budget_summary(request):
    scope = request.pm_scope
    p = request.query_params
    dept = access.pick_department(scope, p.get("department_id"))
    cfg = access.require_config(dept)
    if not budget.has_budget_visibility(scope, dept.pk):
        raise forbidden()
    fy = budget.clean_fy(p["financial_year"]) if p.get("financial_year") else (cfg.current_financial_year or fy_label())
    category_id = parse_int(p.get("category_id"), "category_id")
    if category_id:
        data = budget.budget_vs_actual(dept.pk, fy, category_id=category_id)
        return Response({"financial_year": fy, "category_id": category_id, "rows": [_money_dict(r) for r in data["rows"]], "total": _money_dict(data["total"])})
    return Response(_bva(dept.pk, fy))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
@pm_api(["GET"])
def report(request, name: str):
    scope = request.pm_scope
    builder = REPORTS.get(name)
    if builder is None:
        raise not_found("Unknown report.")
    p = request.query_params
    dept = access.pick_department(scope, p.get("department_id"))
    cfg = access.require_config(dept)
    scope.require_perm(dept.pk, P.REPORTS)
    filters = Filters(p, cfg)
    title, headers, rows = builder(dept, filters)
    title = f"{title} — {dept.name}"
    fmt = (p.get("export") or "").lower()
    if fmt in ("csv", "xlsx", "pdf"):
        return table_response(fmt, title, headers, rows, subtitle=filters.subtitle())
    return Response({"title": title, "subtitle": filters.subtitle(), "headers": headers, "rows": rows, "count": len(rows)})


@pm_api(["GET"])
def report_catalog(request):
    return Response({"reports": sorted(REPORTS)})


# ---------------------------------------------------------------------------
# Auditor drill-down: one object with its full trail and linked records
# ---------------------------------------------------------------------------
def _audit_for(pairs) -> list[dict]:
    q = Q(pk__in=[])
    for obj_type, ids in pairs:
        ids = [str(i) for i in ids if i]
        if ids:
            q |= Q(object_type=obj_type, object_id__in=ids)
    return [s.audit_log(x) for x in ProcurementAuditLog.objects.filter(q).select_related("actor").order_by("created_at", "id")]


def _stock(qs) -> list[dict]:
    return [s.stock_transaction(t) for t in qs.select_related("item", "laboratory", "issued_to", "performed_by")]


def _trail_request(r):
    recs = list(r.procurement_records.all())
    inv_ids = list(Invoice.objects.filter(procurement_record__in=recs).values_list("id", flat=True))
    return (
        s.purchase_request(r, detail=True),
        {
            "records": [s.procurement_record(x) for x in recs],
            "stock_transactions": _stock(r.stock_transactions.all()),
        },
        [("PurchaseRequest", [r.pk]), ("ProcurementRecord", [x.pk for x in recs]), ("Invoice", inv_ids)],
    )


def _trail_record(rec):
    inv_ids = list(rec.invoices.values_list("id", flat=True))
    asset_ids = list(rec.assets.values_list("id", flat=True))
    return (
        s.procurement_record(rec, detail=True),
        {
            "purchase_request": s.purchase_request(rec.purchase_request) if rec.purchase_request_id else None,
            "requirements": [s.requirement(x) for x in rec.requirements.select_related("department", "laboratory", "equipment", "category", "raised_by")],
            "stock_transactions": _stock(rec.stock_transactions.all()),
            "amc_records": [s.amc_record(x) for x in rec.amc_records.select_related("department", "equipment", "asset", "vendor", "created_by")],
        },
        [("ProcurementRecord", [rec.pk]), ("Invoice", inv_ids), ("Asset", asset_ids),
         ("PurchaseRequest", [rec.purchase_request_id])],
    )


def _trail_invoice(inv):
    return (
        s.invoice(inv, detail=True),
        {
            "record": s.procurement_record(inv.procurement_record),
            "assets": [s.asset(a) for a in inv.assets.select_related("department", "laboratory", "equipment", "category", "procurement_record", "vendor", "custodian", "created_by")],
            "stock_transactions": _stock(StockTransaction.objects.filter(invoice=inv)),
        },
        [("Invoice", [inv.pk])],
    )


def _trail_asset(a):
    return s.asset(a, detail=True), {}, [("Asset", [a.pk]), ("AssetTransfer", list(a.transfers.values_list("id", flat=True)))]


def _trail_requirement(r):
    return s.requirement(r, detail=True), {"proposal": s.proposal(r.proposal) if r.proposal_id else None}, [("PlanRequirement", [r.pk])]


def _trail_proposal(p):
    return s.proposal(p, detail=True), {}, [("PlanProposal", [p.pk]), ("PlanRequirement", list(p.requirements.values_list("id", flat=True)))]


def _trail_amc(r):
    return s.amc_record(r, detail=True), {}, [("AMCServiceRecord", [r.pk])]


TRAILS = {
    "request": (PurchaseRequest.objects.select_related("department", "laboratory", "equipment", "request_type", "category", "requested_by"), _trail_request),
    "record": (ProcurementRecord.objects.select_related("department", "laboratory", "equipment", "purchase_request", "category", "selected_vendor", "created_by"), _trail_record),
    "invoice": (Invoice.objects.select_related("vendor", "recorded_by", "variance_reviewed_by", "procurement_record__department"), _trail_invoice),
    "asset": (Asset.objects.select_related("department", "laboratory", "equipment", "category", "procurement_record", "vendor", "custodian", "created_by"), _trail_asset),
    "requirement": (PlanRequirement.objects.select_related("department", "laboratory", "equipment", "category", "raised_by", "proposal"), _trail_requirement),
    "proposal": (PlanProposal.objects.select_related("department", "created_by"), _trail_proposal),
    "amc": (AMCServiceRecord.objects.select_related("department", "equipment", "asset", "vendor", "created_by"), _trail_amc),
}


@pm_api(["GET"])
def trail(request, kind: str, pk: int):
    scope = request.pm_scope
    entry = TRAILS.get(kind)
    if entry is None:
        raise not_found()
    qs, build = entry
    allowed = [d for d in scope.department_ids() if scope.has_perm(d, P.REPORTS)]
    obj = qs.filter(pk=pk, department_id__in=allowed).first()
    if obj is None:
        raise not_found()
    data, related, pairs = build(obj)
    return Response({"kind": kind, "object": data, "related": related, "audit": _audit_for(pairs)})
