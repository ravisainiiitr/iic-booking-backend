"""Budget allocations and budget vs actual.

Semantics (per financial year and funding type):

* Budget    = live allocations.
* Approved  = approved amounts of requests that reached purchase approval (approved → completed; stores issues are
              excluded because they spend no money) + approved plan / non-plan requirements.
* Committed = PO value of live procurement records with a PO and no bill yet.
* Purchased = totals of live bills on live (not cancelled) procurement records.
* Paid      = amounts paid against those bills.
* Balance   = Budget − Purchased − Committed.

Records and bills are attributed to the record's financial year and funding type.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, Sum
from django.utils import timezone

from . import access, audit
from . import constants as c
from .api import choice, parse_int, parse_money, req_reason, req_str
from .errors import ProcurementError, not_found
from .fy import is_valid_fy_label
from .models import BudgetAllocation, Invoice, ItemCategory, PlanRequirement, ProcurementRecord, PurchaseRequest

P = c.OfficePermission
RS = c.RequestStatus
RQ = c.RequirementStatus
ZERO = Decimal("0.00")
APPROVED_REQUEST_STATUSES = (RS.APPROVED, RS.IN_PROCUREMENT, RS.AWAITING_INVOICE, RS.AWAITING_RECEIPT, RS.COMPLETED)
APPROVED_REQUIREMENT_STATUSES = (RQ.APPROVED, RQ.PROCUREMENT_IN_PROGRESS, RQ.PROCURED)


def clean_fy(raw, name="financial_year") -> str:
    value = str(raw or "").strip()
    if not is_valid_fy_label(value):
        raise ProcurementError("Financial year must look like 2026-27.", code="invalid_fy", field=name)
    return value


def _require(scope, dept_id):
    access.require_config(dept_id)
    scope.require_perm(dept_id, P.BUDGET)


@transaction.atomic
def create_allocation(scope, department, data: dict, *, request=None) -> BudgetAllocation:
    _require(scope, department.pk)
    category = None
    cat_id = parse_int(data.get("category_id"), "category_id")
    if cat_id:
        category = ItemCategory.objects.filter(pk=cat_id, department=department).first()
        if category is None:
            raise not_found("Category not found.")
    lab = None
    if data.get("laboratory_id") not in (None, ""):
        from .requests_service import _lookup_laboratory

        lab = _lookup_laboratory(data.get("laboratory_id"))
        if lab.department_id != department.pk:
            raise not_found("Laboratory not found.")
    row = BudgetAllocation.objects.create(
        department=department, financial_year=clean_fy(data.get("financial_year")),
        funding_type=choice(data.get("funding_type"), c.FundingType.values, "funding_type"), laboratory=lab, category=category,
        amount=parse_money(data.get("amount"), "amount"), reference=req_str(data, "reference", max_len=120, required=False),
        remarks=req_str(data, "remarks", max_len=2000, required=False), created_by=scope.user,
    )
    audit.record(scope.user, "budget.allocated", row, new=audit.snapshot(row, ["financial_year", "funding_type", "amount", "category", "laboratory", "reference"]), request=request)
    return row


@transaction.atomic
def update_allocation(scope, row: BudgetAllocation, data: dict, *, request=None) -> BudgetAllocation:
    row = BudgetAllocation.objects.select_for_update().get(pk=row.pk)
    _require(scope, row.department_id)
    if row.is_archived:
        raise ProcurementError("This allocation is archived.", code="archived")
    reason = req_reason(data)
    fields = ["amount", "reference", "remarks"]
    before = audit.snapshot(row, fields)
    if "amount" in data:
        row.amount = parse_money(data.get("amount"), "amount")
    if "reference" in data:
        row.reference = req_str(data, "reference", max_len=120, required=False)
    if "remarks" in data:
        row.remarks = req_str(data, "remarks", max_len=2000, required=False)
    row.save()
    old, new = audit.diff(before, audit.snapshot(row, fields))
    audit.record(scope.user, "budget.updated", row, old=old, new=new, reason=reason, request=request)
    return row


@transaction.atomic
def archive_allocation(scope, row: BudgetAllocation, data: dict, *, request=None) -> BudgetAllocation:
    row = BudgetAllocation.objects.select_for_update().get(pk=row.pk)
    _require(scope, row.department_id)
    if row.is_archived:
        raise ProcurementError("Already archived.", code="archived")
    reason = req_reason(data)
    row.is_archived, row.archived_at, row.archived_by, row.archive_reason = True, timezone.now(), scope.user, reason
    row.save(update_fields=["is_archived", "archived_at", "archived_by", "archive_reason", "updated_at"])
    audit.record(scope.user, "budget.archived", row, reason=reason, request=request)
    return row


def _sum(qs, field) -> Decimal:
    return qs.aggregate(t=Sum(field))["t"] or ZERO


def budget_vs_actual(department_id: int, financial_year: str, *, category_id: int | None = None) -> dict:
    """One row per funding type plus a total row. Optional category filter narrows every column."""
    rows = []
    totals = dict.fromkeys(("budget", "approved", "committed", "purchased", "paid", "balance"), ZERO)
    live_records = ProcurementRecord.objects.filter(department_id=department_id, financial_year=financial_year, is_archived=False).exclude(
        status=c.ProcurementRecordStatus.CANCELLED
    )
    if category_id:
        live_records = live_records.filter(category_id=category_id)
    for funding in c.FundingType.values:
        alloc = BudgetAllocation.objects.filter(department_id=department_id, financial_year=financial_year, funding_type=funding, is_archived=False)
        reqs = PurchaseRequest.objects.filter(
            department_id=department_id, financial_year=financial_year, funding_type=funding, status__in=APPROVED_REQUEST_STATUSES, is_archived=False
        )
        plan = PlanRequirement.objects.filter(
            department_id=department_id, financial_year=financial_year, funding_type=funding, status__in=APPROVED_REQUIREMENT_STATUSES, is_archived=False
        )
        if category_id:
            alloc, reqs, plan = alloc.filter(category_id=category_id), reqs.filter(category_id=category_id), plan.filter(category_id=category_id)
        records = live_records.filter(funding_type=funding)
        bills = Invoice.objects.filter(procurement_record__in=records, is_archived=False)
        has_bill = Invoice.objects.filter(procurement_record=OuterRef("pk"), is_archived=False)
        approved_reqs = sum(((r.approved_amount if r.approved_amount is not None else r.estimated_total) for r in reqs.only("approved_amount", "estimated_total")), ZERO)
        row = {
            "funding_type": funding,
            "budget": _sum(alloc, "amount"),
            "approved": approved_reqs + _sum(plan, "approved_amount"),
            "committed": _sum(records.filter(po_amount__isnull=False).annotate(b=Exists(has_bill)).filter(b=False), "po_amount"),
            "purchased": _sum(bills, "total_amount"),
            "paid": _sum(bills, "paid_amount"),
        }
        row["balance"] = row["budget"] - row["purchased"] - row["committed"]
        row["utilisation_percent"] = _pct(row["purchased"] + row["committed"], row["budget"])
        for k in totals:
            totals[k] += row[k]
        rows.append(row)
    totals["funding_type"] = "TOTAL"
    totals["utilisation_percent"] = _pct(totals["purchased"] + totals["committed"], totals["budget"])
    return {"financial_year": financial_year, "rows": rows, "total": totals}


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    if not whole:
        return None
    return (part * 100 / whole).quantize(Decimal("0.01"))


def has_budget_visibility(scope, dept_id) -> bool:
    return scope.has_perm(dept_id, P.BUDGET) or scope.has_perm(dept_id, P.REPORTS)


def allocations_q(scope) -> Q:
    ids = [d for d in scope.department_ids() if has_budget_visibility(scope, d)]
    return Q(department_id__in=ids)
