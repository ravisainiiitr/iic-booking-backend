"""The invoicing flow (Dean SRIC proposal, Figure 1), built on the existing external-services invoice request.

    booking completed / early request
        -> external_services.SricInvoiceRequest (snapshot of booking, billing, payments)   [existing]
        -> prepare_from_request: buyer, lines (SAC + rate from SRIC's TaxRule), GST split, review rules
        -> rules pass (or SRIC approves)  -> number from the portal series (final)
             B2B (valid GSTIN): e-invoice JSON -> NIC sandbox / mock -> IRN + signed QR -> PDF
             B2C (no GSTIN):    PDF -> signatory's DSC on their own computer -> signed PDF checked (dsc)
        -> deliver: OfficialInvoiceDocument on the same request, buyer notified                [existing]

The weekly SRIC e-mail batch skips a request that has a portal invoice (unless SRIC rejected it, in which case the
request goes the manual way as before). SANDBOX ONLY: requests are taken up for test accounts' bookings; demo
invoices are made from demo customers. Nothing here changes a booking, payment or receipt. There is no
cancellation: an issued invoice stands.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import date
from decimal import Decimal

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import conf, dsc, numbering
from . import payload as payload_mod
from . import pdf as pdf_mod
from . import tax as tax_mod
from .gstin import STATE_CODES, state_code_for_name, state_code_of
from .gstin import validate as validate_gstin
from .models import (
    CustomerType,
    EInvoice,
    EInvoiceEvent,
    EInvoiceLine,
    EinvoiceSettings,
    GstCustomer,
    GstinStatus,
    InvoiceStatus,
    IrpAttempt,
    IrpOperation,
    IrpOutcome,
    SignerCertificate,
    SignerDevice,
    SupplyType,
    TaxRule,
)

logger = logging.getLogger(__name__)

DEFAULT_RULE = {"key": "testing-analysis", "label": "Testing and analysis services", "sac_code": "998397",
                "description": "Research and development services (testing / analysis)", "gst_rate": Decimal("18.00")}
PORTAL_TOLERANCE = Decimal("1.00")


class EinvoiceError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid", field: str = ""):
        super().__init__(message)
        self.message, self.status, self.code, self.field = message, status, code, field

    def payload(self) -> dict:
        body = {"error": self.message, "code": self.code}
        if self.field:
            body["field"] = self.field
        return body


# --- access ------------------------------------------------------------------------------------------------------------

def _is_main_admin(user) -> bool:
    from iic_booking.department_modules.access import is_main_admin

    return bool(user and user.is_authenticated and is_main_admin(user))


def can_review(user) -> bool:
    """SRIC reviewers: the Main Administrator, Accounts In Charge, and accounts SRIC named in the settings."""
    from iic_booking.users.models.user_type import UserType

    if not (user and user.is_authenticated and user.is_active):
        return False
    if _is_main_admin(user) or getattr(user, "user_type", None) == UserType.FINANCE:
        return True
    extra = {e.strip().lower() for e in EinvoiceSettings.get().approver_emails.splitlines() if e.strip()}
    return (user.email or "").strip().lower() in extra


def can_administer(user) -> bool:
    """Registers signatories' certificates and edits settings: the Main Administrator only."""
    return _is_main_admin(user)


def is_signatory(user) -> bool:
    return bool(user and user.is_authenticated and SignerCertificate.objects.filter(user=user, is_active=True).exists())


def require_enabled():
    if not conf.enabled():
        raise EinvoiceError("GST invoicing (sandbox) is switched off on this server.", status=403, code="disabled")


def require(check, user, message="You do not have access to GST invoices."):
    require_enabled()
    if not check(user):
        raise EinvoiceError(message, status=403, code="forbidden")


# --- history ---------------------------------------------------------------------------------------------------------------

def _name(user) -> str:
    if user is None:
        return "Portal (automatic)"
    return (getattr(user, "name", "") or user.email or str(user.pk))[:200]


def _event(inv: EInvoice, action: str, actor=None, *, before: str = "", note: str = "") -> None:
    EInvoiceEvent.objects.create(invoice=inv, action=action, from_status=before, to_status=inv.status, actor=actor,
                                 actor_name=_name(actor), note=note[:2000])


def _set_status(inv: EInvoice, status: str, actor, action: str, note: str = "", fields=()) -> None:
    before = inv.status
    inv.status = status
    inv.save(update_fields=["status", "updated_at", *fields])
    _event(inv, action, actor, before=before, note=note)


def _attempt(inv, operation, provider, result, *, customer=None, actor=None, payload_hash="") -> IrpAttempt:
    if result.ok:
        outcome = IrpOutcome.SUCCESS
    elif result.unsupported:
        outcome = IrpOutcome.UNSUPPORTED
    elif result.uncertain:
        outcome = IrpOutcome.UNCERTAIN
    elif result.http_status is None and not result.error_code:
        outcome = IrpOutcome.NOT_SENT
    else:
        outcome = IrpOutcome.REFUSED
    return IrpAttempt.objects.create(
        invoice=inv, customer=customer, operation=operation, provider=provider.name, simulated=provider.simulated,
        outcome=outcome, http_status=result.http_status, error_code=(result.error_code or "")[:20],
        error_message=(result.error_message or "")[:500], request_redacted=payload_mod.redact(result.request or {}),
        response_redacted=payload_mod.redact(result.response or {}), payload_hash=payload_hash,
        duration_ms=result.duration_ms, actor=actor)


# --- tax rule and buyer ----------------------------------------------------------------------------------------------------

def default_rule() -> TaxRule:
    rule = TaxRule.objects.filter(is_active=True, is_default=True).first() or TaxRule.objects.filter(is_active=True).first()
    if rule is None:
        rule, _ = TaxRule.objects.get_or_create(key=DEFAULT_RULE["key"], defaults={**DEFAULT_RULE, "is_default": True})
    return rule


