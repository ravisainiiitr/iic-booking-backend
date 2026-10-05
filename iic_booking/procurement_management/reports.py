"""Tabular reports. Each report returns ``(title, headers, rows)``; the view renders JSON or CSV / XLSX / PDF.

All reports are scoped to one department and require the ``reports`` permission (HOD, Auditor, Main Admin,
OC Stores and Office users holding it). Filters: ``financial_year``, ``date_from`` / ``date_to``, ``status``.
"""

from __future__ import annotations

from django.db.models import Count, Q, Sum

from . import constants as c
from . import serializers as s
from .api import parse_day
from .budget import budget_vs_actual, clean_fy
from .errors import ProcurementError
from .fy import fy_label
from .models import (
    AMCServiceRecord,
    Asset,
    Invoice,
    ProcurementAuditLog,
    ProcurementRecord,
    PurchaseRequest,
    StockBalance,
    StockTransaction,
)

MAX_ROWS = 10000


class Filters:
    def __init__(self, params, cfg):
        self.fy = clean_fy(params["financial_year"]) if params.get("financial_year") else ""
        self.default_fy = cfg.current_financial_year or fy_label()
        self.date_from = parse_day(params.get("date_from"), "date_from")
        self.date_to = parse_day(params.get("date_to"), "date_to")
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ProcurementError("date_to is before date_from.", code="invalid_dates", field="date_to")
        self.statuses = [x for x in (params.get("status") or "").split(",") if x]

    def dates(self, qs, field):
        if self.date_from:
            qs = qs.filter(**{f"{field}__gte": self.date_from})
        if self.date_to:
            qs = qs.filter(**{f"{field}__lte": self.date_to})
        return qs

    def subtitle(self) -> str:
        parts = []
        if self.fy:
            parts.append(f"FY {self.fy}")
        if self.date_from or self.date_to:
            parts.append(f"{s.iso(self.date_from) or '…'} to {s.iso(self.date_to) or '…'}")
        if self.statuses:
            parts.append("Status: " + ", ".join(self.statuses))
        return " · ".join(parts)


def _name(user) -> str:
    return (s.user_brief(user) or {}).get("name", "")


def requests_report(dept, f: Filters):
    qs = PurchaseRequest.objects.filter(department=dept, is_archived=False).select_related("equipment", "request_type", "requested_by")
    if f.fy:
        qs = qs.filter(financial_year=f.fy)
    if f.statuses:
        qs = qs.filter(status__in=f.statuses)
    qs = f.dates(qs, "created_at__date").order_by("created_at")
    headers = ["Request no.", "Date", "Equipment", "Type", "Title", "Requested by", "Funding", "Status", "Estimated", "Approved", "Small purchase"]
    rows = [
        [r.number, s.iso(r.created_at.date()), r.equipment.name if r.equipment_id else "", r.request_type.name, r.title,
         _name(r.requested_by), r.funding_type, r.get_status_display(), s.m(r.estimated_total), s.m(r.approved_amount) or "",
         "Yes" if r.is_small_purchase else "No"]
        for r in qs[:MAX_ROWS]
    ]
    return "Purchase requests", headers, rows


def purchases_report(dept, f: Filters):
    qs = ProcurementRecord.objects.filter(department=dept, is_archived=False).select_related("selected_vendor", "purchase_request")
    if f.fy:
        qs = qs.filter(financial_year=f.fy)
    if f.statuses:
        qs = qs.filter(status__in=f.statuses)
    qs = f.dates(qs, "created_at__date").annotate(
        billed=Sum("invoices__total_amount", filter=Q(invoices__is_archived=False)),
        paid=Sum("invoices__paid_amount", filter=Q(invoices__is_archived=False)),
    ).order_by("created_at")
    headers = ["Record no.", "Date", "Origin", "Small purchase", "Title", "Request", "Vendor", "FY", "Funding", "Approved", "PO", "Billed", "Paid", "Status"]
    rows = [
        [x.number, s.iso(x.created_at.date()), x.get_origin_display(), "Yes" if x.is_small_purchase else "No", x.title,
         x.purchase_request.number if x.purchase_request_id else "", x.selected_vendor.name if x.selected_vendor_id else "",
         x.financial_year, x.funding_type, s.m(x.approved_amount) or "", s.m(x.po_amount) or "", s.m(x.billed or 0), s.m(x.paid or 0),
         x.get_status_display()]
        for x in qs[:MAX_ROWS]
    ]
    return "Purchases", headers, rows


