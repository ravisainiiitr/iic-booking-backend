"""GSTR-1 export of the portal's B2C invoices (buyers without a GSTIN) for SRIC, one file per calendar month (SRIC
files monthly; no quarterly / IFF option). E-invoices (B2B, with IRN) are not exported: the GST system fills them
into GSTR-1 from the IRP.

* B2CS (Table 7): every intra-state B2C supply, and inter-state B2C invoices up to ``B2CL_LIMIT``, summarised by
  supply type, place of supply and rate; credit notes against these are netted in the month the note is issued.
* B2CL (Table 5): inter-state B2C invoices above ``B2CL_LIMIT`` (₹1 lakh from the August 2024 tax period), invoice by
  invoice, items by rate.
* CDNUR (Table 9B): credit notes against B2CL invoices.
* HSN (Table 12, B2C tab): by SAC and rate.
* Documents issued (Table 13): the invoice series (all numbered invoices of the month, B2B and B2C) and the
  credit-note series, from/to, total, cancelled (always 0: there is no cancellation).

Only invoices signed with the DSC are issued; numbered B2C invoices still waiting for the signature are left out and
reported as a warning. Credit notes come from ``credit_note_rows`` (empty until the portal issues credit notes).

GSTN publishes no formal JSON schema for the offline tool. The shapes below follow the offline tool's upload JSON
(gstin, fp, b2cs, b2cl, cdnur, hsn.hsn_b2c, doc_issue.doc_det) and its Excel template sheets; the version string,
UQC for services and the Excel headers are kept together in ``FORMAT`` so they can be adjusted to the tool SRIC uses.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .gstin import STATE_CODES

B2CL_LIMIT = Decimal("100000.00")
ZERO = Decimal("0.00")
FORMAT = {
    "version": "GST3.2.4",          # offline tool version SRIC uses; confirm before filing
    "service_uqc": "NA",            # GSTN: services (SAC 99...) are reported with UQC NA and no quantity
    "doc_invoices": (1, "Invoices for outward supply"),
    "doc_credit_notes": (5, "Credit Note"),
    "excel_sheets": {
        "b2cl": ["Invoice Number", "Invoice date", "Invoice Value", "Place Of Supply", "Applicable % of Tax Rate",
                 "Rate", "Taxable Value", "Cess Amount", "E-Commerce GSTIN"],
        "b2cs": ["Type", "Place Of Supply", "Applicable % of Tax Rate", "Rate", "Taxable Value", "Cess Amount",
                 "E-Commerce GSTIN"],
        "cdnur": ["UR Type", "Note Number", "Note Date", "Note Type", "Place Of Supply", "Note Value",
                  "Applicable % of Tax Rate", "Rate", "Taxable Value", "Cess Amount"],
        "hsn(b2c)": ["HSN", "Description", "UQC", "Total Quantity", "Total Value", "Rate", "Taxable Value",
                     "Integrated Tax Amount", "Central Tax Amount", "State/UT Tax Amount", "Cess Amount"],
        "docs": ["Nature of Document", "Sr. No. From", "Sr. No. To", "Total Number", "Cancelled"],
    },
}


def _f(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def _rate(value) -> float:
    d = Decimal(str(value))
    return float(d.normalize()) if d == d.to_integral() else float(d)


def _pos(code: str) -> str:
    return f"{code}-{STATE_CODES.get(code, '')}"


def months_in(start: date, end: date) -> list[tuple[int, int]]:
    if end < start:
        raise ValueError("The end date is before the start date.")
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _month_bounds(year: int, month: int, start: date | None, end: date | None) -> tuple[date, date]:
    first = date(year, month, 1)
    last = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)
    return max(first, start or first), min(last, end or last)


# --- credit notes (none yet; credit_note_rows is replaced when the portal issues credit notes) ---------------------

@dataclass
class CreditNoteRow:
    """A credit note as the export needs it. ``lines``: (rate, taxable, iamt, camt, samt) per rate, positive amounts."""

    number: str
    note_date: date
    original_number: str
    original_date: date
    supply_type: str            # "B2C" / "B2B"
    original_b2cl: bool
    inter_state: bool
    pos: str
    value: Decimal
    lines: list[tuple] = field(default_factory=list)
    sac: str = "998397"


def credit_note_rows(start: date, end: date) -> list[CreditNoteRow]:
    """Issued credit notes dated within [start, end]. The portal issues no credit notes yet (no cancellation or
    credit-note flow), so this is empty; when credit notes exist they are listed here."""
    try:
        from .credit_notes import export_rows  # added with the credit-note flow
    except ImportError:
        return []
    return export_rows(start, end)


# --- builder ----------------------------------------------------------------------------------------------------

@dataclass
class MonthExport:
    year: int
    month: int
    start: date
    end: date
    payload: dict
    warnings: list[str]
    summary: dict

    @property
    def fp(self) -> str:
        return f"{self.month:02d}{self.year}"

    def filename(self, ext: str) -> str:
        return f"GSTR1-B2C-{self.payload['gstin']}-{self.fp}-sandbox.{ext}"


def build_month(year: int, month: int, *, start: date | None = None, end: date | None = None) -> MonthExport:
    from .models import EInvoice, EinvoiceSettings, InvoiceStatus, SupplyType

    s = EinvoiceSettings.get()
    lo, hi = _month_bounds(year, month, start, end)
    in_range = (EInvoice.objects.filter(invoice_date__gte=lo, invoice_date__lte=hi, doc_type="INV")
                .exclude(invoice_number=""))
    b2c = list(in_range.filter(supply_type=SupplyType.B2C, status=InvoiceStatus.SIGNED)
               .select_related("customer").prefetch_related("lines").order_by("invoice_number"))
    unsigned = list(in_range.filter(supply_type=SupplyType.B2C, status=InvoiceStatus.AWAITING_SIGNATURE)
                    .values_list("invoice_number", flat=True))
    warnings = []
    if unsigned:
        warnings.append(f"{len(unsigned)} B2C invoice(s) of this period still wait for the DSC and are not included: "
                        + ", ".join(sorted(unsigned)) + ". Sign them before filing.")
    if (lo, hi) != _month_bounds(year, month, None, None):
        warnings.append(f"Only {lo:%d-%m-%Y} to {hi:%d-%m-%Y} is included, not the whole month; a return covers the "
                        "whole month.")

    b2cs = defaultdict(lambda: {"txval": ZERO, "iamt": ZERO, "camt": ZERO, "samt": ZERO})
    b2cl = defaultdict(list)
    hsn = defaultdict(lambda: {"qty": ZERO, "val": ZERO, "txval": ZERO, "iamt": ZERO, "camt": ZERO, "samt": ZERO})
    for inv in b2c:
        large = inv.inter_state and inv.total > B2CL_LIMIT
        by_rate = defaultdict(lambda: {"txval": ZERO, "iamt": ZERO, "camt": ZERO, "samt": ZERO})
        for line in inv.lines.all():
            r = by_rate[line.gst_rate]
            r["txval"] += line.taxable_value
            r["iamt"] += line.igst
            r["camt"] += line.cgst
            r["samt"] += line.sgst
            h = hsn[(line.sac_code, line.gst_rate)]
            h["txval"] += line.taxable_value
            h["iamt"] += line.igst
            h["camt"] += line.cgst
            h["samt"] += line.sgst
            h["val"] += line.total
        if large:
            b2cl[inv.place_of_supply].append({
                "inum": inv.invoice_number, "idt": inv.invoice_date.strftime("%d-%m-%Y"), "val": _f(inv.total),
                "itms": [{"num": i, "itm_det": {"rt": _rate(rt), "txval": _f(v["txval"]), "iamt": _f(v["iamt"]),
                                                "csamt": 0}}
                         for i, (rt, v) in enumerate(sorted(by_rate.items()), start=1)],
            })
        else:
            for rt, v in by_rate.items():
                key = ("INTER" if inv.inter_state else "INTRA", inv.place_of_supply, rt)
                for k in v:
                    b2cs[key][k] += v[k]

    notes = credit_note_rows(lo, hi)
    unsigned_notes = sorted(EInvoice.objects.filter(doc_type="CRN", supply_type=SupplyType.B2C,
                                                    status=InvoiceStatus.AWAITING_SIGNATURE, invoice_date__gte=lo,
                                                    invoice_date__lte=hi).exclude(invoice_number="")
                            .values_list("invoice_number", flat=True))
    if unsigned_notes:
        warnings.append(f"{len(unsigned_notes)} B2C credit note(s) of this period still wait for the DSC and are not "
                        "included: " + ", ".join(unsigned_notes) + ". Sign them before filing.")
    cdnur = []
    for note in (n for n in notes if n.supply_type == "B2C"):
        if note.original_b2cl:
            cdnur.append({
                "typ": "B2CL", "ntty": "C", "nt_num": note.number, "nt_dt": note.note_date.strftime("%d-%m-%Y"),
                "val": _f(note.value), "pos": note.pos,
                "itms": [{"num": i, "itm_det": {"rt": _rate(rt), "txval": _f(tx), "iamt": _f(ia), "csamt": 0}}
                         for i, (rt, tx, ia, _ca, _sa) in enumerate(note.lines, start=1)],
            })
        else:  # B2CS: netted into the summary of the month the note is issued
            for rt, tx, ia, ca, sa in note.lines:
                v = b2cs[("INTER" if note.inter_state else "INTRA", note.pos, rt)]
                v["txval"] -= tx
                v["iamt"] -= ia
                v["camt"] -= ca
                v["samt"] -= sa
        for rt, tx, ia, ca, sa in note.lines:
            h = hsn[(note.sac, rt)]
            h["txval"] -= tx
            h["iamt"] -= ia
            h["camt"] -= ca
            h["samt"] -= sa
            h["val"] -= tx + ia + ca + sa

    payload = {"gstin": s.supplier_gstin, "fp": f"{month:02d}{year}", "version": FORMAT["version"], "hash": "hash"}
    payload["b2cs"] = []
    for (sply, pos, rt), v in sorted(b2cs.items()):
        row = {"sply_ty": sply, "pos": pos, "typ": "OE", "rt": _rate(rt), "txval": _f(v["txval"]), "csamt": 0}
        if sply == "INTER":
            row["iamt"] = _f(v["iamt"])
        else:
            row["camt"], row["samt"] = _f(v["camt"]), _f(v["samt"])
        payload["b2cs"].append(row)
    payload["b2cl"] = [{"pos": pos, "inv": invs} for pos, invs in sorted(b2cl.items())]
    payload["cdnur"] = cdnur
    uqc = FORMAT["service_uqc"]
    payload["hsn"] = {"hsn_b2c": [
        {"num": i, "hsn_sc": sac, "desc": "Research and development services (testing / analysis)", "uqc": uqc,
         "qty": 0 if uqc == "NA" else _f(v["qty"]), "rt": _rate(rt), "txval": _f(v["txval"]), "iamt": _f(v["iamt"]),
         "camt": _f(v["camt"]), "samt": _f(v["samt"]), "csamt": 0}
        for i, ((sac, rt), v) in enumerate(sorted(hsn.items()), start=1)]}

    # Table 13: the whole invoice series of the month (B2B e-invoices too) and the credit-note series.
    numbers = sorted(in_range.values_list("invoice_number", flat=True))
    docs = []
    if numbers:
        num, _label = FORMAT["doc_invoices"]
        docs.append({"doc_num": num, "docs": [_series_entry(1, numbers)]})
    cn_numbers = sorted(n.number for n in notes)
    if cn_numbers:
        num, _label = FORMAT["doc_credit_notes"]
        docs.append({"doc_num": num, "docs": [_series_entry(1, cn_numbers)]})
    payload["doc_issue"] = {"doc_det": docs}
    for key in ("b2cs", "b2cl", "cdnur"):
        if not payload[key]:
            del payload[key]
    if not payload["hsn"]["hsn_b2c"]:
        del payload["hsn"]
    if not docs:
        del payload["doc_issue"]

    summary = {
        "b2c_invoices": len(b2c), "b2cl_invoices": sum(len(v) for v in b2cl.values()),
        "b2cs_rows": len(payload.get("b2cs", [])), "credit_notes": len(notes),
        "credit_notes_available": _credit_notes_exist(),
        "taxable_value": f"{sum((i.taxable_value for i in b2c), ZERO):.2f}",
        "tax": f"{sum((i.cgst + i.sgst + i.igst for i in b2c), ZERO):.2f}",
        "documents_issued": len(numbers), "unsigned_excluded": len(unsigned),
    }
    if not notes:
        warnings.append("No credit notes in this period; the CDNUR section is empty." if summary["credit_notes_available"]
                        else "No credit notes: the portal issues none (there is no credit-note flow yet); the CDNUR "
                             "section is empty.")
    return MonthExport(year, month, lo, hi, payload, warnings, summary)


def _credit_notes_exist() -> bool:
    try:
        from . import credit_notes  # noqa: F401
    except ImportError:
        return False
    return True


def _series_entry(num: int, numbers: list[str]) -> dict:
    return {"num": num, "from": numbers[0], "to": numbers[-1], "totnum": len(numbers), "cancel": 0,
            "net_issue": len(numbers)}


# --- files --------------------------------------------------------------------------------------------------------

def to_json(export: MonthExport) -> bytes:
    return json.dumps(export.payload, indent=1, ensure_ascii=False).encode("utf-8")


def to_excel(export: MonthExport) -> bytes:
    """The offline tool's Excel template sheets (b2cl, b2cs, cdnur, hsn(b2c), docs) with its column headers."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    p = export.payload
    wb = Workbook()
    wb.remove(wb.active)
    sheets = FORMAT["excel_sheets"]

    def sheet(name, rows):
        ws = wb.create_sheet(name)
        ws.append(sheets[name])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in rows:
            ws.append(row)
        for i, header in enumerate(sheets[name], start=1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = max(14, len(header) + 2)

    def xdate(text):  # dd-mm-yyyy -> the template's dd-Mon-yyyy
        d, m, y = (int(x) for x in text.split("-"))
        return date(y, m, d).strftime("%d-%b-%Y")

    sheet("b2cl", [[inv["inum"], xdate(inv["idt"]), inv["val"], _pos(group["pos"]), "", it["itm_det"]["rt"],
                    it["itm_det"]["txval"], 0, ""]
                   for group in p.get("b2cl", []) for inv in group["inv"] for it in inv["itms"]])
    sheet("b2cs", [["OE", _pos(r["pos"]), "", r["rt"], r["txval"], 0, ""] for r in p.get("b2cs", [])])
    sheet("cdnur", [["B2CL", n["nt_num"], xdate(n["nt_dt"]), "C", _pos(n["pos"]), n["val"], "", it["itm_det"]["rt"],
                     it["itm_det"]["txval"], 0] for n in p.get("cdnur", []) for it in n["itms"]])
    sheet("hsn(b2c)", [[h["hsn_sc"], h["desc"], h["uqc"], h["qty"],
                        round(h["txval"] + h["iamt"] + h["camt"] + h["samt"], 2), h["rt"], h["txval"], h["iamt"],
                        h["camt"], h["samt"], 0] for h in p.get("hsn", {}).get("hsn_b2c", [])])
    labels = {FORMAT["doc_invoices"][0]: FORMAT["doc_invoices"][1],
              FORMAT["doc_credit_notes"][0]: FORMAT["doc_credit_notes"][1]}
    sheet("docs", [[labels.get(d["doc_num"], str(d["doc_num"])), e["from"], e["to"], e["totnum"], e["cancel"]]
                   for d in p.get("doc_issue", {}).get("doc_det", []) for e in d["docs"]])
    notes = wb.create_sheet("read me")
    for line in [f"GSTR-1 (B2C) for {export.fp}, GSTIN {p['gstin']} - SANDBOX DEMONSTRATION, not for filing",
                 f"Period included: {export.start:%d-%m-%Y} to {export.end:%d-%m-%Y}"] + export.warnings:
        notes.append([line])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


KINDS = {
    "json": "GSTR-1 JSON (upload to the GST portal / offline tool)",
    "xlsx": "GSTR-1 Excel (the offline tool's template sheets)",
    "invoice_register": "Invoice Register (Excel, invoice by invoice)",
    "credit_note_register": "Credit Note Register (Excel, with the original invoice)",
    "b2b_register": "B2B E-Invoice Register (Excel, to verify GSTR-1 auto-population)",
    "all": "All: GSTR-1 JSON, Invoice Register, Credit Note Register and B2B E-Invoice Register",
}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _month_files(y: int, m: int, start: date, end: date, kind: str) -> tuple[list[tuple[str, bytes]], str]:
    """The files of one month for ``kind`` and the month's warnings."""
    from . import registers

    e = build_month(y, m, start=start, end=end)
    files = []
    if kind in ("json", "all"):
        files.append((e.filename("json"), to_json(e)))
    if kind == "xlsx":
        files.append((e.filename("xlsx"), to_excel(e)))
    if kind in ("invoice_register", "all"):
        reg = registers.invoice_register(y, m, start=start, end=end)
        files.append((registers.filename(reg), registers.to_excel(reg)))
    if kind in ("credit_note_register", "all"):
        reg = registers.credit_note_register(y, m, start=start, end=end)
        files.append((registers.filename(reg), registers.to_excel(reg)))
    if kind in ("b2b_register", "all"):
        reg = registers.b2b_register(y, m, start=start, end=end)
        files.append((registers.filename(reg), registers.to_excel(reg)))
    return files, f"{e.fp}: " + ("; ".join(e.warnings) or "no warnings")


def export(start: date, end: date, kind: str) -> tuple[str, bytes, str]:
    """(file name, content, content type) for [start, end]: one file per month and kind; a zip when there are several
    (several months, or ``all``: GSTR-1 JSON + Invoice Register + Credit Note Register for each month)."""
    if kind not in KINDS:
        raise ValueError("Choose one of: " + ", ".join(KINDS) + ".")
    months = months_in(start, end)
    if len(months) > 24:
        raise ValueError("Choose at most 24 months.")
    files, notes = [], []
    for y, m in months:
        f, note = _month_files(y, m, start, end, kind)
        files.extend(f)
        notes.append(note)
    if len(files) == 1:
        name, data = files[0]
        return name, data, "application/json" if name.endswith(".json") else XLSX
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            zf.writestr(name, data)
        zf.writestr("READ-ME.txt", "One file of each kind per month (SRIC files GSTR-1 monthly). GSTR-1 JSON and the Invoice / "
                    "Credit Note Registers: B2C documents (buyers without a GSTIN). B2B E-Invoice Register: "
                    "e-invoices, to verify GSTR-1 auto-population. SANDBOX DEMONSTRATION.\n\n" + "\n".join(notes))
    gstin = _supplier_gstin()
    label = {"all": "GSTR1-and-registers", "json": "GSTR1", "xlsx": "GSTR1",
             "invoice_register": "InvoiceRegister", "credit_note_register": "CreditNoteRegister",
             "b2b_register": "B2B-EInvoiceRegister"}[kind]
    first, last = f"{months[0][1]:02d}{months[0][0]}", f"{months[-1][1]:02d}{months[-1][0]}"
    period = first if first == last else f"{first}-to-{last}"
    return f"{label}-B2C-{gstin}-{period}-sandbox.zip", buf.getvalue(), "application/zip"


def _supplier_gstin() -> str:
    from .models import EinvoiceSettings

    return EinvoiceSettings.get().supplier_gstin


def preview(start: date, end: date) -> list[dict]:
    """What the files would contain, month by month (for the screens)."""
    out = []
    for y, m in months_in(start, end)[:24]:
        e = build_month(y, m, start=start, end=end)
        out.append({"fp": e.fp, "from": e.start.isoformat(), "to": e.end.isoformat(), "summary": e.summary,
                    "warnings": e.warnings, "payload": e.payload})
    return out