def _customer_values(*, legal_name, gstin, address1, address2="", location, pincode, state="", state_code="",
                     email="", trade_name="", contact_name="") -> tuple[dict, list[str]]:
    """Cleaned buyer fields and the problems that need SRIC's review."""
    problems = []
    gstin = (gstin or "").strip().upper()
    status = GstinStatus.NONE
    if gstin:
        ok, why = validate_gstin(gstin)
        if ok:
            status = GstinStatus.FORMAT_VALID
        else:
            problems.append(f"The buyer's GSTIN {gstin} is not valid ({why})")
            status = GstinStatus.INVALID
    code = state_code_of(gstin) if status == GstinStatus.FORMAT_VALID else ""
    code = code or (state_code if state_code in STATE_CODES else "") or state_code_for_name(state)
    if not code:
        problems.append("The buyer's state is not known, so the place of supply cannot be set")
    pin = "".join(ch for ch in str(pincode or "") if ch.isdigit())[:6]
    if len(pin) != 6:
        problems.append("The buyer's PIN code is not a 6-digit code")
    values = {
        "legal_name": (legal_name or "").strip()[:200], "trade_name": (trade_name or "").strip()[:200],
        "gstin": gstin if status == GstinStatus.FORMAT_VALID else "",
        "gstin_status": status,
        "customer_type": CustomerType.REGISTERED if status == GstinStatus.FORMAT_VALID else CustomerType.UNREGISTERED,
        "address1": (address1 or "").strip()[:100] or "-", "address2": (address2 or "").strip()[:100],
        "location": (location or "").strip()[:100] or "-", "pincode": pin, "state_code": code,
        "email": (email or "").strip()[:254], "contact_name": (contact_name or "").strip()[:120],
    }
    if not values["legal_name"]:
        problems.append("The buyer's billing name is missing")
    return values, problems


# --- lines -------------------------------------------------------------------------------------------------------------

def _lines_from_snapshot(snapshot: dict, rule: TaxRule) -> tuple[list[tax_mod.LineInput], Decimal, Decimal | None]:
    """(lines, portal GST, portal total) from the request's charge breakdown: the GST rows the booking added are
    replaced by SRIC's rate; every other row is a taxable service line."""
    charges = snapshot.get("charges") or {}
    breakdown = [row for row in (charges.get("breakdown") or []) if isinstance(row, dict)]
    equipment = (snapshot.get("equipment") or {}).get("name") or "Facility"
    portal_total = Decimal(str(charges["total"])) if charges.get("total") not in (None, "") else None
    portal_gst = Decimal("0")
    items = []
    for row in breakdown:
        desc = str(row.get("description") or row.get("label") or "").strip()
        try:
            amount = Decimal(str(row.get("amount", 0) or 0))
        except Exception:  # noqa: BLE001
            continue
        if "gst" in desc.lower():
            portal_gst += amount
        elif amount:
            items.append((desc or "Facility charges", amount))
    if not items or any(amount < 0 for _, amount in items):
        base = (portal_total - portal_gst) if portal_total is not None else sum((a for _, a in items), Decimal("0"))
        detail = "; ".join(f"{d}: {a:.2f}" for d, a in items)[:300]
        items = [(f"{rule.description} - {equipment}", base)]
        details = [detail]
    else:
        details = [""] * len(items)
    lines = [tax_mod.LineInput(description=(f"{d} ({equipment})" if equipment not in d else d)[:300],
                               sac_code=rule.sac_code, gst_rate=rule.gst_rate, unit_price=amount, detail=detail,
                               tax_rule_id=rule.pk)
             for (d, amount), detail in zip(items, details)]
    return lines, portal_gst, portal_total


def _receipt_for_booking(booking) -> tuple[str, date | None]:
    """The printed number of the booking's payment receipt (already issued; never allocates one)."""
    if booking is None:
        return "", None
    try:
        from django.db.models import Q

        from iic_booking.payments.models import Payment

        payment = (Payment.objects.filter(Q(payment_order__booking=booking) | Q(payment_order__external_checkout__booking=booking))
                   .exclude(receipt_number__isnull=True).exclude(receipt_number="").order_by("created_at").first())
    except Exception:  # noqa: BLE001 - payments tables optional in some setups
        payment = None
    if payment is None:
        return "", None
    return payment.receipt_number, timezone.localtime(payment.created_at).date()


def _write_lines(inv: EInvoice, lines: list[tax_mod.LineInput]) -> None:
    result = tax_mod.calculate(lines, supplier_state=inv.supplier_state_code, place_of_supply=inv.place_of_supply)
    inv.lines.all().delete()
    for ln in result.lines:
        EInvoiceLine.objects.create(
            invoice=inv, sl_no=ln.sl_no, description=ln.description, detail=ln.detail, sac_code=ln.sac_code,
            quantity=ln.quantity, unit=ln.unit, unit_price=ln.unit_price, taxable_value=ln.taxable_value,
            gst_rate=ln.gst_rate, cgst=ln.cgst, sgst=ln.sgst, igst=ln.igst, total=ln.total, tax_rule_id=ln.tax_rule_id)
    inv.inter_state = result.inter_state
    inv.taxable_value, inv.cgst, inv.sgst, inv.igst = result.taxable_value, result.cgst, result.sgst, result.igst
    inv.round_off, inv.total = result.round_off, result.total
    inv.save()


def _review_reasons(inv: EInvoice, problems: list[str], s: EinvoiceSettings) -> list[str]:
    reasons = list(problems)
    if inv.total > s.review_above_amount:
        reasons.append(f"The invoice total {inv.total:.2f} is above SRIC's review limit of {s.review_above_amount:.2f}")
    if inv.portal_total is not None and abs(inv.total - inv.portal_total) > PORTAL_TOLERANCE:
        reasons.append(f"The invoice total {inv.total:.2f} differs from the booking charge {inv.portal_total:.2f}")
    if inv.sric_request_id and not inv.receipt_number:
        reasons.append("No payment receipt number was found for the booking")
    for line in inv.lines.all():
        if line.tax_rule is None or not line.tax_rule.is_active:
            reasons.append(f"Line {line.sl_no} has no active SRIC tax rule")
    if not reasons and not s.auto_issue:
        reasons.append("SRIC reviews every invoice before issue (automatic issue is switched off)")
    return reasons


# --- preparation -----------------------------------------------------------------------------------------------------------

def eligible_request(req) -> bool:
    """Sandbox: only requests for test accounts' bookings are taken up by the portal invoice."""
    from iic_booking.users.test_accounts import is_test_user

    return conf.enabled() and is_test_user(req.booking.user)


