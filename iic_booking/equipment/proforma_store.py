"""Saving proforma invoices, and paying, cancelling and revising them.

* Every proforma the charges page or the one-equipment estimate issues is saved: number, buyer and line snapshots,
  totals and the PDF exactly as handed out. An estimate with equipment of several departments becomes one proforma
  per department, because each department's money goes to its own sub-wallet and grant.
* For a signed-in user the lines are priced again on the server (the browser's amounts are not trusted); the record
  says so in ``amount_source``.
* A proforma is never changed after issue. Edit = a revision with a new number (``…-R1``) that replaces it; cancel =
  CANCELLED. Both are refused once money is pending or credited against it. Old PDFs are stamped when downloaded.
* Paying = an ordinary wallet recharge for the proforma's department, online (Razorpay order) or offline (receipt
  upload, approval, reconciliation), linked through ProformaPayment. The proforma's state is worked out from those
  links, so it always agrees with the wallet, approval and reconciliation records.
"""

from __future__ import annotations

import io
import logging
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from iic_booking.users.models.user_type import UserType

from .proforma_models import (
    ProformaAmountSource,
    ProformaEvent,
    ProformaPayment,
    ProformaPaymentKind,
    ProformaStatus,
    SavedProformaInvoice,
)
from .proforma_pdf import (
    STATE_CODES,
    Line,
    Party,
    Proforma,
    buyer_from,
    dec,
    department_accounts_email,
    new_number,
    render_proforma,
    state_code_for,
)

logger = logging.getLogger(__name__)
VALID_DAYS = 30
ZERO = Decimal("0.00")


class ProformaError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid", field: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.field = field

    def payload(self) -> dict:
        out = {"error": self.message, "code": self.code}
        if self.field:
            out["field"] = self.field
        return out


# --- pricing the lines ---------------------------------------------------------------------------------------------------

def _title(name: str, code: str) -> str:
    name = (name or "").strip() or "Equipment"
    code = (code or "").strip()
    return f"{name} ({code})" if code and code.lower() not in name.lower() else name


def _clean_inputs(raw) -> dict:
    out = {}
    for k, v in (raw or {}).items() if isinstance(raw, dict) else []:
        if v is None:
            continue
        if isinstance(v, (int, float)):
            out[k] = v
        else:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                out[k] = v
    return out


def priced_lines(user, items: list) -> list[tuple]:
    """[(department, Line, request_item)] priced on the server for ``user``, as the charges page prices them."""
    from .api_views import _calculate_one_proforma_line, _sample_set_groups_limit_error, user_can_see_equipment
    from .document_exports import _proforma_line_details
    from .models import Equipment

    if not isinstance(items, list) or not items:
        raise ProformaError("Add at least one equipment to the proforma.", field="items")
    out = []
    user_type = getattr(user, "user_type", None) or UserType.STUDENT
    for idx, item in enumerate(items):
        eq_id = item.get("equipment_id") if isinstance(item, dict) else None
        try:
            equipment = Equipment.objects.select_related("equipment_group", "internal_department").get(pk=int(eq_id))
        except (TypeError, ValueError, Equipment.DoesNotExist):
            raise ProformaError(f"Line {idx + 1}: equipment not found.", status=404, field="items") from None
        if not user_can_see_equipment(user, equipment):
            raise ProformaError(f"You do not have access to equipment {equipment.code or equipment.pk}.", status=403)
        inputs = _clean_inputs(item.get("input_values"))
        problem = _sample_set_groups_limit_error(equipment, inputs, booking_user=user, user_type=user_type,
                                                 check_max=False, check_formula_max=True)
        if problem:
            raise ProformaError(f"{equipment.code or equipment.pk}: {problem}", field="items")
        data, err = _calculate_one_proforma_line(user, equipment, inputs)
        if err:
            raise ProformaError(err, field="items")
        line = Line(title=_title(equipment.name, equipment.code), details=_proforma_line_details(data, "₹"),
                    taxable=dec(data.get("base_charge")), gst=dec(data.get("gst_amount")))
        request_item = {"equipment_id": equipment.pk, "equipment_code": equipment.code or "",
                        "equipment_name": equipment.name or "", "profile_type": equipment.profile_type or "",
                        "input_values": inputs, "input_labels_and_values": item.get("input_labels_and_values") or {}}
        out.append((equipment.internal_department, line, request_item))
    return out


