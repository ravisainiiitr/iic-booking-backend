"""Monthly registers that go with the GSTR-1 JSON for SRIC's B2C documents (buyers without a GSTIN, signed with the
DSC through the signing utility):

* Invoice Register (Excel): one row per B2C tax invoice of the month, with the buyer, place of supply, taxes, receipt,
  booking, the DSC signature, the GSTR-1 table it goes to and any credit notes issued against it.
* Credit Note Register (Excel): one row per B2C credit note of the month, with the original invoice's number, date,
  value and return period, the GSTR-1 treatment (CDNUR or netted in B2CS) and the reason.

Both list every numbered document of the month, including those still waiting for the DSC; the column
"In GSTR-1 JSON" says which rows the JSON of the same month contains, and the totals row adds up only those, so the
registers reconcile with the JSON. B2B documents (with a GSTIN) are e-invoices and reach GSTR-1 from the IRP; they
are not in these registers.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .gstin import STATE_CODES
from .gstr1 import B2CL_LIMIT, ZERO, _month_bounds

INVOICE_COLUMNS = [
    ("Sl. No.", 7), ("Invoice No.", 16), ("Invoice Date", 12), ("Status", 22), ("In GSTR-1 JSON", 10),
    ("GSTR-1 Table", 16), ("Buyer Name", 28), ("Buyer Address", 36), ("Buyer State", 16), ("Place of Supply", 18),
    ("Supply", 10), ("SAC", 9), ("GST Rate %", 9), ("Taxable Value", 14), ("CGST", 12), ("SGST", 12), ("IGST", 12),
    ("Round Off", 10), ("Invoice Value", 14), ("Receipt No.", 20), ("Receipt Date", 12), ("Booking Ref.", 16),
    ("FBR No.", 14), ("Equipment", 26), ("Department / Facility", 24), ("Signed On", 18), ("Signed By", 20),
    ("DSC Certificate Serial", 22), ("Credit Notes Issued", 22), ("Credited Value", 14), ("Net Value", 14),
]
CREDIT_NOTE_COLUMNS = [
    ("Sl. No.", 7), ("Credit Note No.", 16), ("Credit Note Date", 12), ("Status", 22), ("In GSTR-1 JSON", 10),
    ("GSTR-1 Treatment", 22), ("Original Invoice No.", 16), ("Original Invoice Date", 12),
    ("Original Invoice Value", 14), ("Original Return Period", 12), ("Original GSTR-1 Table", 14),
    ("Buyer Name", 28), ("Buyer State", 16), ("Place of Supply", 18), ("Supply", 10), ("SAC", 9),
    ("GST Rate %", 9), ("Taxable Value", 14), ("CGST", 12), ("SGST", 12), ("IGST", 12), ("Credit Note Value", 14),
    ("Reason", 36), ("Refund Ref.", 12), ("Refund Date", 12), ("Receipt No.", 20), ("Booking Ref.", 16),
    ("Signed On", 18), ("Signed By", 20), ("DSC Certificate Serial", 22),
]
MONEY = '#,##0.00'
AMOUNT_HEADERS = {"Taxable Value", "CGST", "SGST", "IGST", "Round Off", "Invoice Value", "Credited Value",
                  "Net Value", "Original Invoice Value", "Credit Note Value"}


def _state(code: str) -> str:
    return f"{code}-{STATE_CODES.get(code, '')}" if code else ""


def _when(dt) -> str:
    if not dt:
        return ""
    from django.utils import timezone

    return timezone.localtime(dt).strftime("%d-%m-%Y %H:%M")


def _person(user) -> str:
    if user is None:
        return ""
    return (user.get_full_name() or "").strip() or user.get_username()


def _rates(inv) -> tuple[str, str]:
    lines = list(inv.lines.all())
    sacs = sorted({ln.sac_code for ln in lines}) or ["998397"]
    rates = sorted({ln.gst_rate for ln in lines})
    return ", ".join(sacs), ", ".join(f"{r.normalize():f}" for r in rates)


def _address(c) -> str:
    return ", ".join(x for x in (c.address1, c.address2, c.location, c.pincode) if x)


def _table(inv) -> str:
    return "B2CL" if inv.inter_state and inv.total > B2CL_LIMIT else "B2CS"


@dataclass
class Register:
    title: str
    columns: list
    rows: list
    totals: dict
    notes: list
    year: int
    month: int
    gstin: str

    @property
    def fp(self) -> str:
        return f"{self.month:02d}{self.year}"


def invoice_register(year: int, month: int, *, start: date | None = None, end: date | None = None) -> Register:
    from .models import DocType, EInvoice, EinvoiceSettings, InvoiceStatus, SupplyType

    s = EinvoiceSettings.get()
    lo, hi = _month_bounds(year, month, start, end)
    qs = (EInvoice.objects.filter(doc_type=DocType.INVOICE, supply_type=SupplyType.B2C, invoice_date__gte=lo,
                                  invoice_date__lte=hi).exclude(invoice_number="")
          .select_related("customer", "signed_by", "signer_certificate").prefetch_related("lines", "credit_notes")
          .order_by("invoice_number"))
    rows, totals = [], {k: ZERO for k in ("Taxable Value", "CGST", "SGST", "IGST", "Round Off", "Invoice Value")}
    included = waiting = 0
    for i, inv in enumerate(qs, start=1):
        in_json = inv.status == InvoiceStatus.SIGNED
        notes = [n for n in inv.credit_notes.all() if n.invoice_number]
        credited = sum((n.total for n in notes), ZERO)
        sac, rate = _rates(inv)
        c = inv.customer
        rows.append([
            i, inv.invoice_number, inv.invoice_date, inv.get_status_display(), "Yes" if in_json else "No",
            _table(inv) if in_json else "Not yet (awaiting DSC)", c.legal_name, _address(c), _state(c.state_code),
            _state(inv.place_of_supply), "Inter-state" if inv.inter_state else "Intra-state", sac, rate,
            inv.taxable_value, inv.cgst, inv.sgst, inv.igst, inv.round_off, inv.total, inv.receipt_number,
            inv.receipt_date, inv.booking_reference, inv.fbr_number, inv.equipment_name, inv.department_name,
            _when(inv.signed_at), _person(inv.signed_by),
            inv.signer_certificate.serial_number if inv.signer_certificate else "",
            ", ".join(n.invoice_number for n in notes), credited, inv.total - credited,
        ])
        if in_json:
            included += 1
            for k, v in (("Taxable Value", inv.taxable_value), ("CGST", inv.cgst), ("SGST", inv.sgst),
                         ("IGST", inv.igst), ("Round Off", inv.round_off), ("Invoice Value", inv.total)):
                totals[k] += v
        elif inv.status == InvoiceStatus.AWAITING_SIGNATURE:
            waiting += 1
    notes = [f"B2C tax invoices (buyers without a GSTIN) dated {lo:%d-%m-%Y} to {hi:%d-%m-%Y}: {len(rows)}; "
             f"in the GSTR-1 JSON (signed with the DSC): {included}."]
    if waiting:
        notes.append(f"{waiting} invoice(s) still wait for the DSC: sign them in the utility, then export again.")
    notes.append("Totals add up only the rows in the GSTR-1 JSON. Credit notes are listed in the Credit Note "
                 "Register of the month they are issued.")
    return Register("Invoice Register", INVOICE_COLUMNS, rows, totals, notes, year, month, s.supplier_gstin)


def credit_note_register(year: int, month: int, *, start: date | None = None, end: date | None = None) -> Register:
    from .models import ISSUED_STATUSES, DocType, EInvoice, EinvoiceSettings, InvoiceStatus, SupplyType

    s = EinvoiceSettings.get()
    lo, hi = _month_bounds(year, month, start, end)
    qs = (EInvoice.objects.filter(doc_type=DocType.CREDIT_NOTE, supply_type=SupplyType.B2C, invoice_date__gte=lo,
                                  invoice_date__lte=hi).exclude(invoice_number="")
          .select_related("customer", "original_invoice", "refund", "signed_by", "signer_certificate")
          .prefetch_related("lines").order_by("invoice_number"))
    rows, totals = [], {k: ZERO for k in ("Taxable Value", "CGST", "SGST", "IGST", "Credit Note Value")}
    included = waiting = 0
    for i, note in enumerate(qs, start=1):
        in_json = note.status in ISSUED_STATUSES
        orig = note.original_invoice
        large = bool(orig and orig.inter_state and orig.total > B2CL_LIMIT)
        sac, rate = _rates(note)
        c = note.customer
        refund = note.refund
        rows.append([
            i, note.invoice_number, note.invoice_date, note.get_status_display(), "Yes" if in_json else "No",
            ("CDNUR (against B2CL)" if large else "Netted in B2CS") if in_json else "Not yet (awaiting DSC)",
            orig.invoice_number if orig else "", orig.invoice_date if orig else None, orig.total if orig else None,
            orig.invoice_date.strftime("%m%Y") if orig and orig.invoice_date else "",
            ("B2CL" if large else "B2CS") if orig else "", c.legal_name, _state(c.state_code),
            _state(note.place_of_supply), "Inter-state" if note.inter_state else "Intra-state", sac, rate,
            note.taxable_value, note.cgst, note.sgst, note.igst, note.total, note.credit_reason,
            refund.pk if refund else "", refund.created_at.date() if refund else None, note.receipt_number,
            note.booking_reference, _when(note.signed_at), _person(note.signed_by),
            note.signer_certificate.serial_number if note.signer_certificate else "",
        ])
        if in_json:
            included += 1
            for k, v in (("Taxable Value", note.taxable_value), ("CGST", note.cgst), ("SGST", note.sgst),
                         ("IGST", note.igst), ("Credit Note Value", note.total)):
                totals[k] += v
        elif note.status == InvoiceStatus.AWAITING_SIGNATURE:
            waiting += 1
    notes = [f"B2C credit notes dated {lo:%d-%m-%Y} to {hi:%d-%m-%Y}: {len(rows)}; in the GSTR-1 JSON (signed with "
             f"the DSC): {included}."]
    if not rows:
        notes.append("No credit notes in this period.")
    if waiting:
        notes.append(f"{waiting} credit note(s) still wait for the DSC: sign them in the utility, then export again.")
    notes.append("Credit notes against B2CL invoices (inter-state, above Rs 1 lakh) go to CDNUR; the others are "
                 "netted into B2CS of this month. Totals add up only the rows in the GSTR-1 JSON.")
    return Register("Credit Note Register", CREDIT_NOTE_COLUMNS, rows, totals, notes, year, month, s.supplier_gstin)


def to_excel(reg: Register) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = reg.title
    headers = [h for h, _w in reg.columns]
    ws.append([f"{reg.title} - B2C (buyers without a GSTIN) - GSTIN {reg.gstin} - return period {reg.fp} - "
               "SANDBOX DEMONSTRATION, not for filing"])
    ws["A1"].font = Font(bold=True, size=12)
    ws.append([])
    ws.append(headers)
    head = PatternFill("solid", fgColor="1F3A5F")
    for cell in ws[3]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for row in reg.rows:
        ws.append(row)
    total_row = [""] * len(headers)
    total_row[1] = "Total (in GSTR-1 JSON)"
    for k, v in reg.totals.items():
        total_row[headers.index(k)] = v
    ws.append(total_row)
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    for idx, (h, width) in enumerate(reg.columns, start=1):
        letter = ws.cell(3, idx).column_letter
        ws.column_dimensions[letter].width = width
        for r in range(4, ws.max_row + 1):
            cell = ws.cell(r, idx)
            if isinstance(cell.value, Decimal):
                cell.value = float(cell.value)
                cell.number_format = MONEY
            elif isinstance(cell.value, date):
                cell.number_format = "DD-MM-YYYY"
    ws.freeze_panes = "C4"
    ws.auto_filter.ref = f"A3:{ws.cell(3, len(headers)).column_letter}{max(3, ws.max_row - 1)}"
    info = wb.create_sheet("read me")
    for line in reg.notes:
        info.append([line])
    info.column_dimensions["A"].width = 120
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def filename(reg: Register) -> str:
    stem = "InvoiceRegister" if reg.title == "Invoice Register" else "CreditNoteRegister"
    return f"{stem}-B2C-{reg.gstin}-{reg.fp}-sandbox.xlsx"