def prepare_from_request(req, actor=None) -> EInvoice:
    """The portal invoice for an existing SRIC invoice request (one per request; repeating returns the same one)."""
    require_enabled()
    existing = EInvoice.objects.filter(sric_request=req).first()
    if existing is not None:
        return existing
    if not eligible_request(req):
        raise EinvoiceError("In the sandbox, portal invoices are prepared only for test accounts' bookings.",
                            code="not_eligible")
    snap = req.snapshot or {}
    billing = snap.get("billing") or {}
    lines_in = billing.get("address_lines") or []
    values, problems = _customer_values(
        legal_name=billing.get("billing_name"), gstin=billing.get("gstin"),
        address1=lines_in[0] if lines_in else "", address2=", ".join(lines_in[1:]),
        location=billing.get("city"), pincode=billing.get("pincode"), state=billing.get("state"),
        email=(snap.get("user") or {}).get("email", ""), contact_name=(snap.get("user") or {}).get("name", ""))
    rule = default_rule()
    lines, _portal_gst, portal_total = _lines_from_snapshot(snap, rule)
    receipt_number, receipt_date = _receipt_for_booking(req.booking)
    s = EinvoiceSettings.get()
    try:
        with transaction.atomic():
            customer = GstCustomer.objects.create(user=req.booking.user, **values)
            inv = EInvoice.objects.create(
                provider=conf.provider(), supply_type=SupplyType.B2B if customer.registered else SupplyType.B2C,
                draft_reference=numbering.new_draft_reference(), sric_request=req, booking=req.booking,
                customer=customer, booking_reference=str(snap.get("booking_reference") or "")[:40],
                fbr_number=str(snap.get("fbr_number") or "")[:40],
                equipment_name=str((snap.get("equipment") or {}).get("name") or "")[:200],
                department_name=str(snap.get("facility") or "")[:200], receipt_number=receipt_number,
                receipt_date=receipt_date, supplier_state_code=s.supplier_state_code,
                place_of_supply=customer.state_code or s.supplier_state_code, portal_total=portal_total,
                created_by=actor)
            _write_lines(inv, lines)
            inv.review_reasons = _review_reasons(inv, problems, s)
            inv.save(update_fields=["review_reasons"])
            _event(inv, "prepared", actor, note=f"From invoice request {req.pk} ({inv.get_supply_type_display()}).")
    except IntegrityError:
        return EInvoice.objects.get(sric_request=req)
    return _after_prepare(inv, actor)


def create_demo(data: dict, actor) -> EInvoice:
    """A demonstration invoice for a demo buyer (no booking). ``data``: buyer fields, amount, description."""
    require(can_review, actor)
    values, problems = _customer_values(
        legal_name=data.get("legal_name"), gstin=data.get("gstin"), address1=data.get("address1"),
        address2=data.get("address2", ""), location=data.get("location"), pincode=data.get("pincode"),
        state=data.get("state", ""), state_code=data.get("state_code", ""), email=data.get("email", ""),
        trade_name=data.get("trade_name", ""))
    try:
        amount = tax_mod.money(data.get("amount"))
    except Exception:  # noqa: BLE001
        raise EinvoiceError("Enter the charge (taxable value) in rupees.", field="amount")
    if amount <= 0:
        raise EinvoiceError("The charge must be more than zero.", field="amount")
    rule = default_rule()
    if data.get("tax_rule"):
        rule = TaxRule.objects.filter(pk=data["tax_rule"], is_active=True).first() or rule
    s = EinvoiceSettings.get()
    with transaction.atomic():
        customer = GstCustomer.objects.create(is_demo=True, **values)
        inv = EInvoice.objects.create(
            provider=conf.provider(), supply_type=SupplyType.B2B if customer.registered else SupplyType.B2C,
            draft_reference=numbering.new_draft_reference(), is_demo=True, customer=customer,
            booking_reference=str(data.get("booking_reference") or "")[:40],
            equipment_name=str(data.get("equipment") or "")[:200], department_name=str(data.get("facility") or "")[:200],
            receipt_number=str(data.get("receipt_number") or "")[:48], po_number=str(data.get("po_number") or "")[:16],
            supplier_state_code=s.supplier_state_code, place_of_supply=customer.state_code or s.supplier_state_code,
            notes=str(data.get("notes") or "")[:1000], created_by=actor)
        desc = str(data.get("description") or f"{rule.description}").strip()[:300]
        _write_lines(inv, [tax_mod.LineInput(description=desc, sac_code=rule.sac_code, gst_rate=rule.gst_rate,
                                             unit_price=amount, detail=str(data.get("detail") or "")[:300],
                                             tax_rule_id=rule.pk)])
        inv.review_reasons = _review_reasons(inv, problems, s)
        inv.save(update_fields=["review_reasons"])
        _event(inv, "prepared", actor, note=f"Demonstration invoice ({inv.get_supply_type_display()}).")
    return _after_prepare(inv, actor)


def _after_prepare(inv: EInvoice, actor) -> EInvoice:
    if inv.review_reasons:
        _event(inv, "held_for_review", actor, before=inv.status, note="; ".join(inv.review_reasons))
        return inv
    try:
        return finalise(inv, actor, note="Every SRIC rule passed: issued without manual review.")
    except EinvoiceError as exc:
        # e.g. the e-invoice JSON would be refused: no number was used (the transaction rolled back); SRIC reviews it.
        inv = EInvoice.objects.get(pk=inv.pk)
        inv.review_reasons = [exc.message]
        inv.save(update_fields=["review_reasons"])
        _event(inv, "held_for_review", actor, before=inv.status, note=exc.message)
        return inv


# --- review -------------------------------------------------------------------------------------------------------------

def _lock(pk) -> EInvoice:
    try:
        return EInvoice.objects.select_for_update().get(pk=pk)
    except EInvoice.DoesNotExist:
        raise EinvoiceError("Invoice not found.", status=404, code="not_found")