def client_lines(line_items: list) -> list[tuple]:
    """[(department, Line, request_item)] from amounts the browser sent (estimates made without signing in)."""
    from .document_exports import _proforma_line_details
    from .models import Equipment

    ids = set()
    for row in line_items or []:
        try:
            ids.add(int(row.get("equipment_id")))
        except (AttributeError, TypeError, ValueError):
            pass
    by_id = {e.pk: e for e in Equipment.objects.select_related("internal_department").filter(pk__in=ids)}
    out = []
    for row in line_items or []:
        if not isinstance(row, dict):
            continue
        try:
            eq = by_id.get(int(row.get("equipment_id")))
        except (TypeError, ValueError):
            eq = None
        line = Line(title=_title(str(row.get("equipment_name") or ""), str(row.get("equipment_code") or "")),
                    details=_proforma_line_details(row, "₹"), taxable=dec(row.get("base_charge")),
                    gst=dec(row.get("gst_amount")))
        request_item = {"equipment_id": eq.pk if eq else None, "equipment_code": str(row.get("equipment_code") or ""),
                        "equipment_name": str(row.get("equipment_name") or ""),
                        "input_values": _clean_inputs(row.get("input_values")),
                        "input_labels_and_values": row.get("input_labels_and_values") or {}}
        out.append((getattr(eq, "internal_department", None), line, request_item))
    return out


def _by_department(priced: list[tuple]) -> list[tuple]:
    groups: dict = {}
    for dept, line, item in priced:
        key = dept.pk if dept is not None else None
        groups.setdefault(key, [dept, [], []])
        groups[key][1].append(line)
        groups[key][2].append(item)
    return [tuple(v) for v in groups.values()]


# --- issuing ---------------------------------------------------------------------------------------------------------------

def _format_texts() -> tuple[str, str]:
    from .document_exports import _get_proforma_invoice_format

    fmt = _get_proforma_invoice_format()
    return fmt["terms_and_conditions"], fmt["disclaimer"]


def _proforma_for(*, number, issued_at, buyer: Party, lines, department, requested_at) -> Proforma:
    return Proforma(
        number=number, issued_at=issued_at, buyer=buyer, lines=lines,
        department=(getattr(department, "name", "") or "").strip(), requested_at=requested_at, valid_days=VALID_DAYS,
        department_email=department_accounts_email(department),
    )


def _save(pi: Proforma, *, user, department, request_items, source, replaces=None, revision=0, actor=None) -> SavedProformaInvoice:
    terms, disclaimer = _format_texts()
    pdf = render_proforma(pi, extra_terms=terms, disclaimer=disclaimer)
    pos = pi.buyer.state_code or state_code_for(gstin=pi.buyer.gstin, state=pi.buyer.state)
    taxable = sum((ln.taxable for ln in pi.lines), ZERO)
    gst = sum((ln.gst for ln in pi.lines), ZERO)
    issued = timezone.localtime(pi.issued_at) if timezone.is_aware(pi.issued_at) else pi.issued_at
    record = SavedProformaInvoice(
        number=pi.number, user=user, department=department, issued_at=pi.issued_at,
        valid_until=(issued + timedelta(days=VALID_DAYS)).date(),
        buyer=asdict(pi.buyer),
        supplier={"department": pi.department, "contact_email": pi.department_email},
        lines=[{"title": ln.title, "details": ln.details, "sac": ln.sac, "qty": ln.qty, "taxable": str(ln.taxable),
                "gst": str(ln.gst), "total": str(ln.total)} for ln in pi.lines],
        request_items=request_items, taxable_total=taxable, gst_total=gst, total=taxable + gst,
        place_of_supply=pos, inter_state=bool(pos) and pos != "05", amount_source=source,
        revision=revision, replaces=replaces,
    )
    record.pdf.save(f"{pi.number}.pdf", ContentFile(pdf), save=False)
    record.save()
    ProformaEvent.objects.create(
        proforma=record, actor=actor or user, action="revised" if replaces else "issued",
        detail={"total": str(record.total), "lines": len(pi.lines), "amount_source": source,
                **({"replaces": replaces.number} if replaces else {})},
    )
    return record