def _invoices(dept, f: Filters):
    qs = Invoice.objects.filter(department=dept, is_archived=False).select_related("vendor", "procurement_record")
    if f.fy:
        qs = qs.filter(procurement_record__financial_year=f.fy)
    return f.dates(qs, "invoice_date")


def invoices_report(dept, f: Filters):
    qs = _invoices(dept, f)
    if f.statuses:
        qs = qs.filter(Q(variance_status__in=f.statuses) | Q(payment_status__in=f.statuses))
    headers = ["Bill no.", "Bill date", "Vendor", "GSTIN", "Record", "Taxable", "CGST", "SGST", "IGST", "Other", "Total",
               "Approved", "Variance %", "Variance status", "Paid", "Payment status"]
    rows = [
        [i.invoice_number, s.iso(i.invoice_date), i.vendor.name if i.vendor_id else i.vendor_name_text, i.vendor.gstin if i.vendor_id else "",
         i.procurement_record.number, s.m(i.taxable_amount), s.m(i.cgst_amount), s.m(i.sgst_amount), s.m(i.igst_amount),
         s.m(i.other_charges), s.m(i.total_amount), s.m(i.approved_amount) or "", s.m(i.variance_percent), i.get_variance_status_display(),
         s.m(i.paid_amount), i.get_payment_status_display()]
        for i in qs.order_by("invoice_date", "id")[:MAX_ROWS]
    ]
    return "Bills / GST register", headers, rows


def vendor_spend_report(dept, f: Filters):
    qs = (
        _invoices(dept, f)
        .values("vendor__name", "vendor__gstin", "vendor_name_text")
        .annotate(n=Count("id"), taxable=Sum("taxable_amount"), total=Sum("total_amount"), paid=Sum("paid_amount"))
        .order_by("-total")
    )
    headers = ["Vendor", "GSTIN", "Bills", "Taxable", "Total", "Paid"]
    rows = [
        [r["vendor__name"] or r["vendor_name_text"], r["vendor__gstin"] or "", r["n"], s.m(r["taxable"]), s.m(r["total"]), s.m(r["paid"])]
        for r in qs[:MAX_ROWS]
    ]
    return "Vendor-wise spend", headers, rows


def stock_report(dept, f: Filters):
    qs = StockBalance.objects.filter(department=dept).select_related("item", "laboratory").order_by("item__name")
    headers = ["Item code", "Item", "Store", "UoM", "Quantity", "Min level", "Reorder level", "Below min"]
    rows = [
        [b.item.code, b.item.name, b.laboratory.name if b.laboratory_id else "Central store", b.item.uom, s.q(b.quantity),
         s.q(b.min_level), s.q(b.reorder_level), "Yes" if b.min_level > 0 and b.quantity < b.min_level else "No"]
        for b in qs[:MAX_ROWS]
    ]
    return "Stock balances", headers, rows


def stock_ledger_report(dept, f: Filters):
    qs = f.dates(StockTransaction.objects.filter(department=dept).select_related("item", "laboratory", "performed_by", "issued_to"), "transaction_date")
    if f.statuses:
        qs = qs.filter(tx_type__in=f.statuses)
    headers = ["Txn no.", "Date", "Item", "Store", "Type", "Qty", "Balance after", "Unit cost", "Reference", "Issued to", "By", "Remarks"]
    rows = [
        [t.number, s.iso(t.transaction_date), t.item.name, t.laboratory.name if t.laboratory_id else "Central store",
         t.get_tx_type_display(), s.q(t.signed_quantity), s.q(t.balance_after), s.m(t.unit_cost) or "",
         f"{t.reference_type} {t.reference_number}".strip(), _name(t.issued_to), _name(t.performed_by), t.remarks]
        for t in qs.order_by("transaction_date", "id")[:MAX_ROWS]
    ]
    return "Stock ledger", headers, rows