def approve(inv_id, actor, remarks: str = "") -> EInvoice:
    require(can_review, actor)
    with transaction.atomic():
        inv = _lock(inv_id)
        if inv.status != InvoiceStatus.PENDING_REVIEW:
            raise EinvoiceError("Only an invoice waiting for review can be approved.", code="wrong_status")
        if inv.customer.gstin_status == GstinStatus.INVALID or not inv.customer.legal_name or len(inv.customer.pincode) != 6:
            raise EinvoiceError("Correct the buyer's details first.", code="buyer_incomplete")
        inv.reviewed_by, inv.reviewed_at, inv.review_remarks = actor, timezone.now(), (remarks or "").strip()[:2000]
        inv.save(update_fields=["reviewed_by", "reviewed_at", "review_remarks"])
        _event(inv, "approved", actor, before=inv.status, note=inv.review_remarks)
    return finalise(inv, actor)


def reject(inv_id, actor, remarks: str) -> EInvoice:
    """Not issued (no number is used). A booking's request then goes to SRIC by the weekly e-mail, as before."""
    require(can_review, actor)
    if len((remarks or "").strip()) < 5:
        raise EinvoiceError("Give the reason (at least 5 characters).", field="remarks")
    with transaction.atomic():
        inv = _lock(inv_id)
        if inv.status != InvoiceStatus.PENDING_REVIEW:
            raise EinvoiceError("Only an invoice waiting for review can be rejected.", code="wrong_status")
        inv.reviewed_by, inv.reviewed_at, inv.review_remarks = actor, timezone.now(), remarks.strip()[:2000]
        _set_status(inv, InvoiceStatus.REJECTED, actor, "rejected", inv.review_remarks,
                    fields=("reviewed_by", "reviewed_at", "review_remarks"))
    return inv


def update_buyer(inv_id, actor, data: dict) -> EInvoice:
    """Correct the buyer before issue (waiting for review) or after the e-invoice system refused it. The number, if
    one was given, is kept: a refused invoice has no IRN, so it is sent again under the same number."""
    require(can_review, actor)
    with transaction.atomic():
        inv = _lock(inv_id)
        if inv.status not in (InvoiceStatus.PENDING_REVIEW, InvoiceStatus.SUBMISSION_FAILED):
            raise EinvoiceError("The buyer can be corrected only before issue or after a refusal.", code="wrong_status")
        c = inv.customer
        merged = {k: data.get(k, getattr(c, k)) for k in ("legal_name", "trade_name", "gstin", "address1", "address2",
                                                           "location", "pincode", "email")}
        values, problems = _customer_values(**merged, state=data.get("state", ""),
                                            state_code=data.get("state_code", c.state_code))
        if problems:
            raise EinvoiceError("; ".join(problems) + ".", code="buyer_invalid")
        for k, v in values.items():
            setattr(c, k, v)
        c.save()
        inv.place_of_supply = c.state_code
        old_type = inv.supply_type
        inv.supply_type = SupplyType.B2B if c.registered else SupplyType.B2C
        lines = [tax_mod.LineInput(description=ln.description, sac_code=ln.sac_code, gst_rate=ln.gst_rate,
                                   unit_price=ln.unit_price, quantity=ln.quantity, unit=ln.unit, detail=ln.detail,
                                   tax_rule_id=ln.tax_rule_id) for ln in inv.lines.all()]
        _write_lines(inv, lines)
        if inv.status == InvoiceStatus.PENDING_REVIEW:
            inv.review_reasons = _review_reasons(inv, [], EinvoiceSettings.get())
            inv.save(update_fields=["review_reasons"])
        _event(inv, "buyer_corrected", actor, before=inv.status,
               note=f"Buyer details corrected ({old_type} -> {inv.supply_type}).")
    if inv.status == InvoiceStatus.SUBMISSION_FAILED:
        if inv.supply_type == SupplyType.B2C:
            with transaction.atomic():
                inv = _lock(inv.pk)
                _prepare_for_signature(inv, actor)
            return inv
        with transaction.atomic():
            inv = _lock(inv.pk)
            _prepare_payload(inv)
            _set_status(inv, InvoiceStatus.SUBMITTING, actor, "ready_to_resend", fields=("payload", "payload_hash"))
        return submit_irn(inv.pk, actor)
    return inv


# --- issue ---------------------------------------------------------------------------------------------------------------

def finalise(inv: EInvoice, actor, note: str = "") -> EInvoice:
    """Number the invoice (the only place a number is allocated) and send it down its path."""
    with transaction.atomic():
        inv = _lock(inv.pk)
        if inv.invoice_number:
            return inv
        today = timezone.localdate()
        from .models import DocType

        prefix = "ECN" if inv.doc_type == DocType.CREDIT_NOTE else numbering.DEMO_PREFIX
        inv.series, inv.invoice_number = numbering.allocate(today, prefix=prefix)
        inv.invoice_date = today
        inv.save(update_fields=["series", "invoice_number", "invoice_date"])
        _event(inv, "numbered", actor, before=inv.status, note=(note + f" Invoice number {inv.invoice_number}.").strip())
        if inv.supply_type == SupplyType.B2C:
            _prepare_for_signature(inv, actor)
            return inv
        _prepare_payload(inv)
        _set_status(inv, InvoiceStatus.SUBMITTING, actor, "payload_ready", fields=("payload", "payload_hash"))
    return submit_irn(inv.pk, actor)


def _prepare_payload(inv: EInvoice) -> None:
    body = payload_mod.build(inv)
    errors = payload_mod.validate(body)
    if errors:
        raise EinvoiceError("The e-invoice would be refused: " + "; ".join(errors), code="payload_invalid")
    inv.payload, inv.payload_hash = body, payload_mod.fingerprint(body)


def _store_pdf(inv: EInvoice, data: bytes) -> None:
    inv.pdf_sha256 = hashlib.sha256(data).hexdigest()
    inv.pdf.save(f"{inv.invoice_number.replace('/', '-')}-{inv.pdf_sha256[:12]}.pdf", ContentFile(data), save=False)


def _prepare_for_signature(inv: EInvoice, actor) -> None:
    _store_pdf(inv, pdf_mod.render(inv))
    _set_status(inv, InvoiceStatus.AWAITING_SIGNATURE, actor, "sent_for_signature",
                "Tax invoice prepared; waiting for the authorised signatory's DSC.", fields=("pdf", "pdf_sha256"))


