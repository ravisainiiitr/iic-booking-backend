"""PDFs for external-user services, built only from server-side records.

* Provisional (pro forma) invoice: always titled "Provisional Invoice / Performa Invoice — Not a GST Tax Invoice";
  amounts come from the booking (or, before allocation, the special-request estimate, marked as an estimate).
* Envelope label for a physical invoice copy and sample shipment label: a 10 x 4.5 inch page (720 x 324 pt, the
  envelope's label area), 0.25 inch margins, text shrunk to fit instead of clipped. Print at 100% / Actual size.
  The IIC address comes from the Main Administrator's setting (no address is made up when it is empty); the logo is
  the repository's existing crest masthead.
* The sample label has no QR code (31 Oct 2026): the office records the receipt by the booking ID.
"""

from __future__ import annotations

import io
import re
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


_PHONE_PREFIX = re.compile(r"^\s*(mobile|mob\.?|phone|ph\.?|tel\.?|telephone|contact)\b\s*(no\.?|number)?\s*[:\-]?\s*", re.I)


def split_phone_lines(lines: list[str]) -> tuple[list[str], str]:
    """Address lines without "Mobile: ..."/"Phone: ..." rows, and the first such number (the label prints it apart)."""
    kept, phone = [], ""
    for line in lines:
        m = _PHONE_PREFIX.match(line)
        if m and line[m.end():].strip():
            phone = phone or line[m.end():].strip()
        else:
            kept.append(line)
    return kept, phone


LABEL_NAVY = (0x1B / 255, 0x2F / 255, 0x5E / 255)
LABEL_BRICK = (0xA1 / 255, 0x3A / 255, 0x24 / 255)


def _address_box(c, x, y_top, width, height, tag, lines, mobile, accent, *, max_size, min_size):
    """Boxed address with a coloured bar on the left: TAG, the lines (first bold) and the mobile number."""
    from reportlab.lib import colors

    bar, pad = 6.0, 8.0
    c.setLineWidth(1.5)
    c.setStrokeColor(colors.black)
    c.rect(x, y_top - height, width, height, stroke=1, fill=0)
    c.setFillColorRGB(*accent)
    c.rect(x, y_top - height, bar, height, stroke=0, fill=1)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x + bar + pad, y_top - 13, tag)
    c.setFillColor(colors.black)
    text_x, text_w = x + bar + pad, width - bar - 2 * pad
    rows = list(lines) + [f"Mobile: {mobile or 'not given'}"]
    size, wrapped = _fit_lines(c, rows, font="Helvetica", max_size=max_size, min_size=min_size, width=text_w,
                               height=height - 22)
    first_len = len(_fit_lines(c, rows[:1], font="Helvetica", max_size=size, min_size=size, width=text_w,
                               height=10_000)[1]) if lines else 0
    y = y_top - 18 - size
    for i, line in enumerate(wrapped):
        if i == len(wrapped) - 1 and line.startswith("Mobile:"):
            c.setFont("Helvetica-Bold", size)
            c.drawString(text_x, y, "Mobile:")
            c.setFont("Helvetica", size)
            c.drawString(text_x + c.stringWidth("Mobile: ", "Helvetica-Bold", size), y, line[len("Mobile:"):].strip())
        else:
            c.setFont("Helvetica-Bold" if i < first_len else "Helvetica", size)
            c.drawString(text_x, y, line)
        y -= size * 1.2