def issue_for_estimate(user, line_items: list, requested_at) -> list[SavedProformaInvoice]:
    """Save the proformas for an estimate from the charges page (one per department) and return them."""
    signed_in = user is not None and getattr(user, "is_authenticated", False)
    items = [{"equipment_id": r.get("equipment_id"), "input_values": r.get("input_values") or {},
              "input_labels_and_values": r.get("input_labels_and_values") or {}}
             for r in (line_items or []) if isinstance(r, dict)]
    from .api_views import _is_admin_panel_user

    # Staff estimate charges for other user types (Book Equipment > Calculate charges), so their figures are kept as
    # shown; everyone else's proforma is priced again for their own account.
    if (signed_in and not _is_admin_panel_user(user) and items
            and all(i["equipment_id"] not in (None, "") for i in items)):
        priced, source = priced_lines(user, items), ProformaAmountSource.SERVER
    else:
        priced, source = client_lines(line_items), ProformaAmountSource.CLIENT
    if not priced:
        raise ProformaError("Add at least one equipment to the proforma.", field="line_items")
    buyer = buyer_from(user if signed_in else None)
    out = []
    with transaction.atomic():
        for department, lines, request_items in _by_department(priced):
            pi = _proforma_for(number=new_number(requested_at), issued_at=requested_at, buyer=buyer, lines=lines,
                               department=department, requested_at=requested_at)
            out.append(_save(pi, user=user if signed_in else None, department=department,
                             request_items=request_items, source=source))
    return out


def issue_single(*, user, billing_profile, equipment, base_charge, gst_amount, description="") -> SavedProformaInvoice:
    """Save the one-equipment estimate (amounts entered on the booking's invoice panel)."""
    now = timezone.now()
    department = getattr(equipment, "internal_department", None)
    line = Line(title=_title(equipment.name, equipment.code), details=[description] if description else [],
                taxable=dec(base_charge), gst=dec(gst_amount))
    pi = _proforma_for(number=new_number(now), issued_at=now, buyer=buyer_from(user, billing_profile), lines=[line],
                       department=department, requested_at=None)
    with transaction.atomic():
        return _save(pi, user=user, department=department,
                     request_items=[{"equipment_id": equipment.pk, "input_values": {}}],
                     source=ProformaAmountSource.CLIENT)