def _adopt_irn(inv: EInvoice, data: dict, actor, note: str) -> None:
    from django.utils.dateparse import parse_datetime

    inv.irn, inv.ack_number = str(data.get("Irn", ""))[:80], str(data.get("AckNo", ""))[:40]
    ack = parse_datetime(str(data.get("AckDt", "")).replace(" ", "T"))
    if ack is not None and timezone.is_naive(ack):
        ack = timezone.make_aware(ack)
    inv.ack_date = ack or timezone.now()
    inv.irp_status = str(data.get("Status", "ACT"))[:10]
    inv.signed_invoice, inv.signed_qr = data.get("SignedInvoice", ""), data.get("SignedQRCode", "")
    inv.simulated = bool(data.get("SIMULATED"))
    _store_pdf(inv, pdf_mod.render(inv))
    _set_status(inv, InvoiceStatus.IRN_GENERATED, actor, "irn_generated", note,
                fields=("irn", "ack_number", "ack_date", "irp_status", "signed_invoice", "signed_qr", "simulated",
                        "pdf", "pdf_sha256"))


def submit_irn(inv_id, actor=None) -> EInvoice:
    """Send a numbered B2B invoice to the sandbox. After an unanswered attempt it first asks whether the invoice was
    registered (Get IRN by document), so an invoice is never registered twice or renumbered."""
    from .providers import get_provider

    require_enabled()
    lock_key = f"gst_einvoice:submit:{inv_id}"
    if not cache.add(lock_key, "1", 120):
        raise EinvoiceError("This invoice is being sent already.", status=409, code="busy")
    try:
        inv = EInvoice.objects.select_related("customer").get(pk=inv_id)
        if inv.status not in (InvoiceStatus.SUBMITTING, InvoiceStatus.SUBMISSION_UNCERTAIN, InvoiceStatus.SUBMISSION_FAILED):
            raise EinvoiceError("This invoice is not waiting to be sent.", code="wrong_status")
        if inv.supply_type != SupplyType.B2B or not inv.invoice_number:
            raise EinvoiceError("Only numbered invoices to GST-registered buyers get an IRN.", code="not_b2b")
        provider = get_provider()
        inv.provider = provider.name
        inv.save(update_fields=["provider"])
        doc = inv.payload.get("DocDtls") or {}
        was_uncertain = inv.status == InvoiceStatus.SUBMISSION_UNCERTAIN or inv.attempts.filter(
            operation=IrpOperation.GENERATE, outcome=IrpOutcome.UNCERTAIN).exists()
        if was_uncertain:
            found = provider.get_by_document(doc.get("Typ", "INV"), doc.get("No", ""), doc.get("Dt", ""))
            _attempt(inv, IrpOperation.GET_BY_DOC, provider, found, actor=actor, payload_hash=inv.payload_hash)
            if found.ok:
                with transaction.atomic():
                    inv = _lock(inv.pk)
                    _adopt_irn(inv, found.data, actor, "Found already registered after an unanswered attempt.")
                deliver(inv.pk)
                return EInvoice.objects.get(pk=inv.pk)
            if found.uncertain:
                with transaction.atomic():
                    inv = _lock(inv.pk)
                    _set_status(inv, InvoiceStatus.SUBMISSION_UNCERTAIN, actor, "lookup_unanswered", found.error_message)
                return inv
        inv.irp_submitted_at = timezone.now()
        inv.save(update_fields=["irp_submitted_at"])
        result = provider.generate_irn(inv.payload, notes=inv.notes)
        _attempt(inv, IrpOperation.GENERATE, provider, result, actor=actor, payload_hash=inv.payload_hash)
        if not result.ok and result.data.get("duplicate"):
            dup = result.data["duplicate"]
            again = provider.get_irn(dup.get("Irn", "")) if dup.get("Irn") else provider.get_by_document(
                doc.get("Typ", "INV"), doc.get("No", ""), doc.get("Dt", ""))
            _attempt(inv, IrpOperation.GET_IRN, provider, again, actor=actor, payload_hash=inv.payload_hash)
            if again.ok:
                result = again
        with transaction.atomic():
            inv = _lock(inv.pk)
            if result.ok:
                _adopt_irn(inv, result.data, actor, f"IRN from {provider.label}.")
            elif result.uncertain:
                _set_status(inv, InvoiceStatus.SUBMISSION_UNCERTAIN, actor, "submit_unanswered",
                            result.error_message + " The portal will check before trying again.")
                return inv
            else:
                _set_status(inv, InvoiceStatus.SUBMISSION_FAILED, actor, "submit_refused",
                            f"{result.error_code} {result.error_message}".strip())
                return inv
        deliver(inv.pk)
        return EInvoice.objects.get(pk=inv.pk)
    finally:
        cache.delete(lock_key)


def check_gstin(inv_id, actor) -> EInvoice:
    """Ask the sandbox whether the buyer's GSTIN is active (Get GSTIN details)."""
    from .providers import get_provider

    require(can_review, actor)
    inv = EInvoice.objects.select_related("customer").get(pk=inv_id)
    c = inv.customer
    if not c.gstin:
        raise EinvoiceError("This buyer has no GSTIN.", code="no_gstin")
    provider = get_provider()
    result = provider.gstin_details(c.gstin)
    _attempt(inv, IrpOperation.GSTIN, provider, result, customer=c, actor=actor)
    if result.ok:
        active = str(result.data.get("Status", "ACT")).upper() == "ACT"
        c.gstin_status = (GstinStatus.SIMULATED_VERIFIED if provider.simulated else GstinStatus.SANDBOX_VERIFIED) \
            if active else GstinStatus.SANDBOX_INACTIVE
        c.gstin_checked_at = timezone.now()
        c.save(update_fields=["gstin_status", "gstin_checked_at"])
    _event(inv, "gstin_checked", actor, before=inv.status,
           note=c.get_gstin_status_display() if result.ok else f"Check failed: {result.error_message}")
    return inv


# --- DSC signing -------------------------------------------------------------------------------------------------------------

