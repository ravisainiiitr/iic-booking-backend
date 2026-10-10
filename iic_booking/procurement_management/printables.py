"""Printable PDFs: the asset register laid out like the physical book (grouped by page) and QR asset labels.

The QR code encodes ``<FRONTEND_URL>/procurement/scan/<asset tag>`` so a phone camera opens the verification page.
"""

from __future__ import annotations

import io
import re
from urllib.parse import quote

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone


def scan_url(tag: str) -> str:
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}/procurement/scan/{quote(tag, safe='')}"


def _esc(v) -> str:
    return str("" if v is None else v).replace("&", "&amp;").replace("<", "&lt;").replace("₹", "Rs. ")


def _pdf_response(content: bytes, base: str) -> HttpResponse:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("_") or "document"
    resp = HttpResponse(content, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{safe}_{timezone.localdate():%Y%m%d}.pdf"'
    return resp


def register_pdf(reg, assets) -> HttpResponse:
    """GFR-style register print: one block per register page, entries in serial order."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from .serializers import user_brief

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=20, rightMargin=20, topMargin=22, bottomMargin=22,
                            title=f"{reg.code} {reg.name}")
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=7, leading=8.5)
    head = styles["BodyText"].clone("head", fontSize=7, leading=8.5, alignment=1)
    story = [
        Paragraph("Indian Institute of Technology Roorkee", styles["Title"]),
        Paragraph(_esc(f"{reg.department.name} — {reg.get_register_type_display()}"), styles["Heading2"]),
        Paragraph(_esc(f"Register {reg.code}: {reg.name}" + (f" (Vol. {reg.volume})" if reg.volume else "")), styles["Heading3"]),
        Paragraph(f"Printed {timezone.localtime():%d %b %Y %H:%M}. Entries are reproduced from the booking system; "
                  "verify against the physical register.", small),
        Spacer(1, 6),
    ]
    headers = ["Sl. No.", "Date of entry", "Description of article", "Make / Model / Sr. No.", "Qty", "Supplier",
               "PO / Invoice no. & date", "Cost (Rs.)", "Location / Custodian", "Asset tag", "Last verified", "Status / Remarks"]
    widths = [34, 48, 120, 92, 26, 74, 92, 52, 80, 70, 52, 70]
    by_page: dict[int | None, list] = {}
    for a in assets:
        by_page.setdefault(a.register_page, []).append(a)

    def serial_key(a):
        s = a.register_serial or ""
        return (0, int(s), "") if s.isdigit() else (1, 0, s)

    total_cost = 0
    for page in sorted(by_page, key=lambda p: (p is None, p or 0)):
        rows = sorted(by_page[page], key=serial_key)
        story.append(Paragraph(f"<b>Page {page if page is not None else '—'}</b>", styles["BodyText"]))
        data = [[Paragraph(f"<b>{_esc(h)}</b>", head) for h in headers]]
        for a in rows:
            total_cost += a.cost or 0
            mms = " / ".join(x for x in (a.make, a.model_number, a.serial_number) if x)
            po = "; ".join(
                x for x in (
                    " ".join(y for y in (a.po_number, a.po_date.strftime("%d-%m-%Y") if a.po_date else "") if y),
                    " ".join(y for y in (a.invoice_number, a.invoice_date.strftime("%d-%m-%Y") if a.invoice_date else "") if y),
                ) if x
            )
            where = " / ".join(x for x in (
                a.location, a.laboratory.name if a.laboratory_id else "",
                user_brief(a.custodian)["name"] if a.custodian_id else "",
            ) if x)
            verified = ""
            if a.last_verified_on:
                verified = f"{a.last_verified_on:%d-%m-%Y} {a.get_last_verification_result_display()}"
            data.append([Paragraph(_esc(v), small) for v in (
                a.register_serial, a.register_entry_date.strftime("%d-%m-%Y") if a.register_entry_date else "",
                a.description + (f" (accessory of {a.parent.asset_tag or a.parent.number})" if a.parent_id else ""),
                mms, a.quantity, a.supplier_name or (a.vendor.name if a.vendor_id else ""), po, a.cost,
                where, a.asset_tag, verified, " — ".join(x for x in (a.get_status_display(), a.remarks[:120]) if x),
            )])
        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ]))
        story += [table, Spacer(1, 8)]
    if not by_page:
        story.append(Paragraph("No entries.", styles["BodyText"]))
    story += [
        Spacer(1, 8),
        Paragraph(f"<b>Entries:</b> {sum(len(v) for v in by_page.values())} &nbsp;&nbsp; <b>Total cost:</b> Rs. {total_cost}", small),
        Spacer(1, 26),
        Paragraph("Store In Charge ____________________ &nbsp;&nbsp;&nbsp; Verified by ____________________ "
                  "&nbsp;&nbsp;&nbsp; Head of Department ____________________", small),
    ]
    doc.build(story)
    return _pdf_response(buf.getvalue(), f"register_{reg.code}")


def labels_pdf(assets, *, department_name: str = "") -> HttpResponse:
    """A4 sheet of 3 × 8 labels (approx. 70 × 37 mm): QR code + tag + description + register reference."""
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    pdf.setTitle("Asset labels")
    page_w, page_h = A4
    cols, rows = 3, 8
    margin_x, margin_y = 7 * mm, 10 * mm
    cell_w = (page_w - 2 * margin_x) / cols
    cell_h = (page_h - 2 * margin_y) / rows
    qr_size = cell_h - 6 * mm

    def fit(text: str, width: float, font: str, size: float) -> str:
        text = text or ""
        while text and pdf.stringWidth(text, font, size) > width:
            text = text[:-2] + "…" if len(text) > 2 else ""
        return text

    for idx, a in enumerate(assets):
        slot = idx % (cols * rows)
        if idx and slot == 0:
            pdf.showPage()
        col, row = slot % cols, slot // cols
        x = margin_x + col * cell_w
        y = page_h - margin_y - (row + 1) * cell_h
        pdf.setLineWidth(0.3)
        pdf.setDash(1, 2)
        pdf.rect(x + 1 * mm, y + 1 * mm, cell_w - 2 * mm, cell_h - 2 * mm)
        pdf.setDash()
        tag = a.asset_tag or a.number
        widget = QrCodeWidget(scan_url(tag), barLevel="M")
        b = widget.getBounds()
        bw, bh = b[2] - b[0], b[3] - b[1]
        drawing = Drawing(qr_size, qr_size, transform=[qr_size / bw, 0, 0, qr_size / bh, 0, 0])
        drawing.add(widget)
        renderPDF.draw(drawing, pdf, x + 3 * mm, y + 3 * mm)
        tx = x + 3 * mm + qr_size + 2 * mm
        tw = cell_w - (tx - x) - 3 * mm
        top = y + cell_h - 6 * mm
        pdf.setFont("Helvetica", 6)
        pdf.drawString(tx, top, fit(department_name or (a.department.name if a.department_id else ""), tw, "Helvetica", 6))
        pdf.setFont("Helvetica-Bold", 8.5)
        pdf.drawString(tx, top - 4 * mm, fit(tag, tw, "Helvetica-Bold", 8.5))
        pdf.setFont("Helvetica", 6.5)
        desc = a.description or ""
        line1 = fit(desc, tw, "Helvetica", 6.5)
        pdf.drawString(tx, top - 8 * mm, line1)
        rest = desc[len(line1.rstrip("…")):].strip()
        if rest:
            pdf.drawString(tx, top - 11 * mm, fit(rest, tw, "Helvetica", 6.5))
        pdf.setFont("Helvetica", 6)
        if a.register_id:
            pdf.drawString(tx, top - 15 * mm, fit(f"Reg: {a.register_ref}", tw, "Helvetica", 6))
        if a.equipment_id:
            pdf.drawString(tx, top - 18.5 * mm, fit(f"Equip: {a.equipment.name}", tw, "Helvetica", 6))
        pdf.drawString(tx, y + 3.5 * mm, fit(f"Asset {a.number}", tw, "Helvetica", 6))
    if not assets:
        pdf.setFont("Helvetica", 10)
        pdf.drawString(margin_x, page_h - margin_y - 12, "No assets selected.")
    pdf.save()
    return _pdf_response(buf.getvalue(), "asset_labels")