def merged_pdf(records: list[SavedProformaInvoice]) -> bytes:
    if len(records) == 1:
        return pdf_bytes(records[0])
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for r in records:
        for page in PdfReader(io.BytesIO(pdf_bytes(r))).pages:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _stamp(pdf: bytes, mark: str, note: str) -> bytes:
    """The saved PDF with a red CANCELLED / REPLACED mark and a one-line note on every page."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    reader = PdfReader(io.BytesIO(pdf))
    writer = PdfWriter()
    for page in reader.pages:
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(w, h))
        c.saveState()
        c.setFillColor(colors.HexColor("#B42318"))
        c.setFillAlpha(0.22)
        c.translate(w / 2, h / 2 + 40 * mm)
        c.rotate(35)
        c.setFont("Helvetica-Bold", 64)
        c.drawCentredString(0, 0, mark.upper())
        c.restoreState()
        c.setFillColor(colors.HexColor("#B42318"))
        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(w / 2, h - 30 * mm, note[:110])
        c.save()
        page.merge_page(PdfReader(io.BytesIO(buf.getvalue())).pages[0])
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def pdf_bytes(record: SavedProformaInvoice) -> bytes:
    with record.pdf.open("rb") as fh:
        data = fh.read()
    if record.status == ProformaStatus.CANCELLED:
        when = timezone.localtime(record.cancelled_at).strftime("%d %b %Y") if record.cancelled_at else ""
        return _stamp(data, "Cancelled", f"Cancelled on {when}. No payment should be made against this proforma.")
    if record.status == ProformaStatus.REPLACED:
        newer = getattr(record, "replaced_by", None)
        return _stamp(data, "Replaced", f"Replaced by proforma {newer.number if newer else 'a revision'}. "
                                        "Pay against the newer proforma.")
    return data


# --- state worked out from the linked payments ----------------------------------------------------------------------------

STATE_LABELS = {
    "issued": "Issued",
    "payment_submitted": "Payment submitted · awaiting approval",
    "part_paid": "Part paid",
    "paid": "Paid · awaiting reconciliation",
    "reconciled": "Paid and reconciled",
    "expired": "Expired",
    "cancelled": "Cancelled",
    "replaced": "Replaced by a revision",
}


def _payment_rows(record) -> list[dict]:
    from iic_booking.payments.models import PaymentOrderStatus
    from iic_booking.users.models.offline_recharge import OfflineReconciliationStatus
    from iic_booking.users.models.wallet import WalletRechargeRequestStatus

    rows = []
    for link in record.payments.select_related(
        "payment_order", "recharge_request", "recharge_request__offline_payment"
    ).order_by("created_at", "id"):
        row = {"id": link.pk, "kind": link.kind, "amount": str(link.amount), "created_at": link.created_at.isoformat(),
               "state": "started", "credited": ZERO, "pending": ZERO, "reconciled": False, "references": {}}
        if link.payment_order_id:
            order = link.payment_order
            row["references"] = {"razorpay_order_id": order.razorpay_order_id}
            if order.status == PaymentOrderStatus.PAID:
                row["state"] = "credited"
                row["credited"] = order.base_amount
                pay = order.payments.order_by("-created_at").first()
                if pay is not None:
                    from iic_booking.payments.receipts import _settlement_utr

                    utr = _settlement_utr(pay)
                    row["references"].update({"razorpay_payment_id": pay.razorpay_payment_id, "settlement_utr": utr,
                                              "receipt_number": getattr(pay, "receipt_number", "") or ""})
                    row["reconciled"] = bool(utr)
            elif order.status == PaymentOrderStatus.CREATED:
                row["state"] = "started"  # checkout opened, not paid; not counted
            else:
                row["state"] = "failed"
        else:
            req = link.recharge_request
            detail = getattr(req, "offline_payment", None)
            row["references"] = {"request": req.transaction_number,
                                 "utr": getattr(detail, "transaction_reference", "") or "",
                                 "payment_date": detail.payment_date.isoformat() if detail and detail.payment_date else ""}
            if req.status == WalletRechargeRequestStatus.PENDING:
                row["state"] = "pending"
                row["pending"] = dec(req.amount)
            elif req.status == WalletRechargeRequestStatus.APPROVED:
                row["state"] = "credited"
                row["credited"] = dec(req.amount)
                rec = getattr(detail, "reconciliation_status", "")
                row["reconciled"] = rec == OfflineReconciliationStatus.RECONCILED
                if rec == OfflineReconciliationStatus.MISMATCH:
                    row["state"] = "mismatch"
                if detail is not None and detail.reconciled_reference:
                    row["references"]["reconciled_reference"] = detail.reconciled_reference
            else:
                row["state"] = req.status.lower()  # rejected / cancelled: not counted
        rows.append(row)
    return rows


def state_of(record, rows=None) -> dict:
    rows = _payment_rows(record) if rows is None else rows
    credited = sum((r["credited"] for r in rows), ZERO)
    pending = sum((r["pending"] for r in rows), ZERO)
    reconciled = sum((r["credited"] for r in rows if r["reconciled"]), ZERO)
    outstanding = max(record.total - credited - pending, ZERO)
    if record.status == ProformaStatus.CANCELLED:
        key = "cancelled"
    elif record.status == ProformaStatus.REPLACED:
        key = "replaced"
    elif credited >= record.total > 0:
        key = "reconciled" if reconciled >= credited else "paid"
    elif credited > 0:
        key = "part_paid"
    elif pending > 0:
        key = "payment_submitted"
    elif record.valid_until < timezone.localdate():
        key = "expired"
    else:
        key = "issued"
    active = record.status == ProformaStatus.ISSUED
    untouched = credited == 0 and pending == 0
    return {
        "key": key, "label": STATE_LABELS[key], "credited": str(credited), "pending": str(pending),
        "reconciled": str(reconciled), "outstanding": str(outstanding),
        "can_pay": active and key != "expired" and outstanding > 0,
        "can_edit": active and untouched, "can_cancel": active and untouched,
    }


def serialize(record, *, with_history: bool = False) -> dict:
    rows = _payment_rows(record)
    state = state_of(record, rows)
    out = {
        "id": record.pk, "number": record.number, "issued_at": record.issued_at.isoformat(),
        "valid_until": record.valid_until.isoformat(),
        "department": {"id": record.department_id, "name": getattr(record.department, "name", "") or ""},
        "buyer": {"name": record.buyer.get("name", ""), "organisation": record.buyer.get("organisation", ""),
                  "email": record.buyer.get("email", ""), "gstin": record.buyer.get("gstin", "")},
        "user_id": record.user_id,
        "lines": record.lines, "request_items": record.request_items,
        "taxable_total": str(record.taxable_total), "gst_total": str(record.gst_total), "total": str(record.total),
        "place_of_supply": record.place_of_supply,
        "place_of_supply_name": STATE_CODES.get(record.place_of_supply, ""),
        "inter_state": record.inter_state, "amount_source": record.amount_source, "revision": record.revision,
        "replaces": record.replaces.number if record.replaces_id else "",
        "replaced_by": getattr(getattr(record, "replaced_by", None), "number", ""),
        "cancel_reason": record.cancel_reason,
        "cancelled_at": record.cancelled_at.isoformat() if record.cancelled_at else None,
        "state": state,
        "payments": [{**r, "credited": str(r["credited"]), "pending": str(r["pending"])} for r in rows],
    }
    if with_history:
        out["history"] = [
            {"at": e.at.isoformat(), "action": e.action, "detail": e.detail,
             "actor": (getattr(e.actor, "name", "") or getattr(e.actor, "email", "")) if e.actor_id else ""}
            for e in record.events.select_related("actor").order_by("at", "id")
        ]
    return out


# --- who sees what -----------------------------------------------------------------------------------------------------

def own_queryset(user):
    return SavedProformaInvoice.objects.filter(user=user).select_related("department", "replaces", "replaced_by")


def staff_role(user) -> str:
    from iic_booking.users.offline_recharge import approver_role

    return approver_role(user)


def staff_queryset(user):
    """Main Administrator: all; Department Administrator / Accounts In Charge: their department's proformas."""
    qs = SavedProformaInvoice.objects.select_related("department", "user", "replaces", "replaced_by")
    role = staff_role(user)
    if role == "main_admin":
        return qs
    if role in ("dept_admin", "accounts"):
        return qs.filter(department_id=user.department_id)
    return qs.none()