def new_device(user, name: str) -> tuple[SignerDevice, str]:
    """A signing-utility token for a registered signatory; the token is returned once and only its hash is kept."""
    require(is_signatory, user, "Only a signatory with a registered DSC certificate can link a signing utility.")
    token = "sricsign_" + secrets.token_urlsafe(32)
    device = SignerDevice.objects.create(user=user, name=(name or "Signing computer").strip()[:120],
                                         token_sha256=hashlib.sha256(token.encode()).hexdigest(), token_hint=token[:12])
    return device, token


def device_for_token(token: str) -> SignerDevice | None:
    if not token or not token.startswith("sricsign_"):
        return None
    device = SignerDevice.objects.select_related("user").filter(
        token_sha256=hashlib.sha256(token.encode()).hexdigest(), revoked_at__isnull=True).first()
    if device is None or not device.user.is_active or not is_signatory(device.user):
        return None
    SignerDevice.objects.filter(pk=device.pk).update(last_seen_at=timezone.now())
    return device


def signing_queue() -> list[EInvoice]:
    return list(EInvoice.objects.filter(status=InvoiceStatus.AWAITING_SIGNATURE).select_related("customer")
                .order_by("invoice_number"))


def accept_signed(inv_id, device: SignerDevice, data: bytes) -> EInvoice:
    """Store the signed PDF only if ``dsc.verify_signed_pdf`` accepts it, then deliver."""
    require_enabled()
    if len(data) > 15 * 1024 * 1024:
        raise EinvoiceError("The signed PDF is too large.", field="file")
    try:
        return _accept_signed(inv_id, device, data)
    except dsc.SignatureRejected as exc:
        # Logged outside the refused transaction, so every refused upload stays on the invoice's history.
        inv = EInvoice.objects.filter(pk=inv_id).first()
        if inv is not None:
            _event(inv, "signature_refused", device.user, before=inv.status, note=f"{exc} (device {device.name})")
        raise EinvoiceError(f"Signature not accepted: {exc}", code="signature_rejected")


def _accept_signed(inv_id, device: SignerDevice, data: bytes) -> EInvoice:
    with transaction.atomic():
        inv = _lock(inv_id)
        if inv.status != InvoiceStatus.AWAITING_SIGNATURE:
            raise EinvoiceError("This invoice is not waiting for a signature.", code="wrong_status")
        with inv.pdf.open("rb") as fh:
            prepared = fh.read()
        if hashlib.sha256(prepared).hexdigest() != inv.pdf_sha256:
            raise EinvoiceError("The prepared invoice file has changed on the server; it cannot be signed.",
                                status=409, code="prepared_changed")
        certs = list(SignerCertificate.objects.filter(user=device.user, is_active=True))
        verified = dsc.verify_signed_pdf(prepared, data, certs)
        inv.signed_pdf_sha256 = verified.signed_sha256
        inv.signed_pdf.save(f"{inv.invoice_number.replace('/', '-')}-signed-{verified.signed_sha256[:12]}.pdf",
                            ContentFile(data), save=False)
        inv.signed_at, inv.signed_by = timezone.now(), device.user
        inv.signer_certificate_id = verified.certificate_id
        _set_status(inv, InvoiceStatus.SIGNED, device.user, "signed",
                    f"Signed with DSC {verified.subject} (SHA-256 {verified.fingerprint[:16]}...) on {device.name}.",
                    fields=("signed_pdf", "signed_pdf_sha256", "signed_at", "signed_by", "signer_certificate"))
    deliver(inv.pk)
    return EInvoice.objects.get(pk=inv.pk)


def register_certificate(actor, user, raw: bytes, *, label: str = "", is_test: bool = False) -> SignerCertificate:
    require(can_administer, actor, "Only the Main Administrator registers signatories' certificates.")
    try:
        facts = dsc.certificate_details(raw)
    except ValueError:
        raise EinvoiceError("The file is not an X.509 certificate (.cer / .crt, PEM or DER).", field="file")
    if facts["not_after"] < timezone.now():
        raise EinvoiceError("This certificate has expired.", field="file")
    if SignerCertificate.objects.filter(sha256_fingerprint=facts["fingerprint"]).exists():
        raise EinvoiceError("This certificate is registered already.", field="file")
    return SignerCertificate.objects.create(
        user=user, label=(label or "").strip()[:120], certificate_pem=facts["pem"],
        sha256_fingerprint=facts["fingerprint"], subject=facts["subject"], issuer=facts["issuer"],
        serial_number=facts["serial"], not_before=facts["not_before"], not_after=facts["not_after"],
        is_test=is_test, registered_by=actor)


def deactivate_certificate(actor, cert_id, reason: str) -> SignerCertificate:
    require(can_administer, actor, "Only the Main Administrator manages signatories' certificates.")
    cert = SignerCertificate.objects.filter(pk=cert_id).first()
    if cert is None:
        raise EinvoiceError("Certificate not found.", status=404, code="not_found")
    if len((reason or "").strip()) < 5:
        raise EinvoiceError("Give the reason (at least 5 characters).", field="reason")
    cert.is_active, cert.deactivated_at, cert.deactivated_by = False, timezone.now(), actor
    cert.deactivation_reason = reason.strip()[:300]
    cert.save(update_fields=["is_active", "deactivated_at", "deactivated_by", "deactivation_reason"])
    return cert


# --- delivery ----------------------------------------------------------------------------------------------------------------

def final_pdf(inv: EInvoice) -> tuple[bytes, str] | None:
    field = inv.signed_pdf if inv.status == InvoiceStatus.SIGNED else inv.pdf
    if not field:
        return None
    with field.open("rb") as fh:
        data = fh.read()
    suffix = "-signed" if inv.status == InvoiceStatus.SIGNED else ""
    return data, f"tax-invoice-{inv.invoice_number.replace('/', '-')}{suffix}.pdf"


