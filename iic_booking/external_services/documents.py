"""PDFs for external-user services, built only from server-side records.

* Provisional (pro forma) invoice: always titled "Provisional Invoice / Performa Invoice — Not a GST Tax Invoice";
  amounts come from the booking (or, before allocation, the special-request estimate, marked as an estimate).
* Envelope label for a physical invoice copy and sample shipment label: a 10 x 4.5 inch page (720 x 324 pt, the
  envelope's label area), 0.25 inch margins, text shrunk to fit instead of clipped. Print at 100% / Actual size.
  The IIC address comes from the Main Administrator's setting (no address is made up when it is empty); the logo is
  the repository's existing crest masthead.
* The sample label's QR code holds only a link with a random token to a staff-only page; no personal data.
"""

from __future__ import annotations

import io
from datetime import datetime
from decimal import Decimal
from typing import Optional

from django.utils import timezone

PROVISIONAL_TITLE = "Provisional Invoice / Performa Invoice — Not a GST Tax Invoice"
LABEL_WIDTH_PT = 720.0  # 10 in
LABEL_HEIGHT_PT = 324.0  # 4.5 in
MARGIN_PT = 18.0  # 0.25 in


class DocumentError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code

    def payload(self) -> dict:
        return {"error": self.message, "code": self.code}


def iic_address_lines() -> list[str]:
    from .models import ExternalServicesSettings

    lines = [ln for ln in (ExternalServicesSettings.load().iic_postal_address or "").splitlines() if ln.strip()]
    if not lines:
        raise DocumentError(
            "The IIC postal address for labels is not configured yet (Main Administrator settings).",
            code="address_not_configured",
        )
    return lines


# --- provisional invoice ---------------------------------------------------------------------------------------------

def _money(v) -> str:
    try:
        return f"{Decimal(str(v)):,.2f}"
    except Exception:  # noqa: BLE001
        return str(v)


