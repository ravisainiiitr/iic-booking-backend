"""The tax invoice PDF, in the look of the portal's payment receipt (``payments.receipts`` template: brand and accent
colours, crest, emblem watermark).

* B2B: "TAX INVOICE (e-Invoice)" with the IRN, acknowledgement number and date, and the signed QR code from the
  e-invoice system. The IRP's digital signature authenticates it: no signature box.
* B2C: "TAX INVOICE" with the signatory box at ``dsc.SIGNATURE_BOX``, where the signer utility places the DSC signature.
* Every invoice made by this module is a SANDBOX document and says so on the page (banner and diagonal mark).

The PDF is rendered once, when the invoice is final, and stored with its SHA-256; it is never re-rendered, so the
file the signatory signs is byte for byte the one the portal checks the signature against.
"""

from __future__ import annotations

import io
import os
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from . import dsc
from .gstin import STATE_CODES
from .tax import in_words

SANDBOX_MARK = "SANDBOX DEMONSTRATION - NOT A VALID TAX INVOICE"


def _look() -> dict:
    try:
        from iic_booking.payments.receipts import template_values

        t = template_values()
    except Exception:  # noqa: BLE001 - receipt template table missing in a bare database
        from iic_booking.payments.receipts import template_defaults

        t = template_defaults()
    return {
        "brand": t.get("brand_color") or "#153F79",
        "accent": t.get("accent_color") or "#C29938",
        "website": t.get("website") or "equip.iitr.ac.in",
        "service": t.get("service_name") or "IIT Roorkee Equipment Booking Portal",
        "address": t.get("address") or "Roorkee - 247667, Uttarakhand, India",
        "show_logo": t.get("show_logo", True),
        "show_watermark": t.get("show_watermark", True),
    }


def _asset(name: str) -> str | None:
    base = getattr(settings, "BASE_DIR", None)
    path = os.path.join(str(base), "fonts", name) if base else ""
    return path if path and os.path.isfile(path) else None


def _fonts() -> tuple[str, str, bool]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if "EINoto" in pdfmetrics.getRegisteredFontNames():
        return "EINoto", "EINoto-Bold", True
    reg, bold = _asset("NotoSans-Regular.ttf"), _asset("NotoSans-Bold.ttf")
    if reg and bold:
        try:
            pdfmetrics.registerFont(TTFont("EINoto", reg))
            pdfmetrics.registerFont(TTFont("EINoto-Bold", bold))
            return "EINoto", "EINoto-Bold", True
        except Exception:  # noqa: BLE001
            pass
    return "Helvetica", "Helvetica-Bold", False