def deliver(inv_id) -> EInvoice:
    """Put the issued invoice on the booking's existing invoice request (OfficialInvoiceDocument) and tell the buyer."""
    from iic_booking.external_services.invoices import attach_portal_invoice

    from .models import DocType

    inv = EInvoice.objects.select_related("sric_request", "booking__user", "original_invoice").get(pk=inv_id)
    if not inv.issued or inv.delivered_at:
        return inv
    if inv.doc_type == DocType.CREDIT_NOTE:
        return _deliver_credit_note(inv)
    pdf = final_pdf(inv)
    doc = None
    if inv.sric_request_id and pdf:
        doc = attach_portal_invoice(inv.sric_request, pdf[0], file_name=pdf[1], invoice_number=inv.invoice_number,
                                    note=("GST e-invoice (IRN)" if inv.supply_type == SupplyType.B2B
                                          else "Tax invoice signed by SRIC's authorised signatory (DSC)"))
    inv.official_document, inv.delivered_at = doc, timezone.now()
    inv.save(update_fields=["official_document", "delivered_at"])
    _event(inv, "delivered", None, before=inv.status,
           note="Available to the buyer in My Bookings." if doc else "Demonstration invoice: nothing sent to a buyer.")
    return inv


def _deliver_credit_note(note: EInvoice) -> EInvoice:
    """The buyer is told; the credit note downloads from their booking (it does not replace the invoice)."""
    from iic_booking.external_services import notifications

    if note.booking_id and note.booking.user_id:
        notifications.notify(
            f"gst-credit-note:{note.pk}", [note.booking.user],
            title=f"Credit note {note.invoice_number} for {note.booking_reference or note.booking_id}",
            message=(f"A credit note of Rs. {note.total} against tax invoice {note.original_invoice.invoice_number} "
                     "has been issued for your refund. You can download it with your booking."),
            link=f"/my-bookings?booking={note.booking_id}")
    note.delivered_at = timezone.now()
    note.save(update_fields=["delivered_at"])
    _event(note, "delivered", None, before=note.status, note="Buyer told; available with the booking.")
    return note


def process_pending(limit: int = 100) -> dict:
    """Periodic: take up eligible queued requests, and re-check unanswered submissions (lookup first)."""
    from iic_booking.external_services.models import InvoiceRequestStatus, SricInvoiceRequest

    out = {"prepared": 0, "rechecked": 0, "errors": 0}
    if not conf.enabled():
        return {"skipped": "disabled"}
    reqs = (SricInvoiceRequest.objects.filter(gst_einvoice__isnull=True, status__in=[
        InvoiceRequestStatus.QUEUED, InvoiceRequestStatus.HELD_CONSOLIDATION]).select_related("booking__user")
        .order_by("created_at")[:limit])
    for req in reqs:
        if not eligible_request(req):
            continue
        try:
            prepare_from_request(req)
            out["prepared"] += 1
        except Exception:  # noqa: BLE001 - one bad request must not stop the rest
            logger.exception("GST e-invoice: preparing from request %s failed", req.pk)
            out["errors"] += 1
    for inv in EInvoice.objects.filter(status__in=[InvoiceStatus.SUBMISSION_UNCERTAIN, InvoiceStatus.SUBMITTING])[:limit]:
        try:
            submit_irn(inv.pk)
            out["rechecked"] += 1
        except EinvoiceError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("GST e-invoice: re-check of invoice %s failed", inv.pk)
            out["errors"] += 1
    return out


# --- register for GST returns ------------------------------------------------------------------------------------------------

def register(year: int, month: int) -> dict:
    """The monthly register SRIC asked for (proposal section 5): every issued invoice, and the series summary for the
    document summary of GSTR-1 (Table 13)."""
    qs = (EInvoice.objects.filter(invoice_date__year=year, invoice_date__month=month)
          .exclude(invoice_number="").select_related("customer", "series", "original_invoice")
          .order_by("doc_type", "invoice_number"))
    rows = []
    for inv in qs:
        rows.append({
            "invoice_number": inv.invoice_number, "invoice_date": inv.invoice_date.isoformat(),
            "doc_type": inv.doc_type,
            "original_invoice": inv.original_invoice.invoice_number if inv.original_invoice_id else "",
            "status": inv.status, "status_label": inv.get_status_display(), "supply_type": inv.supply_type,
            "buyer": inv.customer.legal_name, "buyer_gstin": inv.customer.gstin, "place_of_supply": inv.place_of_supply,
            "irn": inv.irn, "ack_number": inv.ack_number, "receipt_number": inv.receipt_number,
            "booking_reference": inv.booking_reference, "taxable_value": f"{inv.taxable_value:.2f}",
            "cgst": f"{inv.cgst:.2f}", "sgst": f"{inv.sgst:.2f}", "igst": f"{inv.igst:.2f}", "total": f"{inv.total:.2f}",
            "signed_by": _name(inv.signed_by) if inv.signed_by_id else "", "simulated": inv.simulated,
        })
    series = {}
    for r in rows:
        prefix = r["invoice_number"].rsplit("/", 1)[0]
        entry = series.setdefault(prefix, {"series": prefix, "from": r["invoice_number"], "to": r["invoice_number"],
                                           "count": 0})
        entry["to"] = r["invoice_number"]
        entry["count"] += 1
    issued = [r for r in rows if r["status"] in (InvoiceStatus.IRN_GENERATED, InvoiceStatus.SIGNED)
              and r["doc_type"] == "INV"]
    return {
        "year": year, "month": month, "mode": "sandbox", "rows": rows, "series": list(series.values()),
        "totals": {k: f"{sum(Decimal(r[k]) for r in issued):.2f}" for k in ("taxable_value", "cgst", "sgst", "igst", "total")},
        "counts": {"b2b": sum(1 for r in issued if r["supply_type"] == "B2B"),
                   "b2c": sum(1 for r in issued if r["supply_type"] == "B2C"),
                   "not_yet_issued": len(rows) - len(issued)},
    }


# --- serialisation -----------------------------------------------------------------------------------------------------------

def _iso(dt):
    return timezone.localtime(dt).isoformat() if dt else None