def _lock_own(user, proforma_id: int) -> SavedProformaInvoice:
    record = SavedProformaInvoice.objects.select_for_update().filter(pk=proforma_id, user=user).first()
    if record is None:
        raise ProformaError("Proforma not found.", status=404, code="not_found")
    return record


# --- cancel and revise ---------------------------------------------------------------------------------------------------

def cancel(user, proforma_id: int, *, reason: str = "") -> SavedProformaInvoice:
    with transaction.atomic():
        record = _lock_own(user, proforma_id)
        if not state_of(record)["can_cancel"]:
            raise ProformaError("This proforma can no longer be cancelled: it is closed or a payment is recorded against it.",
                                status=409, code="not_cancellable")
        record.status = ProformaStatus.CANCELLED
        record.cancelled_at = timezone.now()
        record.cancelled_by = user
        record.cancel_reason = (reason or "").strip()[:300]
        record.save(update_fields=["status", "cancelled_at", "cancelled_by", "cancel_reason", "updated_at"])
        ProformaEvent.objects.create(proforma=record, actor=user, action="cancelled",
                                     detail={"reason": record.cancel_reason})
    return record


def _revision_number(record) -> tuple[str, int]:
    root = record
    while root.replaces_id:
        root = root.replaces
    n = record.revision + 1
    return f"{root.number}-R{n}", n


def revise(user, proforma_id: int, items: list) -> SavedProformaInvoice:
    """Issue a revision of the user's proforma from new equipment and inputs; the old one becomes REPLACED."""
    priced = priced_lines(user, items)
    with transaction.atomic():
        record = _lock_own(user, proforma_id)
        if not state_of(record)["can_edit"]:
            raise ProformaError("This proforma can no longer be edited: it is closed or a payment is recorded against it.",
                                status=409, code="not_editable")
        departments = {getattr(d, "pk", None) for d, _, _ in priced}
        if departments != {record.department_id}:
            raise ProformaError("A revision can only hold equipment of the same department as the proforma. "
                                "Make a new proforma for other departments.", field="items")
        number, n = _revision_number(record)
        now = timezone.now()
        pi = _proforma_for(number=number, issued_at=now, buyer=buyer_from(user), lines=[ln for _, ln, _ in priced],
                           department=record.department, requested_at=now)
        record.status = ProformaStatus.REPLACED
        record.save(update_fields=["status", "updated_at"])
        new = _save(pi, user=user, department=record.department, request_items=[i for _, _, i in priced],
                    source=ProformaAmountSource.SERVER, replaces=record, revision=n, actor=user)
        ProformaEvent.objects.create(proforma=record, actor=user, action="replaced", detail={"by": new.number})
    return new