def assets_report(dept, f: Filters):
    qs = Asset.objects.filter(department=dept, is_archived=False).select_related("category", "equipment", "custodian", "procurement_record")
    if f.fy:
        qs = qs.filter(financial_year=f.fy)
    if f.statuses:
        qs = qs.filter(status__in=f.statuses)
    qs = f.dates(qs, "purchase_date").order_by("number")
    headers = ["Asset no.", "Description", "Category", "Serial no.", "Equipment", "Location", "Custodian", "Status", "Purchase date",
               "Cost", "Capitalised", "Funding", "FY", "Record"]
    rows = [
        [a.number, a.description, a.category.name, a.serial_number, a.equipment.name if a.equipment_id else "", a.location,
         _name(a.custodian), a.get_status_display(), s.iso(a.purchase_date) or "", s.m(a.cost), "Yes" if a.is_capitalized else "No",
         a.funding_type, a.financial_year, a.procurement_record.number if a.procurement_record_id else ""]
        for a in qs[:MAX_ROWS]
    ]
    return "Asset register", headers, rows


def amc_report(dept, f: Filters):
    qs = AMCServiceRecord.objects.filter(department=dept, is_archived=False).select_related("equipment", "vendor")
    if f.statuses:
        qs = qs.filter(status__in=f.statuses)
    qs = f.dates(qs, "end_date").order_by("end_date")
    headers = ["AMC no.", "Equipment", "Type", "Vendor", "Reference", "Start", "End", "Value", "GST", "Total", "Status"]
    rows = [
        [r.number, r.equipment.name, r.get_contract_type_display(), r.vendor.name if r.vendor_id else "", r.contract_reference,
         s.iso(r.start_date), s.iso(r.end_date), s.m(r.contract_value), s.m(r.gst_amount), s.m(r.total_value), r.get_status_display()]
        for r in qs[:MAX_ROWS]
    ]
    return "AMC / service contracts", headers, rows


def budget_report(dept, f: Filters):
    fy = f.fy or f.default_fy
    data = budget_vs_actual(dept.pk, fy)
    headers = ["Funding", "Budget", "Approved", "Committed", "Purchased", "Paid", "Balance", "Utilisation %"]
    labels = dict(c.FundingType.choices)

    def row(r):
        return [str(labels.get(r["funding_type"], r["funding_type"])), s.m(r["budget"]), s.m(r["approved"]), s.m(r["committed"]),
                s.m(r["purchased"]), s.m(r["paid"]), s.m(r["balance"]), s.m(r["utilisation_percent"]) if r["utilisation_percent"] is not None else ""]

    return f"Budget vs actual — FY {fy}", headers, [row(r) for r in data["rows"]] + [row(data["total"])]


def audit_report(dept, f: Filters):
    qs = f.dates(ProcurementAuditLog.objects.filter(department=dept).select_related("actor"), "created_at__date")
    if f.statuses:
        qs = qs.filter(action__in=f.statuses)
    headers = ["When", "Actor", "Action", "Object", "Number", "Old", "New", "Reason", "IP"]
    rows = [
        [s.iso(x.created_at), _name(x.actor) or "System", x.action, f"{x.object_type} #{x.object_id}", x.object_number,
         _compact(x.old_value), _compact(x.new_value), x.reason, x.ip_address or ""]
        for x in qs.order_by("created_at", "id")[:MAX_ROWS]
    ]
    return "Audit trail", headers, rows


def _compact(value) -> str:
    if not value:
        return ""
    return "; ".join(f"{k}={v}" for k, v in value.items()) if isinstance(value, dict) else str(value)


REPORTS = {
    "requests": requests_report,
    "purchases": purchases_report,
    "invoices": invoices_report,
    "vendor-spend": vendor_spend_report,
    "stock": stock_report,
    "stock-ledger": stock_ledger_report,
    "assets": assets_report,
    "amc": amc_report,
    "budget": budget_report,
    "audit": audit_report,
}
