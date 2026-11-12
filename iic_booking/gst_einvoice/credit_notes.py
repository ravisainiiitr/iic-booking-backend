"""Credit notes for refunds of invoiced bookings, only where the Main Administrator allowed one for that invoice.

* Normal bookings get their tax invoice after the work is done, and a full refund before the 48-hour mark goes to the
  wallet before any invoice exists: neither meets a credit note, and nothing here changes them.
* An invoice issued early (the user's advance-invoice option, ``external_services`` ``InvoiceOption.EARLY``, accepted
  with its disclosure) or a lab-failure refund after completion meet a refund. Without a credit note the refund
  leaves the GST out (``refunds.quote``). The Main Administrator may allow a credit note for one issued portal invoice
  (``allow``, with a reason): the refund then returns the GST too, and recording the refund creates the credit note.
* A credit note is an ``EInvoice`` with ``doc_type`` CRN in its own continuous series (ECN/26-27/0001; GST document
  numbers are at most 16 characters), against the original invoice (number and date). Taxable value and the
  CGST/SGST or IGST split follow the original invoice, pro rata for a partial refund, and the total credited never
  exceeds the invoice.
  - Buyer with a GSTIN: registered with the e-invoice sandbox / mock as a CRN (same path as invoices).
  - No GSTIN: a PDF in the invoice's look, signed with SRIC's DSC through the signer utility.
* Credit notes feed the GSTR-1 export (``export_rows``): CDNUR for B2CL invoices, netted into B2CS otherwise, and
  the credit-note series in Table 13. B2B credit notes, like B2B invoices, reach GSTR-1 (CDNR) from the IRP.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import conf

logger = logging.getLogger(__name__)
ZERO = Decimal("0.00")
CENT = Decimal("0.01")
PREFIX = "ECN"


def _m(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


# --- permission (Main Administrator, one invoice at a time) ---------------------------------------------------------

def booking_invoice(booking_id):
    from .models import ISSUED_STATUSES, DocType, EInvoice

    return (EInvoice.objects.filter(booking_id=booking_id, doc_type=DocType.INVOICE, status__in=ISSUED_STATUSES)
            .order_by("-created_at").first())


def permission_for(invoice):
    from .models import CreditNotePermission

    perm = CreditNotePermission.objects.filter(invoice=invoice).first()
    return perm if perm is not None and perm.active else None


def permitted_for_booking(booking) -> bool:
    """True only when the module is on and the Main Administrator allowed a credit note for this booking's invoice."""
    if not conf.enabled():
        return False
    try:
        inv = booking_invoice(booking.pk)
    except Exception:  # noqa: BLE001 - never let this hook break a refund quote
        logger.exception("GST credit note: permission check failed for booking %s", getattr(booking, "pk", None))
        return False
    return inv is not None and permission_for(inv) is not None and remaining(inv)[0] > 0


def allow(actor, invoice_id, reason: str):
    from . import service
    from .models import CreditNotePermission, DocType, EInvoice

    service.require(service.can_administer, actor, "Only the Main Administrator can allow a credit note.")
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise service.EinvoiceError("Give the reason (at least 10 characters).", field="reason")
    with transaction.atomic():
        inv = EInvoice.objects.select_for_update().filter(pk=invoice_id).first()
        if inv is None:
            raise service.EinvoiceError("Invoice not found.", status=404, code="not_found")
        if inv.doc_type != DocType.INVOICE or not inv.issued or not inv.booking_id:
            raise service.EinvoiceError("A credit note can be allowed only for an issued invoice of a booking.",
                                        code="not_allowed")
        perm = CreditNotePermission.objects.filter(invoice=inv).first()
        if perm is not None and perm.active:
            raise service.EinvoiceError("A credit note is already allowed for this invoice.", code="already_allowed")
        if perm is not None:  # re-allowed after a revocation: a fresh decision, the old one stays in the history
            perm.enabled_by, perm.reason, perm.enabled_at = actor, reason[:500], timezone.now()
            perm.revoked_by, perm.revoked_at, perm.revoke_reason = None, None, ""
            perm.save()
        else:
            perm = CreditNotePermission.objects.create(invoice=inv, enabled_by=actor, reason=reason[:500])
        service._event(inv, "credit_note_allowed", actor, before=inv.status,
                       note=f"Main Administrator allowed a credit note on refund: {reason}")
    return perm