def provisional_invoice_pdf(*, booking=None, special_request=None) -> bytes:
    """Exactly one of ``booking`` / ``special_request`` (estimate before allocation)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from iic_booking.equipment.document_exports import _equipment_department_name, _pdf_letterhead_story_lines
    from iic_booking.users.models import ExternalBillingProfile

    source = booking or special_request
    user = source.user
    equipment = source.equipment
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9, leading=12)
    warn = ParagraphStyle("warn", parent=styles["Normal"], fontSize=10, leading=13, textColor=colors.HexColor("#8a1c1c"))
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=1.6 * cm, rightMargin=1.6 * cm, topMargin=1.4 * cm,
                            bottomMargin=1.4 * cm, title=PROVISIONAL_TITLE)
    story = list(_pdf_letterhead_story_lines(department_name=_equipment_department_name(equipment),
                                             document_title=PROVISIONAL_TITLE))
    story.append(Paragraph(
        "This document is a provisional (pro forma) statement for information only. It is <b>not a GST tax "
        "invoice</b>. The official GST invoice is issued by the SRIC office, IIT Roorkee.", warn))
    story.append(Spacer(1, 8))
    billing = ExternalBillingProfile.objects.filter(user=user).first()
    address = ", ".join(x for x in [
        getattr(billing, "billing_address_line1", ""), getattr(billing, "billing_address_line2", ""),
        getattr(billing, "billing_city", ""), getattr(billing, "billing_state", ""),
        getattr(billing, "billing_pincode", ""), getattr(billing, "billing_country", ""),
    ] if x)
    generated = timezone.localtime().strftime("%d %b %Y %I:%M %p IST")
    if booking is not None:
        reference = getattr(booking, "virtual_booking_id", "") or f"#{booking.pk}"
        breakdown = booking.charge_breakdown or []
        total = booking.total_charge
        due = Decimal(str(getattr(booking, "amount_due", 0) or 0))
        payment_state = "Paid" if booking.payment_settled_at or due <= 0 else f"Amount due: Rs. {_money(due)}"
        basis = "Amount as recorded on the booking"
    else:
        reference = f"Special request #{special_request.pk}"
        breakdown = special_request.estimated_charge_breakdown or []
        total = special_request.estimated_charge
        payment_state = "Not payable yet (request awaiting approval)"
        basis = "Estimate from your inputs; the final amount is set when a slot is allocated"
    meta = [
        ["Reference", reference],
        ["Billed to", getattr(billing, "billing_name", "") or getattr(user, "name", "") or user.email],
        ["GSTIN", getattr(billing, "gstin", "") or "Not provided"],
        ["Address", address or "Not provided"],
        ["Equipment", f"{equipment.name} ({equipment.code})"],
        ["Generated", generated],
        ["Payment status", payment_state],
    ]
    table = Table([[Paragraph(f"<b>{k}</b>", small), Paragraph(str(v), small)] for k, v in meta],
                  colWidths=[3.6 * cm, 13.6 * cm])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [table, Spacer(1, 10)]
    rows = [["Description", "Amount (Rs.)"]]
    for item in breakdown:
        rows.append([Paragraph(str(item.get("description", "")), small), _money(item.get("amount", ""))])
    rows.append([Paragraph("<b>Total</b>", small), _money(total)])
    charges = Table(rows, colWidths=[13.2 * cm, 4.0 * cm])
    charges.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
    ]))
    story += [charges, Spacer(1, 8), Paragraph(f"Basis: {basis}.", small)]
    doc.build(story)
    return buffer.getvalue()


# --- labels ---------------------------------------------------------------------------------------------------------

def _fit_lines(canvas, lines, *, font, max_size, min_size, width, height, leading=1.2):
    """Largest font size (down to ``min_size``) at which ``lines`` wrap into ``width`` x ``height``."""
    from reportlab.lib.utils import simpleSplit

    size = max_size
    while size >= min_size:
        wrapped = [w for line in lines for w in (simpleSplit(line, font, size, width) or [""])]
        if len(wrapped) * size * leading <= height:
            return size, wrapped
        size -= 0.5
    wrapped = [w for line in lines for w in (simpleSplit(line, font, min_size, width) or [""])]
    max_lines = max(1, int(height // (min_size * leading)))
    if len(wrapped) > max_lines:  # never clip silently: end with an ellipsis the user can see
        wrapped = wrapped[:max_lines]
        wrapped[-1] = wrapped[-1][: max(0, len(wrapped[-1]) - 1)] + "…"
    return min_size, wrapped


def _draw_block(c, x, y_top, width, height, title, lines, *, max_size=13, min_size=7, bold_first=True):
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x, y_top - 8, title.upper())
    size, wrapped = _fit_lines(c, lines, font="Helvetica", max_size=max_size, min_size=min_size, width=width,
                               height=height - 14)
    y = y_top - 14 - size
    for i, line in enumerate(wrapped):
        c.setFont("Helvetica-Bold" if (i == 0 and bold_first) else "Helvetica", size)
        c.drawString(x, y, line)
        y -= size * 1.2


def _logo(c, x, y_top, max_w, max_h):
    from reportlab.lib.utils import ImageReader

    from iic_booking.equipment.document_exports import _pdf_masthead_path

    path = _pdf_masthead_path()
    if not path:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y_top - 12, "Indian Institute of Technology Roorkee")
        return
    img = ImageReader(path)
    w, h = img.getSize()
    scale = min(max_w / w, max_h / h)
    c.drawImage(img, x, y_top - h * scale, width=w * scale, height=h * scale, mask="auto")


def _label_canvas():
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(LABEL_WIDTH_PT, LABEL_HEIGHT_PT))
    c.setTitle("IIC label 10 x 4.5 in")
    return buffer, c


def envelope_label_pdf(*, recipient_lines: list[str], reference: str) -> bytes:
    """Official invoice envelope: IIC (sender, with logo) top left, recipient right, reference at the bottom."""
    sender = iic_address_lines()
    buffer, c = _label_canvas()
    inner_w = LABEL_WIDTH_PT - 2 * MARGIN_PT
    top = LABEL_HEIGHT_PT - MARGIN_PT
    _logo(c, MARGIN_PT, top, inner_w * 0.40, 46)
    _draw_block(c, MARGIN_PT, top - 52, inner_w * 0.42, 150, "From", sender, max_size=9, min_size=6)
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(LABEL_WIDTH_PT - MARGIN_PT, top - 12, "BY SPEED POST")
    _draw_block(c, MARGIN_PT + inner_w * 0.46, top - 40, inner_w * 0.54, 210, "To", recipient_lines,
                max_size=15, min_size=8)
    c.setFont("Helvetica", 8)
    c.drawString(MARGIN_PT, MARGIN_PT, f"Ref: {reference}"[:110])
    c.showPage()
    c.save()
    return buffer.getvalue()


def _fit_bold(c, text: str, width: float, max_size: float, min_size: float) -> float:
    """Largest bold size (down to ``min_size``) at which ``text`` fits on one line of ``width``."""
    size = max_size
    while size > min_size and c.stringWidth(text, "Helvetica-Bold", size) > width:
        size -= 0.5
    return size


def _one_line(c, text: str, width: float, font: str, size: float) -> str:
    """``text`` cut with an ellipsis to fit one line (never clipped silently)."""
    if c.stringWidth(text, font, size) <= width:
        return text
    while text and c.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


def sample_label_pdf(*, sender_lines: list[str], lab_name: str, lab_location: str = "", equipment_name: str,
                     equipment_code: str = "", booking_reference: str, reference: str, qr_url: str,
                     sample_count: int, attention: Optional[str] = None, lab_incharge: Optional[str] = None) -> bytes:
    """Sample shipment label, laid out for sorting at the receiving office.

    Left: IIC address (where the courier delivers), then a boxed band with the LAB in large bold, the equipment and
    the person it is addressed to (Officer In Charge, lab in-charge). Right: the QR code (scanned with a phone camera:
    office receipt, then lab receipt) with the short reference and booking ID. Bottom left: the sender, small.
    """
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors

    buffer, c = _label_canvas()
    inner_w = LABEL_WIDTH_PT - 2 * MARGIN_PT
    top = LABEL_HEIGHT_PT - MARGIN_PT
    qr_size = 160.0
    right_w = qr_size + 12
    left_w = inner_w - right_w - 14

    # IIC address (delivery address for the courier)
    _logo(c, MARGIN_PT, top, left_w * 0.55, 34)
    _draw_block(c, MARGIN_PT, top - 40, left_w, 62, "To", iic_address_lines(), max_size=10, min_size=7)

    # Sorting band: lab, equipment, addressed person
    band_top, band_h = top - 106, 104.0
    c.setLineWidth(2)
    c.setStrokeColor(colors.black)
    c.rect(MARGIN_PT, band_top - band_h, left_w, band_h, stroke=1, fill=0)
    pad = 10
    text_w = left_w - 2 * pad
    c.setFont("Helvetica-Bold", 8)
    c.drawString(MARGIN_PT + pad, band_top - 13, "LAB")
    lab_line = lab_name or equipment_name
    lab_size = _fit_bold(c, lab_line, text_w, 26, 13)
    c.setFont("Helvetica-Bold", lab_size)
    c.drawString(MARGIN_PT + pad, band_top - 16 - lab_size, _one_line(c, lab_line, text_w, "Helvetica-Bold", lab_size))
    y = band_top - 22 - lab_size - 12
    eq = f"Equipment: {equipment_name}" + (f" ({equipment_code})" if equipment_code else "")
    if lab_location:
        eq += f"  ·  {lab_location}"
    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGIN_PT + pad, y, _one_line(c, eq, text_w, "Helvetica-Bold", 11))
    y -= 16
    if attention:
        c.setFont("Helvetica-Bold", 13)
        c.drawString(MARGIN_PT + pad, y, _one_line(c, f"Attn: {attention} (Officer In Charge)", text_w,
                                                    "Helvetica-Bold", 13))
        y -= 15
    if lab_incharge:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(MARGIN_PT + pad, y, _one_line(c, f"Lab in-charge: {lab_incharge}", text_w, "Helvetica-Bold", 10))

    # QR, reference and booking ID
    widget = QrCodeWidget(qr_url)
    x0, y0, x1, y1 = widget.getBounds()
    drawing = Drawing(qr_size, qr_size, transform=[qr_size / (x1 - x0), 0, 0, qr_size / (y1 - y0), 0, 0])
    drawing.add(widget)
    qr_x = LABEL_WIDTH_PT - MARGIN_PT - qr_size
    renderPDF.draw(drawing, c, qr_x, top - qr_size)
    cx = qr_x + qr_size / 2
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(cx, top - qr_size - 18, reference)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(cx, top - qr_size - 32, _one_line(c, f"Booking {booking_reference}", right_w,
                                                          "Helvetica-Bold", 9))
    c.setFont("Helvetica", 9)
    c.drawCentredString(cx, top - qr_size - 45, f"{sample_count} sample(s)")
    c.setFont("Helvetica", 7)
    c.drawCentredString(cx, top - qr_size - 58, "Staff: scan with the phone camera")
    c.drawCentredString(cx, top - qr_size - 67, "(or enter the code above)")

    # Sender (small)
    _draw_block(c, MARGIN_PT, MARGIN_PT + 66, left_w, 66, "From", sender_lines, max_size=8.5, min_size=6,
                bold_first=False)
    c.showPage()
    c.save()
    return buffer.getvalue()


def address_lines(snapshot: dict) -> list[str]:
    parts = [snapshot.get("recipient_name", ""), snapshot.get("organisation", ""), snapshot.get("line1", ""),
             snapshot.get("line2", "")]
    city_line = ", ".join(x for x in [snapshot.get("city", ""), snapshot.get("district", "")] if x)
    state_line = " - ".join(x for x in [snapshot.get("state", ""), snapshot.get("postal_code", "")] if x)
    parts += [city_line, state_line, snapshot.get("country", "")]
    if snapshot.get("phone"):
        parts.append(f"Phone: {snapshot['phone']}")
    return [p for p in parts if p]


def generated_at() -> datetime:
    return timezone.localtime()