# --- paying ---------------------------------------------------------------------------------------------------------------

def _amount(raw, outstanding: Decimal) -> Decimal:
    if raw in (None, ""):
        return outstanding
    amount = dec(raw)
    if amount <= 0:
        raise ProformaError("Enter an amount above zero.", field="amount")
    if amount > outstanding:
        raise ProformaError(f"The amount cannot be more than the ₹{outstanding} still to pay on this proforma.",
                            field="amount")
    return amount


def _payable(record) -> Decimal:
    state = state_of(record)
    if not state["can_pay"]:
        raise ProformaError("Nothing can be paid on this proforma: it is paid, expired, cancelled or replaced.",
                            status=409, code="not_payable")
    if record.department_id is None:
        raise ProformaError("This proforma has no department to pay to. Make a new proforma from the charges page.",
                            status=409, code="no_department")
    return Decimal(state["outstanding"])


def start_online_payment(user, proforma_id: int, amount=None) -> dict:
    """Open a Razorpay wallet-recharge order for the proforma's department; the browser then runs the usual checkout."""
    from iic_booking.payments.razorpay_service import create_order
    from iic_booking.payments.models import PaymentPurpose
    from iic_booking.users.models.wallet_sric_settings import online_gateway_recharge_enabled
    from iic_booking.users.repositories.wallet_repository import (
        WalletRepository,
        resolve_internal_department_for_wallet_recharge,
    )
    from iic_booking.users.student_wallet_recharge import assert_iitr_student_may_recharge

    with transaction.atomic():
        record = _lock_own(user, proforma_id)
        amount = _amount(amount, _payable(record))
        if not online_gateway_recharge_enabled(department=record.department_id):
            raise ProformaError("Online payment is not available for this department. Record an offline payment instead.",
                                status=403, code="online_disabled")
        forbidden = assert_iitr_student_may_recharge(user, department_id=record.department_id)
        if forbidden:
            raise ProformaError(forbidden, status=403, code="forbidden")
        wallet = user.get_accessible_wallet()
        if wallet is None and user.can_have_wallet():
            wallet, _ = WalletRepository.get_or_create(user)
        if wallet is None:
            raise ProformaError("Your account has no wallet.", status=403, code="no_wallet")
        department = resolve_internal_department_for_wallet_recharge(wallet, record.department_id)
        if department is None:
            raise ProformaError("This department cannot be paid from your wallet.", status=403, code="invalid_department")
        order, payload = create_order(user=user, purpose=PaymentPurpose.WALLET_RECHARGE, base_amount=amount,
                                      wallet=wallet, department=department)
        ProformaPayment.objects.create(proforma=record, kind=ProformaPaymentKind.ONLINE, payment_order=order,
                                       amount=amount, created_by=user)
        ProformaEvent.objects.create(proforma=record, actor=user, action="online_payment_started",
                                     detail={"amount": str(amount), "razorpay_order_id": order.razorpay_order_id})
    return {**payload, "proforma_id": record.pk, "proforma_number": record.number}


def record_offline_payment(user, proforma_id: int, data, upload):
    """Submit an offline payment (with its receipt) against the proforma; it goes to approval and reconciliation."""
    from iic_booking.users import offline_recharge

    with transaction.atomic():
        record = _lock_own(user, proforma_id)
        outstanding = _payable(record)
        amount = _amount(data.get("amount"), outstanding)
        fields = {k: data.get(k) for k in data.keys()}
        fields.update({"department_id": record.department_id, "amount": str(amount)})
        remarks = str(fields.get("remarks") or "").strip()
        fields["remarks"] = f"Against proforma {record.number}." + (f" {remarks}" if remarks else "")
        try:
            result = offline_recharge.submit(user, fields, upload)
        except offline_recharge.OfflineRechargeError as exc:
            raise ProformaError(exc.message, status=exc.status, code=exc.code, field=exc.field) from None
        req = result.request
        if not ProformaPayment.objects.filter(recharge_request=req).exists():
            ProformaPayment.objects.create(proforma=record, kind=ProformaPaymentKind.OFFLINE, recharge_request=req,
                                           amount=amount, created_by=user)
            ProformaEvent.objects.create(proforma=record, actor=user, action="offline_payment_submitted",
                                         detail={"amount": str(amount), "request": req.transaction_number})
    return req