def sample_label_pdf(*, sender_lines: list[str], lab_name: str, lab_location: str = "", equipment_name: str,
                     equipment_code: str = "", booking_reference: str, reference: str, sample_count: int,
                     attention: Optional[str] = None, lab_incharge: Optional[str] = None, sender_mobile: str = "",
                     office_mobile: str = "") -> bytes:
    """Sample shipment label, laid out for sorting by hand at the receiving office (no QR code, 31 Oct 2026).

    Top: the IIT Roorkee masthead and the BOOKING ID, the largest text on the label, which staff use to record the
    receipt. Left: a TO box (IIC address, Officer In Charge, lab and equipment, the department office mobile) and a
    FROM box (the sender with their mobile number). Right: the booking ID again, the label reference and the number of
    samples. ``office_mobile`` is the mobile of the department's Accounts In Charge; without one, a "Mobile:" line of
    the configured IIC address is used.
    """
    from reportlab.lib import colors

    iic_lines, address_mobile = split_phone_lines(iic_address_lines())
    office_mobile = office_mobile or address_mobile
    buffer, c = _label_canvas()
    inner_w = LABEL_WIDTH_PT - 2 * MARGIN_PT
    top = LABEL_HEIGHT_PT - MARGIN_PT
    right_edge = LABEL_WIDTH_PT - MARGIN_PT

    # Header: masthead left, booking ID right, dashed rule below
    header_h = 44.0
    _logo(c, MARGIN_PT, top, inner_w * 0.32, header_h - 4)
    id_w = inner_w * 0.62
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(right_edge, top - 9, "BOOKING ID")
    id_size = _fit_bold(c, booking_reference, id_w, 28, 12)
    c.setFont("Helvetica-Bold", id_size)
    c.drawRightString(right_edge, top - 13 - id_size, _one_line(c, booking_reference, id_w, "Helvetica-Bold", id_size))
    c.setLineWidth(1.2)
    c.setDash(4, 3)
    c.line(MARGIN_PT, top - header_h - 4, right_edge, top - header_h - 4)
    c.setDash()

    # Body: TO and FROM boxes left, booking panel right
    body_top = top - header_h - 12
    body_h = body_top - MARGIN_PT
    right_w = 150.0
    left_w = inner_w - right_w - 14
    gap = 8.0
    to_h = body_h * 0.62
    to_lines = list(iic_lines)
    attn = f"Attn: {attention} (Officer In Charge)" if attention else "Attn: Officer In Charge"
    to_lines.append(f"{attn}, {lab_name}" if lab_name else attn)
    eq = f"Equipment: {equipment_name}" + (f" ({equipment_code})" if equipment_code else "")
    if lab_location:
        eq += f"  ·  {lab_location}"
    to_lines.append(eq)
    if lab_incharge:
        to_lines.append(f"Lab in-charge: {lab_incharge}")
    _address_box(c, MARGIN_PT, body_top, left_w, to_h, "TO", to_lines, office_mobile, LABEL_NAVY,
                 max_size=11, min_size=6.5)
    # Name on its own line, the rest of the sender address as one wrapped line so it prints large enough to read
    from_lines = sender_lines[:1] + ([", ".join(sender_lines[1:])] if len(sender_lines) > 1 else [])
    _address_box(c, MARGIN_PT, body_top - to_h - gap, left_w, body_h - to_h - gap, "FROM", from_lines,
                 sender_mobile, LABEL_BRICK, max_size=11, min_size=6)

    # Booking panel: the booking ID in a heavy box (what the office writes in the register), reference, count
    px = right_edge - right_w
    c.setStrokeColor(colors.black)
    c.setLineWidth(2.5)
    box_h = 92.0
    c.rect(px, body_top - box_h, right_w, box_h, stroke=1, fill=0)
    cx = px + right_w / 2
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(cx, body_top - 16, "BOOKING ID")
    size = _fit_bold(c, booking_reference, right_w - 14, 22, 9)
    c.setFont("Helvetica-Bold", size)
    c.drawCentredString(cx, body_top - 22 - size, _one_line(c, booking_reference, right_w - 14, "Helvetica-Bold", size))
    c.setFont("Helvetica", 9)
    c.drawCentredString(cx, body_top - box_h + 12, f"Label {reference}")
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(cx, body_top - box_h - 24, f"{sample_count} sample" + ("" if sample_count == 1 else "s"))
    c.setFont("Helvetica", 7.5)
    notes = ("Write the booking ID on every sample.", "Enclose the signed requisition form.",
             "Staff: record the receipt against", "this booking ID on the portal.")
    for i, text in enumerate(notes):
        c.drawCentredString(cx, body_top - box_h - 42 - i * 10, text)
    c.showPage()
    c.save()
    return buffer.getvalue()


def department_office_mobile(department) -> str:
    """Mobile of the Accounts In Charge of ``department`` (the label's TO contact; the portal serves many
    departments, not only IIC). Empty when none has a number."""
    if department is None:
        return ""
    from django.contrib.auth import get_user_model

    from iic_booking.users.models.user_type import UserType

    User = get_user_model()
    rows = (User.objects.filter(user_type=UserType.FINANCE, is_active=True, department=department)
            .order_by("pk").values_list("phone_number", "secondary_phone_number"))
    for phone, alt in rows:
        number = (phone or alt or "").strip()
        if number:
            return number
    return ""


def address_lines(snapshot: dict, *, phone: bool = True) -> list[str]:
    parts = [snapshot.get("recipient_name", ""), snapshot.get("organisation", ""), snapshot.get("line1", ""),
             snapshot.get("line2", "")]
    city_line = ", ".join(x for x in [snapshot.get("city", ""), snapshot.get("district", "")] if x)
    state_line = " - ".join(x for x in [snapshot.get("state", ""), snapshot.get("postal_code", "")] if x)
    parts += [city_line, state_line, snapshot.get("country", "")]
    if phone and snapshot.get("phone"):
        parts.append(f"Phone: {snapshot['phone']}")
    return [p for p in parts if p]


def generated_at() -> datetime:
    return timezone.localtime()