def invoice_dict(inv: EInvoice, *, detail: bool = False) -> dict:
    c = inv.customer
    out = {
        "id": inv.pk, "number": inv.display_number, "invoice_number": inv.invoice_number,
        "doc_type": inv.doc_type, "doc_type_label": inv.get_doc_type_display(),
        "original_invoice": ({"id": inv.original_invoice_id, "number": inv.original_invoice.invoice_number,
                              "date": inv.original_invoice.invoice_date.isoformat()}
                             if inv.original_invoice_id else None),
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
        "status": inv.status, "status_label": inv.get_status_display(), "supply_type": inv.supply_type,
        "supply_type_label": inv.get_supply_type_display(), "mode": inv.mode, "provider": inv.provider,
        "simulated": inv.simulated, "is_demo": inv.is_demo, "review_reasons": inv.review_reasons,
        "buyer": {"name": c.legal_name, "gstin": c.gstin, "gstin_status": c.gstin_status,
                  "gstin_status_label": c.get_gstin_status_display(), "state_code": c.state_code,
                  "state": STATE_CODES.get(c.state_code, ""), "location": c.location},
        "booking_id": inv.booking_id, "booking_reference": inv.booking_reference, "fbr_number": inv.fbr_number,
        "sric_request_id": inv.sric_request_id, "receipt_number": inv.receipt_number,
        "equipment": inv.equipment_name, "facility": inv.department_name,
        "place_of_supply": inv.place_of_supply, "inter_state": inv.inter_state,
        "taxable_value": f"{inv.taxable_value:.2f}", "cgst": f"{inv.cgst:.2f}", "sgst": f"{inv.sgst:.2f}",
        "igst": f"{inv.igst:.2f}", "round_off": f"{inv.round_off:.2f}", "total": f"{inv.total:.2f}",
        "portal_total": f"{inv.portal_total:.2f}" if inv.portal_total is not None else None,
        "irn": inv.irn, "ack_number": inv.ack_number, "ack_date": _iso(inv.ack_date),
        "signed_at": _iso(inv.signed_at), "signed_by": _name(inv.signed_by) if inv.signed_by_id else "",
        "delivered_at": _iso(inv.delivered_at), "created_at": _iso(inv.created_at),
        "has_pdf": bool(inv.pdf), "has_signed_pdf": bool(inv.signed_pdf),
        "pdf_url": f"/api/v1/gst-einvoice/invoices/{inv.pk}/pdf/" if (inv.pdf or inv.signed_pdf) else None,
    }
    if detail:
        out["buyer"].update({"legal_name": c.legal_name, "trade_name": c.trade_name, "address1": c.address1,
                             "address2": c.address2, "pincode": c.pincode, "email": c.email})
        out["lines"] = [{"sl_no": ln.sl_no, "description": ln.description, "detail": ln.detail, "sac_code": ln.sac_code,
                         "quantity": f"{ln.quantity.normalize():f}", "taxable_value": f"{ln.taxable_value:.2f}",
                         "gst_rate": f"{ln.gst_rate.normalize():f}", "cgst": f"{ln.cgst:.2f}", "sgst": f"{ln.sgst:.2f}",
                         "igst": f"{ln.igst:.2f}", "total": f"{ln.total:.2f}"} for ln in inv.lines.all()]
        out["events"] = [{"action": e.action, "from": e.from_status, "to": e.to_status, "by": e.actor_name,
                          "note": e.note, "at": _iso(e.created_at)} for e in inv.events.all()]
        out["attempts"] = [{"operation": a.operation, "outcome": a.outcome, "provider": a.provider,
                            "simulated": a.simulated, "error_code": a.error_code, "error_message": a.error_message,
                            "http_status": a.http_status, "duration_ms": a.duration_ms, "at": _iso(a.created_at)}
                           for a in inv.attempts.all()]
        out["payload"] = inv.payload
        out["signer_certificate"] = ({"subject": inv.signer_certificate.subject,
                                      "fingerprint": inv.signer_certificate.sha256_fingerprint,
                                      "is_test": inv.signer_certificate.is_test}
                                     if inv.signer_certificate_id else None)
        out["pdf_sha256"], out["signed_pdf_sha256"] = inv.pdf_sha256, inv.signed_pdf_sha256
        out["review_remarks"] = inv.review_remarks
        out["credit_reason"] = inv.credit_reason
        from .credit_notes import permission_for, remaining

        perm = permission_for(inv) if inv.doc_type == "INV" else None
        out["credit_note_permission"] = ({"enabled_by": _name(perm.enabled_by), "reason": perm.reason,
                                          "enabled_at": _iso(perm.enabled_at)} if perm else None)
        out["credit_notes"] = [{"id": n.pk, "number": n.display_number, "status": n.status,
                                "status_label": n.get_status_display(), "total": f"{n.total:.2f}"}
                               for n in inv.credit_notes.all()]
        if inv.doc_type == "INV" and inv.issued:
            left_taxable, left_total = remaining(inv)
            out["credit_remaining"] = {"taxable_value": f"{left_taxable:.2f}", "total": f"{left_total:.2f}"}
    return out


def buyer_view(user, booking_id) -> dict | None:
    """What the booking's owner sees: the portal invoice of their booking, when one exists."""
    inv = (EInvoice.objects.filter(booking_id=booking_id, booking__user=user, doc_type="INV")
           .select_related("customer").order_by("-created_at").first())
    if inv is None:
        return None
    notes = [{"number": n.invoice_number, "date": n.invoice_date.isoformat() if n.invoice_date else None,
              "total": f"{n.total:.2f}", "status_label": n.get_status_display(), "issued": n.issued,
              "download_url": f"/api/v1/gst-einvoice/invoices/{n.pk}/pdf/" if n.issued else None}
             for n in inv.credit_notes.all()]
    return {
        "number": inv.display_number, "status": inv.status, "status_label": inv.get_status_display(),
        "supply_type": inv.supply_type, "issued": inv.issued, "mode": inv.mode, "simulated": inv.simulated,
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None, "irn": inv.irn,
        "ack_number": inv.ack_number, "total": f"{inv.total:.2f}", "receipt_number": inv.receipt_number,
        "signed_at": _iso(inv.signed_at), "credit_notes": notes,
        "download_url": (f"/api/v1/external-booking/invoices/documents/{inv.official_document_id}/download/"
                         if inv.official_document_id else None),
        "next_step": ("Download the invoice." if inv.issued else
                      "SRIC's authorised signatory signs it with their DSC; it appears here once signed."
                      if inv.status == InvoiceStatus.AWAITING_SIGNATURE else
                      "SRIC is reviewing the invoice." if inv.status == InvoiceStatus.PENDING_REVIEW else
                      "The accounts office is completing the invoice."),
    }
