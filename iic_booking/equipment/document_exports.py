import io
from decimal import Decimal
from typing import Any, Dict, Tuple

from django.conf import settings


def _safe_str(v: Any) -> str:
    return "" if v is None else str(v)


def _money(v: Any) -> str:
    try:
        return f"{Decimal(str(v)).quantize(Decimal('0.01'))}"
    except Exception:
        return _safe_str(v)


def _equipment_department_name(equipment) -> str:
    """
    Return the internal department name for an equipment, if available.
    Falls back to ORG_DEPARTMENT_NAME or ORG_LEGAL_NAME (legacy IIC wording).
    """
    try:
        dept = getattr(equipment, "internal_department", None)
        name = getattr(dept, "name", None) if dept is not None else None
        name = (str(name).strip() if name is not None else "")
        if name:
            return name
    except Exception:
        pass
    fallback = getattr(settings, "ORG_DEPARTMENT_NAME", "") or getattr(settings, "ORG_LEGAL_NAME", "")
    fallback = (str(fallback).strip() if fallback is not None else "")
    return fallback or "—"


def _pdf_letterhead_story_lines(*, department_name: str) -> list:
    """Return ReportLab flowables for the standard letterhead block."""
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import Paragraph, Spacer
    from reportlab.lib.units import cm

    styles = getSampleStyleSheet()
    dept_style = ParagraphStyle(
        "dept_header",
        parent=styles["Normal"],
        fontSize=12,
        leading=14,
        alignment=TA_CENTER,
        spaceAfter=2,
        fontName="Helvetica-Bold",
    )
    org_style = ParagraphStyle(
        "org_header",
        parent=styles["Normal"],
        fontSize=11,
        leading=13,
        alignment=TA_CENTER,
        spaceAfter=6,
        fontName="Helvetica-Bold",
    )
    dept = _safe_str(department_name).strip() or "—"
    org = getattr(settings, "ORG_PARENT_NAME", "Indian Institute of Technology Roorkee")
    org = _safe_str(org).strip() or "Indian Institute of Technology Roorkee"

    return [
        Paragraph(dept.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), dept_style),
        Paragraph(org.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), org_style),
        Spacer(1, 0.2 * cm),
    ]

# Rupee symbol for PDF (Unicode U+20B9). Use with a font that supports it (e.g. DejaVu Sans).
RUPEES_SYMBOL = "\u20B9"


