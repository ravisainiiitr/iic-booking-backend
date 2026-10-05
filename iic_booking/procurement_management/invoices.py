"""Invoices / bills: GST maths, duplicate guard, variance against the approved amount, review and payments."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from . import access, audit
from . import constants as c
from .api import choice, parse_day, parse_int, parse_money, parse_qty, parse_rate, req_str
from .errors import ProcurementError, forbidden
from .models import ApprovalAction, Invoice, InvoiceLine, Item, Vendor

CENT = Decimal("0.01")
HUNDRED = Decimal("100")
V = c.VarianceStatus
P = c.OfficePermission
OPEN_VARIANCE = frozenset({V.OFFICE_REVIEW, V.REAPPROVAL_REQUIRED})


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _lines(dept, raw_lines) -> list[InvoiceLine]:
    if not isinstance(raw_lines, list):
        raise ProcurementError("lines must be a list.", code="invalid", field="lines")
    out = []
    for idx, raw in enumerate(raw_lines[:200]):
        raw = raw or {}
        item = None
        if raw.get("item_id") not in (None, ""):
            item = Item.objects.filter(pk=parse_int(raw.get("item_id"), "item_id"), department=dept, is_archived=False).first()
            if item is None:
                raise ProcurementError("Unknown item.", code="invalid_item", field=f"lines[{idx}].item_id")
        desc = str(raw.get("description") or (item.name if item else "")).strip()
        if not desc:
            raise ProcurementError("Each bill line needs a description.", code="required", field=f"lines[{idx}].description")
        quantity = parse_qty(raw.get("quantity"), f"lines[{idx}].quantity")
        price = parse_money(raw.get("unit_price"), f"lines[{idx}].unit_price")
        rate = parse_rate(raw.get("gst_rate"), f"lines[{idx}].gst_rate")
        taxable = q2(quantity * price)
        gst = q2(taxable * rate / HUNDRED)
        out.append(
            InvoiceLine(
                item=item, description=desc[:255], quantity=quantity, uom=str(raw.get("uom") or (item.uom if item else "Nos"))[:30],
                unit_price=price, gst_rate=rate, taxable_amount=taxable, gst_amount=gst, line_total=taxable + gst,
            )
        )
    return out


def compute_variance(invoice: Invoice, cfg) -> None:
    approved = invoice.approved_amount
    if approved is None or approved <= 0:
        invoice.variance_amount = Decimal("0.00")
        invoice.variance_percent = Decimal("0.00")
        invoice.variance_status = V.NOT_APPLICABLE
        return
    invoice.variance_amount = invoice.total_amount - approved
    invoice.variance_percent = q2(invoice.variance_amount * HUNDRED / approved)
    if invoice.variance_percent <= cfg.variance_tolerance_percent:
        invoice.variance_status = V.WITHIN_TOLERANCE
    elif cfg.variance_action == c.VarianceAction.FLAG_ONLY:
        invoice.variance_status = V.FLAGGED
    elif cfg.variance_action == c.VarianceAction.REAPPROVAL:
        invoice.variance_status = V.REAPPROVAL_REQUIRED
    else:
        invoice.variance_status = V.OFFICE_REVIEW


@transaction.atomic
def record_invoice(scope, record, data: dict, *, approved_amount=None, request=None) -> Invoice:
    """Create a bill for ``record``. Totals are computed server-side from lines (or header amounts)."""
    cfg = access.require_config(record.department)
    dept = record.department
    vendor = None
    if data.get("vendor_id") not in (None, ""):
        vendor = Vendor.objects.filter(pk=parse_int(data.get("vendor_id"), "vendor_id"), department=dept, is_archived=False).first()
        if vendor is None:
            raise ProcurementError("Unknown vendor.", code="invalid_vendor", field="vendor_id")
    vendor_name = req_str(data, "vendor_name", required=vendor is None)
    number = req_str(data, "invoice_number", max_len=80)
    inv_date = parse_day(data.get("invoice_date"), "invoice_date", required=True)
    if inv_date > timezone.localdate():
        raise ProcurementError("The bill date cannot be in the future.", code="invalid_date", field="invoice_date")
    supply = choice(data.get("supply_type"), c.SupplyType.values, "supply_type", default=c.SupplyType.INTRA_STATE)
    dup = Invoice.objects.filter(department=dept, invoice_number__iexact=number, is_archived=False)
    dup = dup.filter(vendor=vendor) if vendor else dup.filter(vendor__isnull=True, vendor_name_text__iexact=vendor_name)
    if dup.exists():
        raise ProcurementError("This bill number is already recorded for the vendor.", code="duplicate_invoice", field="invoice_number")
    inv = Invoice(
        department=dept, procurement_record=record, vendor=vendor, vendor_name_text=vendor_name if not vendor else vendor.name,
        invoice_number=number, invoice_date=inv_date, supply_type=supply, recorded_by=scope.user,
        remarks=req_str(data, "remarks", max_len=5000, required=False),
    )
    lines = _lines(dept, data.get("lines")) if data.get("lines") not in (None, "", []) else []
    other = parse_money(data.get("other_charges"), "other_charges", required=False) or Decimal("0.00")
    if lines:
        taxable = sum((x.taxable_amount for x in lines), Decimal("0.00"))
        gst = sum((x.gst_amount for x in lines), Decimal("0.00"))
        if supply == c.SupplyType.INTER_STATE:
            inv.igst_amount = gst
        else:
            inv.cgst_amount = q2(gst / 2)
            inv.sgst_amount = gst - inv.cgst_amount
    else:
        taxable = parse_money(data.get("taxable_amount"), "taxable_amount")
        if supply == c.SupplyType.INTER_STATE:
            inv.igst_amount = parse_money(data.get("igst_amount"), "igst_amount", required=False) or Decimal("0.00")
        else:
            inv.cgst_amount = parse_money(data.get("cgst_amount"), "cgst_amount", required=False) or Decimal("0.00")
            inv.sgst_amount = parse_money(data.get("sgst_amount"), "sgst_amount", required=False) or Decimal("0.00")
    inv.taxable_amount = taxable
    inv.other_charges = other
    inv.total_amount = taxable + inv.cgst_amount + inv.sgst_amount + inv.igst_amount + other
    if inv.total_amount <= 0:
        raise ProcurementError("The bill total must be more than zero.", code="invalid_total")
    inv.approved_amount = approved_amount if approved_amount is not None else record.approved_amount
    compute_variance(inv, cfg)
    inv.save()
    for line in lines:
        line.invoice = inv
    InvoiceLine.objects.bulk_create(lines)
    audit.record(
        scope.user, "invoice.recorded", inv, department=dept,
        new={"record": record.number, "invoice_number": number, "total": inv.total_amount, "approved": inv.approved_amount,
             "variance_percent": inv.variance_percent, "variance_status": inv.variance_status},
        request=request,
    )
    if record.purchase_request_id:
        r = record.purchase_request
        ApprovalAction.objects.create(
            department_id=dept.pk, purchase_request=r, stage=c.ApprovalStage.OFFICE, action=c.ApprovalActionType.INVOICE_RECORDED,
            from_status=r.status, to_status=r.status, actor=scope.user, actor_role=c.ModuleRole.OFFICE,
            comments=f"Bill {number} for ₹{inv.total_amount}", amount=inv.total_amount,
        )
        if inv.variance_status == V.REAPPROVAL_REQUIRED:
            ApprovalAction.objects.create(
                department_id=dept.pk, purchase_request=r, stage=c.ApprovalStage.SYSTEM,
                action=c.ApprovalActionType.REAPPROVAL_REQUIRED, from_status=r.status, to_status=r.status, actor=scope.user,
                comments=f"Bill exceeds approved amount by {inv.variance_percent}%", amount=inv.variance_amount,
            )
    from .stock import receive_for_invoice

    receive_for_invoice(scope, inv, request=request)
    if inv.variance_status in OPEN_VARIANCE:
        from . import notify

        users = notify.office_users(dept.pk, P.INVOICES)
        if inv.variance_status == V.REAPPROVAL_REQUIRED:
            users += notify.hod_users(dept.pk)
        notify.notify(
            users, department_id=dept.pk, title=f"Bill variance: {record.number}",
            message=f"Bill {number} (₹{inv.total_amount}) differs from the approved ₹{inv.approved_amount} by {inv.variance_percent}%.",
            link=f"/procurement/records/{record.pk}", event="invoice_variance", actor=scope.user,
            extra={"record_id": record.pk, "invoice_id": inv.pk},
        )
    return inv


@transaction.atomic
def review_variance(scope, inv: Invoice, *, note: str, request=None) -> Invoice:
    """Clear an open variance. Office review needs the ``invoices`` permission; re-approval needs the HOD.
    The person who recorded the bill cannot clear its variance."""
    inv = (
        Invoice.objects.select_for_update(of=("self",))
        .select_related("procurement_record", "procurement_record__purchase_request")
        .get(pk=inv.pk)
    )
    access.require_config(inv.department_id)
    if inv.variance_status not in OPEN_VARIANCE:
        raise ProcurementError("This bill has no open variance.", code="invalid_status")
    if inv.recorded_by_id == scope.user.pk:
        raise forbidden("The person who recorded the bill cannot clear its variance.")
    r = inv.procurement_record.purchase_request
    if r is not None and r.requested_by_id == scope.user.pk:
        raise forbidden("You cannot clear the variance on your own request.")
    if inv.variance_status == V.REAPPROVAL_REQUIRED:
        if not scope.has_role(inv.department_id, c.ModuleRole.HOD):
            raise forbidden("Only the HOD can re-approve this bill.")
    elif not scope.has_perm(inv.department_id, P.INVOICES):
        raise forbidden()
    before = inv.variance_status
    inv.variance_status = V.CLEARED
    inv.variance_reviewed_by = scope.user
    inv.variance_reviewed_at = timezone.now()
    inv.variance_review_note = note
    inv.save()
    audit.record(scope.user, "invoice.variance_cleared", inv, old={"variance_status": before},
                 new={"variance_status": inv.variance_status}, reason=note, request=request)
    if r is not None:
        ApprovalAction.objects.create(
            department_id=inv.department_id, purchase_request=r,
            stage=c.ApprovalStage.HOD if before == V.REAPPROVAL_REQUIRED else c.ApprovalStage.OFFICE,
            action=c.ApprovalActionType.APPROVE, from_status=r.status, to_status=r.status, actor=scope.user,
            actor_role=c.ModuleRole.HOD if before == V.REAPPROVAL_REQUIRED else c.ModuleRole.OFFICE,
            comments=f"Bill variance cleared: {note}", amount=inv.total_amount,
        )
    from .purchases import try_complete

    try_complete(scope, inv.procurement_record, request=request)
    return inv


@transaction.atomic
def record_payment(scope, inv: Invoice, data: dict, *, request=None) -> Invoice:
    inv = Invoice.objects.select_for_update(of=("self",)).select_related("procurement_record").get(pk=inv.pk)
    access.require_config(inv.department_id)
    scope.require_perm(inv.department_id, P.PAYMENTS)
    if inv.variance_status in OPEN_VARIANCE:
        raise ProcurementError("Clear the bill variance before recording payment.", code="variance_open")
    amount = parse_money(data.get("amount"), "amount", allow_zero=False)
    if inv.paid_amount + amount > inv.total_amount:
        raise ProcurementError("Payment exceeds the bill total.", code="overpayment", field="amount")
    pay_date = parse_day(data.get("payment_date"), "payment_date", required=True)
    if pay_date > timezone.localdate():
        raise ProcurementError("The payment date cannot be in the future.", code="invalid_date", field="payment_date")
    reference = req_str(data, "payment_reference", max_len=120)
    before = {"paid_amount": inv.paid_amount, "payment_status": inv.payment_status}
    inv.paid_amount += amount
    inv.payment_status = c.PaymentStatus.PAID if inv.paid_amount == inv.total_amount else c.PaymentStatus.PARTIALLY_PAID
    inv.save()
    rec = inv.procurement_record
    totals = [(i.paid_amount, i.total_amount) for i in rec.invoices.filter(is_archived=False)]
    rec.paid_amount = sum((p for p, _ in totals), Decimal("0.00"))
    all_paid = all(p == t for p, t in totals)
    rec.payment_status = c.PaymentStatus.PAID if all_paid else c.PaymentStatus.PARTIALLY_PAID
    rec.payment_date = pay_date
    rec.payment_reference = reference
    if all_paid and c.ProcurementStep.PAYMENT not in (rec.completed_steps or []):
        rec.completed_steps = [*(rec.completed_steps or []), c.ProcurementStep.PAYMENT.value]
    rec.save()
    audit.record(
        scope.user, "invoice.payment_recorded", inv, old=before,
        new={"paid_amount": inv.paid_amount, "payment_status": inv.payment_status, "amount": amount,
             "payment_date": pay_date, "reference": reference},
        request=request,
    )
    from .purchases import try_complete

    try_complete(scope, rec, request=request)
    return inv