def revoke(actor, invoice_id, reason: str):
    from . import service
    from .models import CreditNotePermission

    service.require(service.can_administer, actor, "Only the Main Administrator can withdraw a credit note.")
    if len((reason or "").strip()) < 5:
        raise service.EinvoiceError("Give the reason (at least 5 characters).", field="reason")
    with transaction.atomic():
        perm = CreditNotePermission.objects.select_for_update().select_related("invoice").filter(
            invoice_id=invoice_id, revoked_at__isnull=True).first()
        if perm is None:
            raise service.EinvoiceError("No credit note is allowed for this invoice.", status=404, code="not_found")
        perm.revoked_by, perm.revoked_at, perm.revoke_reason = actor, timezone.now(), reason.strip()[:500]
        perm.save(update_fields=["revoked_by", "revoked_at", "revoke_reason"])
        service._event(perm.invoice, "credit_note_withdrawn", actor, before=perm.invoice.status,
                       note=f"Credit note no longer allowed: {perm.revoke_reason}")
    return perm


# --- amounts ---------------------------------------------------------------------------------------------------------

def remaining(inv) -> tuple[Decimal, Decimal]:
    """(taxable, total) of the invoice not yet credited by issued or pending credit notes."""
    from .models import InvoiceStatus

    notes = inv.credit_notes.exclude(status=InvoiceStatus.REJECTED)
    taxable = inv.taxable_value - sum((n.taxable_value for n in notes), ZERO)
    total = inv.total - sum((n.total for n in notes), ZERO)
    return max(taxable, ZERO), max(total, ZERO)


def split(inv, credit_taxable: Decimal) -> list[dict]:
    """The credit, line by line in the invoice's proportions: taxable and each tax pro rata to the invoice."""
    lines = list(inv.lines.all())
    if not lines or inv.taxable_value <= 0:
        return []
    ratio = min(Decimal(1), credit_taxable / inv.taxable_value)
    out, used = [], ZERO
    for i, ln in enumerate(lines):
        last = i == len(lines) - 1
        tx = _m(credit_taxable - used) if last else _m(ln.taxable_value * ratio)
        used += tx
        part = (tx / ln.taxable_value) if ln.taxable_value else Decimal(0)
        out.append({"line": ln, "taxable": tx, "cgst": _m(ln.cgst * part), "sgst": _m(ln.sgst * part),
                    "igst": _m(ln.igst * part)})
    return out


# --- the refund hook ------------------------------------------------------------------------------------------------

def on_refund_recorded(refund_id) -> None:
    """Called after a refund is committed. Creates the credit note when (and only when) one is allowed."""
    if not conf.enabled():
        return
    try:
        create_for_refund(refund_id)
    except Exception:  # noqa: BLE001 - the refund stands; the credit note can be retried from the invoice screen
        logger.exception("GST credit note: could not be created for refund %s", refund_id)