def _register_pdf_rupee_font():
    """
    Register a Unicode-capable TTF font that includes the Rupee symbol (₹), so PDFs can render it.
    Tries DejaVu Sans from reportlab TTFSearchPath or common system paths. Returns the registered
    font name if successful, else None (caller should use 'Rs.' instead of ₹).
    """
    import os
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab import rl_config
    except ImportError:
        return None
    font_name = "DejaVuSansRupee"
    if font_name in pdfmetrics.getRegisteredFontNames():
        return font_name
    search_paths = []
    try:
        search_paths.extend(getattr(rl_config, "TTFSearchPath", []) or [])
    except Exception:
        pass
    try:
        import reportlab
        reportlab_fonts = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
        if os.path.isdir(reportlab_fonts):
            search_paths.append(reportlab_fonts)
    except Exception:
        pass
    # Project fonts folder and common system paths for DejaVu fonts
    try:
        base_dir = getattr(settings, "BASE_DIR", None)
        if base_dir:
            search_paths.append(os.path.join(base_dir, "fonts"))
    except Exception:
        pass
    for extra in [
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/TTF",
        os.path.join(os.environ.get("WINDIR", ""), "Fonts"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
    ]:
        if extra and os.path.isdir(extra):
            search_paths.append(extra)
    candidates = [
        "DejaVuSans.ttf",
        "DejaVuSans-Regular.ttf",
        "dejavu/DejaVuSans.ttf",
        "segoeui.ttf",  # Windows: has Rupee symbol
        "Segoe UI.ttf",
    ]
    for candidate in candidates:
        for folder in search_paths:
            path = os.path.join(folder, candidate)
            if os.path.isfile(path):
                try:
                    pdfmetrics.registerFont(TTFont(font_name, path))
                    return font_name
                except Exception:
                    pass
    return None


def _pdf_rupees_label():
    """Return the rupee label to use in PDF: ₹ if a supporting font is registered, else 'Rs.'."""
    if _register_pdf_rupee_font():
        return RUPEES_SYMBOL
    return "Rs."


def _amount_in_words(amount_decimal: Decimal) -> str:
    """Convert amount to Indian Rupees in words (e.g. Rupees One Thousand Five Hundred Only)."""
    try:
        amount = Decimal(str(amount_decimal)).quantize(Decimal("0.01"))
    except Exception:
        return "Rupees Zero Only"
    if amount < 0:
        return "Rupees Zero Only"
    units = [
        "",
        "One",
        "Two",
        "Three",
        "Four",
        "Five",
        "Six",
        "Seven",
        "Eight",
        "Nine",
        "Ten",
        "Eleven",
        "Twelve",
        "Thirteen",
        "Fourteen",
        "Fifteen",
        "Sixteen",
        "Seventeen",
        "Eighteen",
        "Nineteen",
    ]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def _up_to_99(n: int) -> str:
        if n < 20:
            return units[n]
        return (tens[n // 10] + (" " + units[n % 10] if n % 10 else "")).strip()

    def _up_to_999(n: int) -> str:
        if n < 100:
            return _up_to_99(n)
        return (units[n // 100] + " Hundred " + _up_to_99(n % 100)).strip()

    def _up_to_lakh(n: int) -> str:
        if n < 1000:
            return _up_to_999(n)
        if n < 100000:
            return (_up_to_999(n // 1000) + " Thousand " + _up_to_999(n % 1000)).strip()
        return (_up_to_999(n // 100000) + " Lakh " + _up_to_999(n % 100000)).strip()

    def _up_to_crore(n: int) -> str:
        if n < 10000000:
            return _up_to_lakh(n)
        return (_up_to_crore(n // 10000000) + " Crore " + _up_to_lakh(n % 10000000)).strip()

    whole = int(amount)
    paise = int((amount - whole) * 100)
    if whole == 0 and paise == 0:
        return "Rupees Zero Only"
    whole_str = _up_to_crore(whole).strip() if whole else "Zero"
    out = f"Rupees {whole_str}"
    if paise > 0:
        out += f" and {_up_to_99(paise)} Paise"
    return out + " Only"


def _get_invoice_breakdown(booking) -> Tuple[Decimal, Decimal, Decimal]:
    """
    Returns (base_amount, gst_amount, total_amount) as Decimals.
    Attempts to infer from charge_breakdown; falls back to total_charge.
    """
    total = Decimal(str(getattr(booking, "total_charge", "0") or "0"))
    base = Decimal("0")
    gst = Decimal("0")
    breakdown = getattr(booking, "charge_breakdown", None) or []
    if isinstance(breakdown, list):
        for line in breakdown:
            if not isinstance(line, dict):
                continue
            desc = _safe_str(line.get("description", "")).lower()
            amt = Decimal(str(line.get("amount", 0) or 0))
            if "gst" in desc:
                gst += amt
            else:
                base += amt
    if base == 0 and gst == 0:
        base = total
    return (base.quantize(Decimal("0.01")), gst.quantize(Decimal("0.01")), total.quantize(Decimal("0.01")))


def build_booking_invoice_pdf(*, booking, billing_profile) -> bytes:
    """Generate a simple PDF invoice for a Booking."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16, alignment=TA_LEFT, spaceAfter=8)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9, leading=12)
    small_right = ParagraphStyle("small_right", parent=small, alignment=TA_RIGHT)

    org_name = getattr(settings, "ORG_LEGAL_NAME", "IIC Booking")
    org_gstin = getattr(settings, "ORG_GSTIN", "")
    org_address = getattr(settings, "ORG_ADDRESS", "")

    inv_no = getattr(booking, "virtual_booking_id", None) or f"BK-{getattr(booking, 'booking_id', '')}"
    inv_date = getattr(booking, "created_at", None)
    inv_date_str = inv_date.strftime("%Y-%m-%d") if inv_date else ""

    base, gst, total = _get_invoice_breakdown(booking)

    bill_name = (getattr(billing_profile, "billing_name", "") or "").strip() or getattr(booking.user, "name", "") or booking.user.email
    gstin = (getattr(billing_profile, "gstin", "") or "").strip()
    addr_lines = [
        getattr(billing_profile, "billing_address_line1", ""),
        getattr(billing_profile, "billing_address_line2", ""),
        " ".join([s for s in [getattr(billing_profile, "billing_city", ""), getattr(billing_profile, "billing_state", ""), getattr(billing_profile, "billing_pincode", "")] if s]),
        getattr(billing_profile, "billing_country", ""),
    ]
    addr_lines = [l.strip() for l in addr_lines if l and str(l).strip()]
    bill_addr = "<br/>".join([line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for line in addr_lines]) or "—"

    story = []
    story.extend(_pdf_letterhead_story_lines(department_name=_equipment_department_name(getattr(booking, "equipment", None))))
    story.append(Paragraph("INVOICE", h1))

    header = Table(
        [
            [
                Paragraph(f"<b>From</b><br/>{_safe_str(org_name)}<br/>{_safe_str(org_address)}" + (f"<br/>GSTIN: {_safe_str(org_gstin)}" if org_gstin else ""), small),
                Paragraph(f"<b>Invoice No.</b> {inv_no}<br/><b>Date</b> {inv_date_str}", small_right),
            ]
        ],
        colWidths=[11 * cm, 5.5 * cm],
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(header)
    story.append(Spacer(1, 0.4 * cm))

    bill_to = Table(
        [[Paragraph(f"<b>Bill To</b><br/>{_safe_str(bill_name)}<br/>{bill_addr}" + (f"<br/>GSTIN: {_safe_str(gstin)}" if gstin else ""), small)]],
        colWidths=[16.5 * cm],
    )
    bill_to.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.grey), ("PADDING", (0, 0), (-1, -1), 6)]))
    story.append(bill_to)
    story.append(Spacer(1, 0.5 * cm))

    eq = booking.equipment
    desc = f"{_safe_str(getattr(eq, 'name', 'Equipment'))} ({_safe_str(getattr(eq, 'code', ''))})"
    minutes = int(getattr(booking, "total_time_minutes", 0) or 0)
    duration = f"{minutes} minutes" if minutes else "—"

    items = [
        ["Description", "Qty", "Amount (₹)"],
        [desc, duration, _money(base)],
    ]
    if gst > 0:
        items.append(["GST", "", _money(gst)])
    items.append(["Total", "", _money(total)])

    t = Table(items, colWidths=[10.5 * cm, 3.0 * cm, 3.0 * cm], repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (2, 1), (2, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(t)

    doc.build(story)
    return buffer.getvalue()


def build_shipping_label_pdf(*, booking, billing_profile) -> bytes:
    """Generate a PDF shipping label (From/To) for a Booking.

    - Cut marker at top/bottom so users can cut and paste the label on the box.
    - "Samples for {Equipment Name} characterization" in bold, underlined, slightly larger.
    - Booking id: {ref} (Item line removed).
    - FROM: User profile shipping address (sender).
    - TO: IIC Roorkee address with Kind Attn: {OIC Name} (OIC = equipment's first manager).
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    # Bold, underlined, slightly larger for "Samples for {Equipment} characterization"
    samples_style = ParagraphStyle(
        "samples",
        parent=styles["Normal"],
        fontSize=14,
        leading=16,
        spaceAfter=8,
        alignment=1,  # TA_CENTER
    )
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=14)
    cut_style = ParagraphStyle(
        "cut",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#888888"),
        alignment=1,
        spaceAfter=6,
        spaceBefore=6,
    )

    eq = booking.equipment
    equipment_name = _safe_str(getattr(eq, "name", None) or getattr(eq, "code", "Equipment")).strip() or "Equipment"
    booking_ref = getattr(booking, "virtual_booking_id", None) or str(getattr(booking, "booking_id", ""))

    # OIC name: first equipment manager for this equipment
    oic_name = ""
    try:
        mgrs = list(getattr(eq, "equipment_managers", None).all()[:1] if hasattr(eq, "equipment_managers") else [])
        if mgrs:
            m = getattr(mgrs[0], "manager", None)
            if m:
                oic_name = _safe_str(getattr(m, "name", None) or getattr(m, "email", "")).strip()
    except Exception:
        pass

    # TO address: fixed IIC address + Kind Attn: OIC Name
    to_lines = [
        "The Head,",
        "Institute Instrumentation Centre,",
        "Indian Institute of Technology Roorkee",
        "Roorkee - 247667 (Uttarakhand) India",
    ]
    if oic_name:
        to_lines.append(f"Kind Attn: {oic_name}")

    # FROM address: user profile shipping address (or billing if same-as-billing)
    to_same = bool(getattr(billing_profile, "shipping_same_as_billing", True))
    if to_same:
        from_name = (getattr(billing_profile, "billing_name", "") or "").strip() or getattr(booking.user, "name", "") or booking.user.email
        from_lines = [
            getattr(billing_profile, "billing_address_line1", ""),
            getattr(billing_profile, "billing_address_line2", ""),
            " ".join([s for s in [getattr(billing_profile, "billing_city", ""), getattr(billing_profile, "billing_state", ""), getattr(billing_profile, "billing_pincode", "")] if s]),
            getattr(billing_profile, "billing_country", ""),
        ]
        from_phone = getattr(booking.user, "phone_number", "") or ""
    else:
        from_name = (getattr(billing_profile, "shipping_name", "") or "").strip() or getattr(booking.user, "name", "") or booking.user.email
        from_lines = [
            getattr(billing_profile, "shipping_address_line1", ""),
            getattr(billing_profile, "shipping_address_line2", ""),
            " ".join([s for s in [getattr(billing_profile, "shipping_city", ""), getattr(billing_profile, "shipping_state", ""), getattr(billing_profile, "shipping_pincode", "")] if s]),
            getattr(billing_profile, "shipping_country", ""),
        ]
        from_phone = (getattr(billing_profile, "shipping_phone", "") or "").strip()

    from_lines = [l.strip() for l in from_lines if l and str(l).strip()]

    # "Samples for {Equipment Name} characterization" - bold and underlined
    samples_line = f"<b><u>Samples for {equipment_name} characterization</u></b>"

    # Label dimensions: content must fit inside bordered frame (no overflow)
    label_width = 16 * cm
    # FROM/TO columns fit inside frame (frame has padding, so use slightly less than half width each)
    inner_width = label_width - (1 * cm)  # leave room for outer padding
    col_cm = (inner_width / 2) / cm
    from_to_col = min(8.25, col_cm) * cm

    # FROM/TO address table: fits inside outer border
    from_para = Paragraph(
        "<b>FROM</b><br/>" + _safe_str(from_name) + "<br/>" + "<br/>".join(from_lines) + (f"<br/>MOB: {_safe_str(from_phone)}" if from_phone else ""),
        body,
    )
    to_para = Paragraph("<b>TO</b><br/>" + "<br/>".join(to_lines), body)
    from_to_table = Table(
        [[from_para, to_para]],
        colWidths=[from_to_col, from_to_col],
    )
    from_to_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
                ("BACKGROUND", (1, 0), (1, -1), colors.HexColor("#e0f2fe")),
            ]
        )
    )

    # Cut instruction: appears OUTSIDE the bordered label (above and below)
    cut_text = Paragraph(
        "<font color='#64748b' size='8'>&#9679; &#8212; &#8212; Cut along the border to detach label &#8212; &#8212; &#9679;</font>",
        cut_style,
    )

    # Header: samples line + booking id
    header_content = Paragraph(
        samples_line + "<br/><font size='10'><b>Booking id:</b> " + booking_ref + "</font>",
        samples_style,
    )
    header_table = Table([[header_content]], colWidths=[inner_width])
    header_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#e0e7ff")),
                ("PADDING", (0, 0), (-1, -1), 10),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    # Reminder: Signed Equipment Requisition Form Attached (checkbox so users don't forget to send it)
    checkbox_style = ParagraphStyle(
        "checkbox",
        parent=styles["Normal"],
        fontSize=10,
        leading=12,
        leftIndent=0,
        spaceBefore=8,
        spaceAfter=4,
    )
    checkbox_para = Paragraph(
        "<b>NOTE:</b> <b>Signed Equipment Requisition Form Attached</b> (please send along with samples)",
        checkbox_style,
    )
    checkbox_cell = Table([[checkbox_para]], colWidths=[inner_width])
    checkbox_cell.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fef3c7")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    # Bordered frame: header + FROM/TO + checkbox reminder
    bordered_frame = Table(
        [
            [header_table],
            [from_to_table],
            [checkbox_cell],
        ],
        colWidths=[label_width],
    )
    bordered_frame.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 2.5, colors.HexColor("#1e3a5f")),
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ]
        )
    )

    # Cut text OUTSIDE border; bordered frame contains only label content
    story = [cut_text, bordered_frame, cut_text]
    doc.build(story)
    return buffer.getvalue()


def build_return_shipping_label_pdf(*, booking, billing_profile) -> bytes:
    """Generate a PDF return shipping label (From/To) for returning samples back to the user.

    FROM: IIC Roorkee address (sender)
    TO: External user's shipping address (recipient)
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Heading2"], fontSize=14, leading=16, alignment=1, spaceAfter=8)
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=14)
    cut_style = ParagraphStyle(
        "cut",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#888888"),
        alignment=1,
        spaceAfter=6,
        spaceBefore=6,
    )

    equipment_name = _safe_str(getattr(booking.equipment, "name", None) or getattr(booking.equipment, "code", "Equipment")).strip() or "Equipment"
    booking_ref = getattr(booking, "virtual_booking_id", None) or str(getattr(booking, "booking_id", ""))

    # FROM: fixed IIC address (sender)
    from_lines = [
        "Institute Instrumentation Centre,",
        "Indian Institute of Technology Roorkee",
        "Roorkee - 247667 (Uttarakhand) India",
    ]

    # TO: user shipping address (use billing vs shipping based on profile flag)
    to_same = bool(getattr(billing_profile, "shipping_same_as_billing", True))
    if to_same:
        to_name = (getattr(billing_profile, "billing_name", "") or "").strip() or getattr(booking.user, "name", "") or booking.user.email
        to_lines = [
            getattr(billing_profile, "billing_address_line1", ""),
            getattr(billing_profile, "billing_address_line2", ""),
            " ".join([s for s in [getattr(billing_profile, "billing_city", ""), getattr(billing_profile, "billing_state", ""), getattr(billing_profile, "billing_pincode", "")] if s]),
            getattr(billing_profile, "billing_country", ""),
        ]
        to_phone = getattr(booking.user, "phone_number", "") or ""
    else:
        to_name = (getattr(billing_profile, "shipping_name", "") or "").strip() or getattr(booking.user, "name", "") or booking.user.email
        to_lines = [
            getattr(billing_profile, "shipping_address_line1", ""),
            getattr(billing_profile, "shipping_address_line2", ""),
            " ".join([s for s in [getattr(billing_profile, "shipping_city", ""), getattr(billing_profile, "shipping_state", ""), getattr(billing_profile, "shipping_pincode", "")] if s]),
            getattr(billing_profile, "shipping_country", ""),
        ]
        to_phone = (getattr(billing_profile, "shipping_phone", "") or "").strip()
    to_lines = [l.strip() for l in to_lines if l and str(l).strip()]

    cut_text = Paragraph(
        "<font color='#64748b' size='8'>&#9679; &#8212; &#8212; Cut along the border to detach label &#8212; &#8212; &#9679;</font>",
        cut_style,
    )

    header = Paragraph(f"<b>Return shipment label</b><br/><font size='10'>Samples for {equipment_name} characterization</font><br/><font size='10'><b>Booking id:</b> {booking_ref}</font>", title_style)
    header_table = Table([[header]], colWidths=[16 * cm])
    header_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#e0e7ff")), ("PADDING", (0, 0), (-1, -1), 10)]))

    from_para = Paragraph("<b>FROM</b><br/>" + "<br/>".join(from_lines), body)
    to_para = Paragraph(
        "<b>TO</b><br/>" + _safe_str(to_name) + "<br/>" + "<br/>".join(to_lines) + (f"<br/>MOB: {_safe_str(to_phone)}" if to_phone else ""),
        body,
    )
    addr_table = Table([[from_para, to_para]], colWidths=[8 * cm, 8 * cm])
    addr_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
                ("BACKGROUND", (1, 0), (1, -1), colors.HexColor("#e0f2fe")),
            ]
        )
    )

    frame = Table([[header_table], [addr_table]], colWidths=[16 * cm])
    frame.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 2.5, colors.HexColor("#1e3a5f")),
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )

    story = [cut_text, frame, Spacer(1, 0.2 * cm), cut_text]
    doc.build(story)
    return buffer.getvalue()


def build_proforma_invoice_pdf(*, data: Dict[str, Any], billing_profile) -> bytes:
    """
    Proforma invoice for an equipment estimate.
    Expected `data` to include:
      - equipment_name, equipment_code
      - base_charge, gst_amount, total_charge
      - description (optional)
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16, spaceAfter=8)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9, leading=12)

    org_name = getattr(settings, "ORG_LEGAL_NAME", "IIC Booking")
    org_gstin = getattr(settings, "ORG_GSTIN", "")
    org_address = getattr(settings, "ORG_ADDRESS", "")

    bill_name = (getattr(billing_profile, "billing_name", "") or "").strip() or getattr(getattr(billing_profile, "user", None), "name", "") or getattr(getattr(billing_profile, "user", None), "email", "") or "—"
    gstin = (getattr(billing_profile, "gstin", "") or "").strip()

    story = []
    dept_name = (data.get("department_name") or "").strip() or getattr(settings, "ORG_DEPARTMENT_NAME", "") or getattr(settings, "ORG_LEGAL_NAME", "")
    story.extend(_pdf_letterhead_story_lines(department_name=dept_name))
    story.append(Paragraph("PROFORMA INVOICE", h1))
    story.append(Paragraph(f"<b>From</b><br/>{_safe_str(org_name)}<br/>{_safe_str(org_address)}" + (f"<br/>GSTIN: {_safe_str(org_gstin)}" if org_gstin else ""), small))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(f"<b>To</b><br/>{_safe_str(bill_name)}" + (f"<br/>GSTIN: {_safe_str(gstin)}" if gstin else ""), small))
    story.append(Spacer(1, 0.5 * cm))

    eq_name = _safe_str(data.get("equipment_name", "Equipment"))
    eq_code = _safe_str(data.get("equipment_code", ""))
    desc = _safe_str(data.get("description", "")).strip() or f"{eq_name} ({eq_code})".strip()
    base = _money(data.get("base_charge", "0"))
    gst = _money(data.get("gst_amount", "0"))
    total = _money(data.get("total_charge", "0"))

    rows = [
        ["Description", "Amount (₹)"],
        [desc, base],
    ]
    try:
        if Decimal(str(gst)) > 0:
            rows.append(["GST", gst])
    except Exception:
        pass
    rows.append(["Total", total])

    t = Table(rows, colWidths=[12.0 * cm, 4.5 * cm], repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (1, 1), (1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ]
        )
    )
    story.append(t)
    doc.build(story)
    return buffer.getvalue()


