"""Mode of procurement (GFR 2017 Rules 149, 154, 155, 161-166) and forwarding bills to the Accounts In Charge.

Thresholds are per-department settings (defaults follow the GFR 2017 amendments):
direct purchase ≤ ``direct_purchase_limit``; Local Purchase Committee ≤ ``purchase_committee_limit``;
limited tender ≤ ``limited_tender_limit``; above that an open tender. Goods / services available on GeM must be
bought through GeM (Rule 149) — the suggestion says so and the officer records the GeM reference.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import access, audit, notify
from . import constants as c
from .api import choice, parse_bool, req_str
from .errors import ProcurementError, forbidden
from .models import Invoice, ProcurementRecord

M = c.PurchaseMode
P = c.OfficePermission


def suggest(cfg, amount: Decimal | None) -> dict:
    amount = amount or Decimal("0.00")
    if amount <= cfg.direct_purchase_limit:
        mode, why = M.DIRECT, f"Up to ₹{cfg.direct_purchase_limit}: direct purchase on a certificate of reasonable price (Rule 154)."
    elif amount <= cfg.purchase_committee_limit:
        mode, why = M.PURCHASE_COMMITTEE, f"Up to ₹{cfg.purchase_committee_limit}: Local Purchase Committee (Rule 155)."
    elif amount <= cfg.limited_tender_limit:
        mode, why = M.LIMITED_TENDER, f"Up to ₹{cfg.limited_tender_limit}: limited tender enquiry (Rule 162)."
    else:
        mode, why = M.OPEN_TENDER, f"Above ₹{cfg.limited_tender_limit}: advertised / open tender (Rule 161)."
    return {
        "amount": str(amount),
        "suggested": mode,
        "reason": why,
        "gem_note": "If the item or service is available on GeM, procurement through GeM is mandatory (Rule 149); "
                    "record the GeM order / bid number.",
        "thresholds": {
            "direct_purchase_limit": str(cfg.direct_purchase_limit),
            "purchase_committee_limit": str(cfg.purchase_committee_limit),
            "limited_tender_limit": str(cfg.limited_tender_limit),
        },
        "options": [{"value": m.value, "label": str(m.label)} for m in M],
    }


def for_record(rec: ProcurementRecord) -> dict:
    cfg = access.get_config(rec.department_id)
    return suggest(cfg, rec.approved_amount or rec.estimated_amount)


@transaction.atomic
def set_mode(scope, rec: ProcurementRecord, data: dict, *, request=None) -> ProcurementRecord:
    rec = ProcurementRecord.objects.select_for_update(of=("self",)).get(pk=rec.pk)
    cfg = access.require_config(rec.department_id)
    scope.require_perm(rec.department_id, P.PROCUREMENT)
    if rec.status in (c.ProcurementRecordStatus.COMPLETED, c.ProcurementRecordStatus.CANCELLED):
        raise ProcurementError("This record is closed.", code="closed")
    mode = choice(data.get("purchase_mode"), M.values, "purchase_mode")
    gem = req_str(data, "gem_reference", max_len=120, required=mode == M.GEM)
    suggested = suggest(cfg, rec.approved_amount or rec.estimated_amount)["suggested"]
    deviates = mode not in (suggested, M.GEM, M.RATE_CONTRACT)
    reason = req_str(data, "purchase_mode_reason", max_len=5000, required=deviates or mode in (M.SINGLE_TENDER, M.PROPRIETARY))
    before = {"purchase_mode": rec.purchase_mode, "gem_reference": rec.gem_reference}
    rec.purchase_mode, rec.purchase_mode_reason, rec.gem_reference = mode, reason, gem
    rec.save(update_fields=["purchase_mode", "purchase_mode_reason", "gem_reference", "updated_at"])
    audit.record(
        scope.user, "procurement.mode_set", rec, old=before,
        new={"purchase_mode": mode, "gem_reference": gem, "suggested": suggested}, reason=reason, request=request,
    )
    return rec


@transaction.atomic
def forward_invoice(scope, inv: Invoice, data: dict, *, request=None) -> Invoice:
    """Store / Office hands the bill (with receipt & inspection done) to the Accounts In Charge for payment."""
    inv = Invoice.objects.select_for_update(of=("self",)).select_related("procurement_record").get(pk=inv.pk)
    access.require_config(inv.department_id)
    dept = inv.department_id
    if not (scope.has_role(dept, c.ModuleRole.OC_STORES) or scope.has_perm(dept, P.INVOICES) or scope.has_perm(dept, P.PROCUREMENT)):
        raise forbidden("Only OC Stores or the Office can forward bills to Accounts.")
    undo = parse_bool(data.get("undo") or "")
    if undo:
        if not inv.forwarded_to_accounts_at:
            raise ProcurementError("The bill has not been forwarded.", code="invalid_status")
        if inv.payment_status != c.PaymentStatus.UNPAID:
            raise ProcurementError("Payment has started; the bill cannot be recalled.", code="invalid_status")
        reason = req_str(data, "note", max_len=2000)
        inv.forwarded_to_accounts_at, inv.forwarded_by, inv.forward_note = None, None, ""
        inv.save(update_fields=["forwarded_to_accounts_at", "forwarded_by", "forward_note", "updated_at"])
        audit.record(scope.user, "invoice.recalled_from_accounts", inv, reason=reason, request=request)
        return inv
    if inv.forwarded_to_accounts_at:
        raise ProcurementError("The bill is already with Accounts.", code="already_forwarded")
    inv.forwarded_to_accounts_at = timezone.now()
    inv.forwarded_by = scope.user
    inv.forward_note = req_str(data, "note", max_len=2000, required=False)
    inv.save(update_fields=["forwarded_to_accounts_at", "forwarded_by", "forward_note", "updated_at"])
    audit.record(scope.user, "invoice.forwarded_to_accounts", inv, new={"note": inv.forward_note}, request=request)
    rec = inv.procurement_record
    notify.notify(
        notify.department_role_users(dept, c.ModuleRole.ACCOUNTS) + notify.office_users(dept, P.PAYMENTS),
        department_id=dept,
        title=f"Bill for payment: {inv.invoice_number}",
        message=f"{rec.number} — {rec.title}: ₹{inv.total_amount}. {inv.forward_note}".strip(),
        link=f"/procurement/records/{rec.pk}",
        event="invoice_forwarded",
        actor=scope.user,
        extra={"invoice_id": inv.pk, "record_id": rec.pk},
    )
    return inv