def _faded(path: str, strength: float = 0.07):
    """The crest blended into white, for the watermark drawn behind the text."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    img = Image.open(path).convert("RGBA")
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    alpha = img.split()[-1].point(lambda a: int(a * strength))
    white.paste(img, mask=alpha)
    return ImageReader(white.convert("RGB"))


def _qr(text: str):
    import qrcode
    from reportlab.lib.utils import ImageReader

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=4, border=1)
    qr.add_data(text)
    qr.make(fit=True)
    return ImageReader(qr.make_image(fill_color="black", back_color="white").convert("RGB"))


def _money(v) -> str:
    value = Decimal(str(v or 0)).quantize(Decimal("0.01"))
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    if len(whole) > 3:  # Indian grouping 12,34,567
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{whole}.{frac}"


def _state(code: str) -> str:
    return f"{STATE_CODES.get(code, '-')} ({code})" if code else "-"


def render(invoice) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import Paragraph, Table, TableStyle

    from .models import EinvoiceSettings, SupplyType

    s = EinvoiceSettings.get()
    look = _look()
    sans, bold, has_rupee = _fonts()
    R = "₹" if has_rupee else "Rs."
    W, H = A4
    M = 40
    CW = W - 2 * M
    navy, gold = colors.HexColor(look["brand"]), colors.HexColor(look["accent"])
    ink, muted, rule = colors.HexColor("#1E293B"), colors.HexColor("#5B6573"), colors.HexColor("#D6DBE4")
    tint, orange = colors.HexColor("#F3F6FB"), colors.HexColor("#C2410C")
    b2b = invoice.supply_type == SupplyType.B2B
    c = invoice.customer

    buf = io.BytesIO()
    cv = rl_canvas.Canvas(buf, pagesize=A4, invariant=1)
    cv.setTitle(f"Tax Invoice {invoice.display_number}")
    cv.setAuthor(s.supplier_legal_name)
    cv.setSubject("GST tax invoice (sandbox demonstration)")
    cv.setCreator(look["service"])

    def st(size=8.6, font=sans, color=ink, leading=None, align=0):
        return ParagraphStyle("x", fontName=font, fontSize=size, leading=leading or size * 1.3, textColor=color,
                              alignment=align)

    def text(x, y, value, size=9, font=sans, color=ink):
        cv.setFont(font, size)
        cv.setFillColor(color)
        cv.drawString(x, y, value)

    def right(x, y, value, size=9, font=sans, color=ink):
        cv.setFont(font, size)
        cv.setFillColor(color)
        cv.drawRightString(x, y, value)

    crest = _asset("IITR_Logo.png") if (look["show_logo"] or look["show_watermark"]) else None
    # Watermark first, so everything else is drawn over it.
    if crest and look["show_watermark"]:
        size = 330
        cv.drawImage(_faded(crest), (W - size) / 2, (H - size) / 2 - 20, size, size, mask=None)
    cv.saveState()
    cv.setFillColor(orange)
    cv.setFillAlpha(0.08)
    cv.translate(W / 2, H / 2)
    cv.rotate(35)
    cv.setFont(bold, 30)
    cv.drawCentredString(0, 0, "SANDBOX - NOT A VALID INVOICE")
    cv.restoreState()

    # Bands and sandbox banner.
    cv.setFillColor(navy)
    cv.rect(0, H - 9, W, 9, stroke=0, fill=1)
    cv.setFillColor(gold)
    cv.rect(0, H - 11.5, W, 2.5, stroke=0, fill=1)
    y = H - 30
    cv.setFillColor(colors.HexColor("#FFF4E5"))
    cv.setStrokeColor(orange)
    cv.setLineWidth(0.8)
    cv.rect(M, y - 16, CW, 16, stroke=1, fill=1)
    cv.setFont(bold, 8.2)
    cv.setFillColor(orange)
    cv.drawCentredString(W / 2, y - 11, SANDBOX_MARK)
    y -= 26

    # Header: crest, supplier; title on the right.
    logo = 58
    if crest and look["show_logo"]:
        cv.drawImage(crest, M, y - logo, logo, logo, mask="auto")
    hx = M + (logo + 12 if crest and look["show_logo"] else 0)
    text(hx, y - 15, s.supplier_legal_name.upper(), 13.5, bold, navy)
    text(hx, y - 29, s.supplier_office, 8.8, bold)
    text(hx, y - 41, look["address"], 8, sans, muted)
    text(hx, y - 52, f"GSTIN: {s.supplier_gstin}   |   PAN: {s.supplier_pan}   |   State: {_state(s.supplier_state_code)}",
         7.8, sans, muted)
    y -= logo + 10
    cv.setStrokeColor(gold)
    cv.setLineWidth(1.2)
    cv.line(M, y, W - M, y)
    y -= 20
    text(M, y, "TAX INVOICE" + ("  (e-Invoice)" if b2b else ""), 15, bold, navy)
    right(W - M, y, f"Invoice No. {invoice.display_number}", 10.5, bold, ink)
    y -= 13
    text(M, y, "Original for recipient", 7.8, sans, muted)
    issued = invoice.invoice_date.strftime("%d %b %Y") if invoice.invoice_date else "-"
    right(W - M, y, f"Invoice date: {issued}", 8.6, sans, ink)
    y -= 12

    # B2B: IRN block with the QR code.
    if b2b and invoice.irn:
        box_h = 86
        cv.setFillColor(tint)
        cv.setStrokeColor(rule)
        cv.roundRect(M, y - box_h, CW, box_h, 4, stroke=1, fill=1)
        qr_size = 78
        if invoice.signed_qr:
            cv.drawImage(_qr(invoice.signed_qr), W - M - qr_size - 4, y - box_h + 4, qr_size, qr_size)
        p = Paragraph(f"<b>IRN</b>&nbsp; {invoice.irn}", st(8.2, leading=10.5))
        p.wrapOn(cv, CW - qr_size - 24, 40)
        p.drawOn(cv, M + 8, y - 30)
        ack = timezone.localtime(invoice.ack_date).strftime("%d %b %Y %H:%M") if invoice.ack_date else "-"
        text(M + 8, y - 46, f"Ack No.: {invoice.ack_number}      Ack Date: {ack}", 8.2)
        note = ("Simulated sandbox values (mock): not issued by the GST e-invoice system." if invoice.simulated
                else "Registered on the NIC e-invoice SANDBOX: not a real IRN.")
        text(M + 8, y - 60, note, 7.6, bold, orange)
        text(M + 8, y - 74, "Scan the QR code to read the signed invoice data.", 7.4, sans, muted)
        y -= box_h + 10

    # Buyer and invoice details.
    col = (CW - 12) / 2
    buyer = [f"<b>{c.legal_name}</b>"]
    if c.trade_name:
        buyer.append(c.trade_name)
    buyer += [x for x in (c.address1, c.address2, f"{c.location} - {c.pincode}") if x]
    buyer.append(f"GSTIN: <b>{c.gstin}</b>" if c.registered else "GSTIN: Unregistered (no GSTIN)")
    buyer.append(f"State: {_state(c.state_code)}")
    if c.email:
        buyer.append(c.email)
    details = [("Place of supply", _state(invoice.place_of_supply)),
               ("Supply", "Inter-state (IGST)" if invoice.inter_state else "Intra-state (CGST + SGST)"),
               ("Reverse charge", "No")]
    for label, value in (("Booking", invoice.booking_reference), ("FBR No.", invoice.fbr_number),
                         ("Equipment", invoice.equipment_name), ("Facility", invoice.department_name),
                         ("Payment receipt", invoice.receipt_number + (
                             f" ({invoice.receipt_date.strftime('%d %b %Y')})" if invoice.receipt_date else "")
                          if invoice.receipt_number else ""),
                         ("Buyer's PO", invoice.po_number)):
        if value:
            details.append((label, value))
    left = [Paragraph("BILL TO", st(7, bold, muted))] + [Paragraph(line, st(8.4)) for line in buyer]
    right_rows = [[Paragraph(k, st(7.8, color=muted)), Paragraph(v, st(8.2))] for k, v in details]
    rt = Table(right_rows, colWidths=[78, col - 78])
    rt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                            ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    lt = Table([[p] for p in left], colWidths=[col])
    lt.setStyle(TableStyle([("BOTTOMPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), 1),
                            ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    both = Table([[lt, rt]], colWidths=[col + 12, col])
    both.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    _, bh = both.wrapOn(cv, CW, 300)
    both.drawOn(cv, M, y - bh)
    y -= bh + 10

    # Lines.
    inter = invoice.inter_state
    head = ["#", "Description", "SAC", "Qty", f"Taxable ({R})", "Rate"] + (
        ["IGST"] if inter else ["CGST", "SGST"]) + [f"Total ({R})"]
    hs, bs, bsr = st(7.4, bold, colors.white), st(7.8), st(7.8, align=2)
    rows = [[Paragraph(h, hs) for h in head]]
    for line in invoice.lines.all():
        desc = f"{line.description}" + (f"<br/><font size=6.8 color='#5B6573'>{line.detail}</font>" if line.detail else "")
        qty = f"{line.quantity.normalize():f}"
        taxes = [_money(line.igst)] if inter else [_money(line.cgst), _money(line.sgst)]
        rows.append([Paragraph(str(line.sl_no), bs), Paragraph(desc, bs), Paragraph(line.sac_code, bs),
                     Paragraph(qty, bsr), Paragraph(_money(line.taxable_value), bsr),
                     Paragraph(f"{line.gst_rate.normalize():f}%", bsr)] + [Paragraph(t, bsr) for t in taxes]
                    + [Paragraph(_money(line.total), bsr)])
    widths = [18, 0, 42, 28, 64, 34] + ([56] if inter else [50, 50]) + [64]
    widths[1] = CW - sum(widths)
    lines_table = Table(rows, colWidths=widths, repeatRows=1)
    lines_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), navy), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, rule), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    _, th = lines_table.wrapOn(cv, CW, 400)
    lines_table.drawOn(cv, M, y - th)
    y -= th + 8

    # Totals on the right, words on the left.
    half = (invoice.lines.first().gst_rate / 2).normalize() if invoice.lines.exists() else Decimal("0")
    total_rows = [("Taxable value", invoice.taxable_value)]
    if inter:
        total_rows.append((f"IGST @ {(half * 2).normalize():f}%", invoice.igst))
    else:
        total_rows += [(f"CGST @ {half:f}%", invoice.cgst), (f"SGST @ {half:f}%", invoice.sgst)]
    if invoice.round_off:
        total_rows.append(("Round off", invoice.round_off))
    tx = W - M - 200
    ty = y
    for label, value in total_rows:
        text(tx, ty - 10, label, 8.4, sans, muted)
        right(W - M, ty - 10, _money(value), 8.4)
        ty -= 13
    cv.setFillColor(tint)
    cv.rect(tx - 6, ty - 18, 206, 18, stroke=0, fill=1)
    text(tx, ty - 12.5, "Invoice total", 9.2, bold, navy)
    right(W - M, ty - 12.5, f"{R} {_money(invoice.total)}", 9.6, bold, navy)
    ty -= 24
    words = Paragraph(f"<b>Amount in words:</b> {in_words(invoice.total)}<br/><b>Tax in words:</b> "
                      f"{in_words(invoice.cgst + invoice.sgst + invoice.igst)}", st(7.8, leading=10.5))
    words.wrapOn(cv, tx - M - 16, 80)
    words.drawOn(cv, M, y - words.height - 2)
    y = min(ty, y - words.height - 6) - 6

    # Paid note and terms.
    paid = ("Paid in full" + (f" - payment receipt {invoice.receipt_number}" if invoice.receipt_number else "")
            + ". No amount is due against this invoice.")
    notes = [f"<b>{paid}</b>"] + [line for line in s.terms.splitlines() if line.strip()]
    if invoice.notes and not invoice.notes.strip().startswith("#"):
        notes.append(invoice.notes.strip()[:300])
    terms = Paragraph("<br/>".join(notes), st(7.4, leading=9.6, color=muted))
    terms.wrapOn(cv, CW, 120)
    top_of_footer = dsc.SIGNATURE_BOX[3] + 20
    terms_y = max(y - terms.height, top_of_footer)
    terms.drawOn(cv, M, terms_y)

    # Signature area (fixed position, so the signer utility knows where to put the DSC signature).
    x1, y1, x2, y2 = dsc.SIGNATURE_BOX
    text(x1, y2 + 6, f"For {s.supplier_trade_name}", 8.4, bold, ink)
    if b2b:
        p = Paragraph("Authenticated by the GST e-invoice system (IRN and signed QR code). "
                      "No signature is required on an e-invoice.", st(7.6, color=muted, leading=9.8))
        p.wrapOn(cv, x2 - x1, 60)
        p.drawOn(cv, x1, y2 - p.height - 4)
    else:
        cv.setStrokeColor(navy)
        cv.setDash(3, 2)
        cv.setLineWidth(0.7)
        cv.rect(x1, y1, x2 - x1, y2 - y1, stroke=1, fill=0)
        cv.setDash()
        # The box stays empty: the signer utility draws the visible DSC signature inside it.
        text(x1, y1 - 11, "Authorised Signatory", 8.2, bold)
        right(x2, y1 - 11, "digital signature (DSC)", 7, sans, muted)
    p = Paragraph(
        "Prepared by the " + look["service"] + " under SRIC's GSTIN, in the portal's invoice series. "
        + ("Verify the signature in any PDF reader (Adobe Reader: Signature panel)." if not b2b else
           "Verify the IRN on the GST e-invoice portal."), st(7, color=muted, leading=9))
    p.wrapOn(cv, x1 - M - 20, 60)
    p.drawOn(cv, M, y1)

    # Footer.
    cv.setStrokeColor(rule)
    cv.setLineWidth(0.5)
    cv.line(M, 54, W - M, 54)
    text(M, 42, f"{look['service']}  |  {look['website']}  |  {s.supplier_email}", 7.2, sans, muted)
    right(W - M, 42, SANDBOX_MARK, 7.2, bold, orange)
    cv.setFillColor(navy)
    cv.rect(0, 0, W, 6, stroke=0, fill=1)
    cv.showPage()
    cv.save()
    return buf.getvalue()