def _get_proforma_invoice_format():
    """Load admin-editable proforma format (singleton). Returns defaults if no row exists."""
    from .models import ProformaInvoiceFormat
    try:
        fmt = ProformaInvoiceFormat.objects.first()
    except Exception:
        fmt = None
    if not fmt:
        return {
            "terms_and_conditions": "Standard Terms and Conditions available for Standard Proforma Invoices.",
            "disclaimer": "This is a computer generated invoice and does not require a signature.",
        }
    return {
        "terms_and_conditions": (fmt.terms_and_conditions or "").strip()
        or "Standard Terms and Conditions available for Standard Proforma Invoices.",
        "disclaimer": (fmt.disclaimer or "").strip()
        or "This is a computer generated invoice and does not require a signature.",
    }


def build_proforma_invoice_multi_pdf(
    *,
    user,
    request_date_time,
    line_items: list,
    subtotal: str,
    total_gst: str,
    total_amount: str,
) -> bytes:
    """
    Proforma invoice PDF with IIT Roorkee letterhead, user details, date/time of request,
    line items table (inputs & charge breakup), totals, terms text, and disclaimer.
    Uses ProformaInvoiceFormat model for admin-editable terms and disclaimer.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=2.0 * cm,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16, alignment=TA_CENTER, spaceAfter=6)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9, leading=11)
    small_left = ParagraphStyle("small_left", parent=small, alignment=TA_LEFT)
    terms_style = ParagraphStyle("terms", parent=small, fontSize=9, alignment=TA_LEFT, spaceBefore=6, spaceAfter=4)
    disclaimer_style = ParagraphStyle("disclaimer", parent=small, fontSize=8, alignment=TA_CENTER, textColor=colors.grey, spaceBefore=8)

    org_name = getattr(settings, "ORG_LEGAL_NAME", "IIC Booking, IIT Roorkee")
    org_gstin = getattr(settings, "ORG_GSTIN", "")
    org_address = getattr(settings, "ORG_ADDRESS", "Indian Institute of Technology Roorkee, Roorkee, Uttarakhand, India")

    # User requesting
    user_name = getattr(user, "name", "") or getattr(user, "get_full_name", lambda: "")() or "Guest"
    user_email = getattr(user, "email", "") or "—"
    user_dept = getattr(user, "department_name", "") or getattr(user, "department", None)
    if user_dept and hasattr(user_dept, "name"):
        user_dept = user_dept.name
    user_dept = (user_dept or "—").strip() or "—"
    user_type = getattr(user, "user_type", "") or "—"
    if user_type:
        user_type = str(user_type).replace("_", " ").title()

    # Date and time: convert to local timezone and show on one line
    req_dt = request_date_time
    try:
        from django.utils import timezone as tz
        if hasattr(req_dt, "astimezone") and getattr(settings, "USE_TZ", True):
            if tz.is_naive(req_dt):
                req_dt = tz.make_aware(req_dt, tz.get_current_timezone())
            else:
                req_dt = req_dt.astimezone(tz.get_current_timezone())
    except Exception:
        pass
    req_date_str = req_dt.strftime("%d-%b-%Y") if hasattr(req_dt, "strftime") else str(req_dt)
    req_time_str = req_dt.strftime("%I:%M %p") if hasattr(req_dt, "strftime") else ""

    story = []
    # Derive department name: if all line items belong to the same internal department, show it; else show "Multiple Departments".
    dept_name = ""
    try:
        eq_ids = []
        for li in line_items or []:
            v = li.get("equipment_id") if isinstance(li, dict) else None
            if v is None:
                continue
            try:
                eq_ids.append(int(v))
            except Exception:
                continue
        if eq_ids:
            from .models import Equipment
            qs = Equipment.objects.select_related("internal_department").filter(equipment_id__in=list(set(eq_ids)))
            names = set()
            for eq in qs:
                d = getattr(eq, "internal_department", None)
                n = (getattr(d, "name", None) or "").strip() if d else ""
                if n:
                    names.add(n)
            if len(names) == 1:
                dept_name = list(names)[0]
            elif len(names) > 1:
                dept_name = "Multiple Departments"
    except Exception:
        dept_name = ""
    dept_name = dept_name or (getattr(settings, "ORG_DEPARTMENT_NAME", "") or getattr(settings, "ORG_LEGAL_NAME", ""))
    story.extend(_pdf_letterhead_story_lines(department_name=dept_name))
    story.append(Paragraph("PROFORMA INVOICE", h1))
    story.append(Paragraph(
        f"<b>From</b><br/>{_safe_str(org_name)}<br/>{_safe_str(org_address)}"
        + (f"<br/>GSTIN: {_safe_str(org_gstin)}" if org_gstin else ""),
        small,
    ))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        f"<b>Requested by</b><br/>{_safe_str(user_name)}<br/>"
        f"Email: {_safe_str(user_email)}<br/>Department: {_safe_str(user_dept)}<br/>User type: {_safe_str(user_type)}",
        small,
    ))
    # Date and Time on one line, left-aligned
    story.append(Paragraph(
        f"<b>Date of request:</b> {_safe_str(req_date_str)} &nbsp;&nbsp;&nbsp; <b>Time of request:</b> {_safe_str(req_time_str)}",
        small_left,
    ))
    story.append(Spacer(1, 0.5 * cm))

    format_opts = _get_proforma_invoice_format()

    # Use Rupee symbol (₹) if a supporting font is available, else "Rs."
    rupee_font_name = _register_pdf_rupee_font()
    rupees_label = _pdf_rupees_label()
    if rupee_font_name:
        small_rupee = ParagraphStyle("small_rupee", parent=small, fontName=rupee_font_name)
    else:
        small_rupee = small

    # Header row: light text on dark background (Paragraph style with white text so it's readable)
    header_font = rupee_font_name or "Helvetica-Bold"
    header_style = ParagraphStyle(
        "proforma_header",
        parent=small,
        fontName=header_font,
        fontSize=9,
        textColor=colors.white,
    )

    # Table headers and cells: use rupee symbol when font supports it (headers as Paragraphs so ₹ renders)
    base_col = f"Base ({rupees_label})"
    gst_col = f"GST ({rupees_label})"
    total_col = f"Total ({rupees_label})"
    header_row = ["#", "Equipment", "Inputs & charge breakup", base_col, gst_col, total_col]
    header_row = [
        Paragraph("#", header_style),
        Paragraph("Equipment", header_style),
        Paragraph("Inputs & charge breakup", header_style),
        Paragraph(base_col, header_style),
        Paragraph(gst_col, header_style),
        Paragraph(total_col, header_style),
    ]
    table_data = [header_row]
    for idx, row in enumerate(line_items, 1):
        eq_name = _safe_str(row.get("equipment_name", ""))
        eq_code = _safe_str(row.get("equipment_code", ""))
        eq_para = Paragraph(
            f"{eq_name} ({eq_code})".replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
            small,
        )
        input_vals = row.get("input_labels_and_values") or row.get("input_values") or {}
        if isinstance(input_vals, dict):
            input_parts = [f"{k}: {v}" for k, v in input_vals.items() if v not in (None, "", [])]
        else:
            input_parts = []
        breakdown = row.get("charge_breakdown") or []
        breakup_lines = list(input_parts)
        for b in breakdown:
            desc = b.get("description", "")
            amt = b.get("amount", 0)
            try:
                breakup_lines.append(f"{desc}: {rupees_label}{Decimal(str(amt)).quantize(Decimal('0.01'))}")
            except Exception:
                breakup_lines.append(f"{desc}: {rupees_label}{amt}")
        breakup_cell = "<br/>".join([line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for line in breakup_lines]) if breakup_lines else "—"
        base_c = _money(row.get("base_charge", "0"))
        gst_c = _money(row.get("gst_amount", "0"))
        tot_c = _money(row.get("total_charge", "0"))
        table_data.append([str(idx), eq_para, Paragraph(breakup_cell, small_rupee), base_c, gst_c, tot_c])

    table_data.append(["", "", "Subtotal", _money(subtotal), _money(total_gst), _money(total_amount)])
    # Final row: total amount with amount in words (same row, words in column 2)
    try:
        total_dec = Decimal(str(total_amount))
        amount_words = _amount_in_words(total_dec)
    except Exception:
        amount_words = "Rupees Only"
    total_amount_para = Paragraph(
        "Total amount<br/><font size='8'>" + amount_words.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</font>",
        small,
    )
    table_data.append(["", "", total_amount_para, "", "", _money(total_amount)])

    # Wider equipment column to avoid overlap; slightly narrower breakup column
    col_widths = [0.8 * cm, 4.0 * cm, 5.7 * cm, 2.0 * cm, 1.8 * cm, 2.2 * cm]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), header_font),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (3, 1), (5, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("BACKGROUND", (0, -2), (-1, -2), colors.HexColor("#e2e8f0")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#cbd5e1")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 0.3 * cm))
    # Terms and conditions (admin-editable), just after the table
    terms_text = format_opts["terms_and_conditions"]
    if terms_text:
        story.append(Paragraph(terms_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), terms_style))
    story.append(Spacer(1, 0.2 * cm))
    # Disclaimer (admin-editable)
    disclaimer_text = format_opts["disclaimer"]
    if disclaimer_text:
        story.append(Paragraph(disclaimer_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), disclaimer_style))
    doc.build(story)
    return buffer.getvalue()