def create_for_refund(refund_id, actor=None):
    from iic_booking.external_services.models import ExternalRefund

    from . import numbering, service
    from .models import DocType, EInvoice, EInvoiceLine, InvoiceStatus

    refund = ExternalRefund.objects.select_related("booking").filter(pk=refund_id).first()
    if refund is None or EInvoice.objects.filter(refund=refund).exists():
        return None
    inv = booking_invoice(refund.booking_id)
    if inv is None or permission_for(inv) is None or not refund.gst_refunded:
        return None
    left_taxable, left_total = remaining(inv)
    credit_taxable = min(_m(refund.refund_base), left_taxable)
    if credit_taxable <= 0:
        return None
    parts = split(inv, credit_taxable)
    taxes = {k: sum((p[k] for p in parts), ZERO) for k in ("cgst", "sgst", "igst")}
    total = min(_m(credit_taxable + taxes["cgst"] + taxes["sgst"] + taxes["igst"]), left_total)
    try:
        with transaction.atomic():
            note = EInvoice.objects.create(
                doc_type=DocType.CREDIT_NOTE, original_invoice=inv, refund=refund, provider=conf.provider(),
                status=InvoiceStatus.PENDING_REVIEW, supply_type=inv.supply_type,
                draft_reference=numbering.new_draft_reference(), booking=inv.booking, is_demo=inv.is_demo,
                customer=inv.customer, booking_reference=inv.booking_reference, fbr_number=inv.fbr_number,
                equipment_name=inv.equipment_name, department_name=inv.department_name,
                receipt_number=inv.receipt_number, receipt_date=inv.receipt_date,
                supplier_state_code=inv.supplier_state_code, place_of_supply=inv.place_of_supply,
                inter_state=inv.inter_state, taxable_value=credit_taxable, cgst=taxes["cgst"], sgst=taxes["sgst"],
                igst=taxes["igst"], total=total, round_off=_m(total - credit_taxable - sum(taxes.values(), ZERO)),
                credit_reason=(f"Refund of {refund.refund_amount} ({refund.get_origin_display()})")[:300],
                created_by=actor)
            for n, p in enumerate(parts, start=1):
                ln = p["line"]
                EInvoiceLine.objects.create(
                    invoice=note, sl_no=n, description=ln.description, detail=f"Credit against {inv.invoice_number}",
                    sac_code=ln.sac_code, quantity=Decimal("1"), unit=ln.unit, unit_price=p["taxable"],
                    taxable_value=p["taxable"], gst_rate=ln.gst_rate, cgst=p["cgst"], sgst=p["sgst"],
                    igst=p["igst"], total=p["taxable"] + p["cgst"] + p["sgst"] + p["igst"], tax_rule=ln.tax_rule)
            service._event(note, "prepared", actor, note=f"Credit note against {inv.invoice_number} for refund "
                                                          f"{refund.pk} (allowed by the Main Administrator).")
            service._event(inv, "credit_note_prepared", actor, before=inv.status,
                           note=f"Credit note {note.draft_reference} for refund {refund.pk}.")
    except IntegrityError:
        return EInvoice.objects.filter(refund=refund).first()
    return service.finalise(note, actor, note="Credit note allowed by the Main Administrator.")


# --- GSTR-1 ---------------------------------------------------------------------------------------------------------

def export_rows(start: date, end: date):
    """Issued B2C and B2B credit notes dated within [start, end], as ``gstr1.CreditNoteRow``."""
    from .gstr1 import B2CL_LIMIT, CreditNoteRow
    from .models import ISSUED_STATUSES, DocType, EInvoice

    rows = []
    qs = (EInvoice.objects.filter(doc_type=DocType.CREDIT_NOTE, status__in=ISSUED_STATUSES, invoice_date__gte=start,
                                  invoice_date__lte=end).select_related("original_invoice").prefetch_related("lines")
          .order_by("invoice_number"))
    for note in qs:
        orig = note.original_invoice
        by_rate = {}
        for ln in note.lines.all():
            r = by_rate.setdefault(ln.gst_rate, [ZERO, ZERO, ZERO, ZERO])
            r[0] += ln.taxable_value
            r[1] += ln.igst
            r[2] += ln.cgst
            r[3] += ln.sgst
        rows.append(CreditNoteRow(
            number=note.invoice_number, note_date=note.invoice_date, original_number=orig.invoice_number,
            original_date=orig.invoice_date, supply_type=note.supply_type,
            original_b2cl=bool(orig.inter_state and orig.total > B2CL_LIMIT), inter_state=note.inter_state,
            pos=note.place_of_supply, value=note.total,
            lines=[(rt, v[0], v[1], v[2], v[3]) for rt, v in sorted(by_rate.items())],
            sac=(note.lines.first().sac_code if note.lines.exists() else "998397")))
    return rows
