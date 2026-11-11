"""Proforma Invoice PDF, designed to match the online-payment receipt (same colours, crest watermark, bands).

Every proforma the portal issues (the estimate on the charges page, the one-equipment estimate and the government
organisations' consolidated proforma) goes through :func:`render_proforma`, so they all carry the fields a proforma
invoice needs in India:

* supplier: institute, department, address, GSTIN, PAN and state with its GST state code;
* proforma number, date of issue, validity and place of supply;
* buyer: name, organisation, billing address, GSTIN (or "Unregistered"), state, email and phone;
* each service with its SAC code, quantity, taxable value, GST rate and GST amount;
* GST split by place of supply: CGST + SGST inside the supplier's state, IGST outside it;
* total in figures and in words, how to pay, terms, a declaration and the authorised-signatory block;
* "not a tax invoice" said plainly, because no input tax credit can be claimed on a proforma.

The supplier's name, address, GSTIN, colours, crest and watermark come from the receipt template the Main Admin edits
on the dashboard, so the proforma and the receipt always agree. Bank details are printed only when
``PROFORMA_BANK_DETAILS`` is set; nothing is ever made up.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from xml.sax.saxutils import escape

from django.conf import settings

# GST state codes (first two digits of a GSTIN).
STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}
_ALIASES = {"uttaranchal": "05", "orissa": "21", "pondicherry": "34", "new delhi": "07", "nct of delhi": "07",
            "j&k": "01", "jammu & kashmir": "01", "daman and diu": "26", "dadra and nagar haveli": "26"}
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
SUPPLIER_STATE_CODE = "05"  # IIT Roorkee, Uttarakhand
DEFAULT_SAC = "998346"  # Technical testing and analysis services
_VAGUE_TERMS = "standard terms and conditions available for standard proforma invoices."

TWO = Decimal("0.01")


def dec(v) -> Decimal:
    try:
        return Decimal(str(v if v not in (None, "") else "0")).quantize(TWO, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def inr(v) -> str:
    """Indian digit grouping with two decimals: 1234567.5 -> 12,34,567.50."""
    d = dec(v)
    sign = "-" if d < 0 else ""
    whole, frac = f"{abs(d):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return f"{sign}{whole}.{frac}"


_DURATION = re.compile(r"\(\s*(?:slot\s+)?duration\s*:\s*([^()]*?)\s*\)", re.IGNORECASE)
_ONE = re.compile(r"(?<![\d.])1 (sample|slot|hour|part|piece|element|set|unit|item)s\b", re.IGNORECASE)


def charge_text(desc: str, rupee: str = "\u20b9") -> str:
    """A charge-breakdown line made short and readable, each fact once.

    "5.0 slots ( slot duration: 1.5 hours ) @ 3000.00 per slots ( slot duration: 1.5 hours )"
    becomes "5 slots of 1.5 hours @ ₹3,000 per slot".
    """
    s = " ".join(str(desc or "").split()).rstrip(": ")
    durations = [d.strip().lower() for d in _DURATION.findall(s) if d.strip()]
    s = _DURATION.sub("", s)
    s = re.sub(r"(?<![\d.])(\d+)\.0+(?!\d)", r"\1", s)  # 5.0 slots -> 5 slots
    s = re.sub(r"\bper (\w+?)s\b", r"per \1", s)  # per slots -> per slot
    s = re.sub(r"@\s*(?:\u20b9|Rs\.?)?\s*(\d+(?:\.\d+)?)",
               lambda m: f"@ {rupee}{inr(m.group(1)).removesuffix('.00')}", s)
    s = _ONE.sub(lambda m: f"1 {m.group(1)}", s)
    s = " ".join(s.split()).replace(" ,", ",").strip(" :")
    if durations:
        s = re.sub(r"^(\d+(?:\.\d+)? \w+)", lambda m: f"{m.group(1)} of {durations[0]}", s, count=1) \
            if re.match(r"^\d", s) else f"{s} ({durations[0]} each)"
    return s[:160]


def valid_gstin(value: str) -> str:
    v = (value or "").strip().upper()
    return v if _GSTIN_RE.match(v) else ""


def state_code_for(*, gstin: str = "", state: str = "") -> str:
    g = valid_gstin(gstin)
    if g and g[:2] in STATE_CODES:
        return g[:2]
    s = (state or "").strip().lower()
    if not s:
        return ""
    if s in _ALIASES:
        return _ALIASES[s]
    for code, name in STATE_CODES.items():
        if name.lower() == s or name.lower().replace(" and ", " & ") == s:
            return code
    return ""


def pan_from_gstin(gstin: str) -> str:
    g = valid_gstin(gstin)
    return g[2:12] if g else ""


@dataclass
class Party:
    name: str = ""
    organisation: str = ""
    address_lines: list = field(default_factory=list)
    gstin: str = ""
    state: str = ""
    state_code: str = ""
    email: str = ""
    phone: str = ""
    category: str = ""


@dataclass
class Line:
    title: str
    details: list = field(default_factory=list)
    taxable: Decimal = Decimal("0.00")
    gst: Decimal = Decimal("0.00")
    sac: str = ""
    qty: str = "1"

    @property
    def total(self) -> Decimal:
        return (self.taxable + self.gst).quantize(TWO)

    @property
    def rate(self) -> str:
        if self.taxable <= 0 or self.gst <= 0:
            return "Nil"
        pct = (self.gst * 100 / self.taxable).quantize(Decimal("0.01"))
        return f"{pct.normalize():f}%"


@dataclass
class Proforma:
    number: str
    issued_at: object  # aware datetime
    buyer: Party
    lines: list
    department: str = ""
    requested_at: object = None
    valid_days: int = 30
    notes: list = field(default_factory=list)
    title: str = "Proforma Invoice"


# --- supplier and look (from the receipt template) ---------------------------------------------------------------------

def _supplier_and_look(department: str) -> tuple[Party, dict]:
    try:
        from iic_booking.payments.receipts import template_values

        t = template_values()
    except Exception:  # the receipt template table may be missing in a bare test database
        from iic_booking.payments.receipts import template_defaults

        t = template_defaults()
    gstin = valid_gstin(t.get("institute_gstin", "")) if t.get("show_gstin", True) else ""
    code = gstin[:2] if gstin else SUPPLIER_STATE_CODE
    supplier = Party(
        name=t.get("institution_name") or "Indian Institute of Technology Roorkee",
        organisation=department or t.get("office_name") or "",
        address_lines=[t.get("address") or "Roorkee – 247667, Uttarakhand, India"],
        gstin=gstin,
        state=STATE_CODES.get(code, "Uttarakhand"),
        state_code=code,
        email=t.get("contact_email") or "",
    )
    look = {
        "brand": t.get("brand_color") or "#153F79",
        "accent": t.get("accent_color") or "#C29938",
        "website": t.get("website") or "equip.iitr.ac.in",
        "service": t.get("service_name") or "IIT Roorkee Equipment Booking Portal",
        "show_logo": t.get("show_logo", True),
        "show_watermark": t.get("show_watermark", True),
    }
    return supplier, look


def _crest_path() -> str | None:
    base = getattr(settings, "BASE_DIR", None)
    path = os.path.join(str(base), "fonts", "IITR_Logo.png") if base else ""
    return path if path and os.path.isfile(path) else None


def _fonts() -> tuple[str, str, bool]:
    """(regular, bold, has_rupee). Noto Sans ships in fonts/ and has the rupee sign; Helvetica is the fallback."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if "PINoto" in pdfmetrics.getRegisteredFontNames():
        return "PINoto", "PINoto-Bold", True
    base = getattr(settings, "BASE_DIR", None)
    reg = os.path.join(str(base), "fonts", "NotoSans-Regular.ttf") if base else ""
    bold = os.path.join(str(base), "fonts", "NotoSans-Bold.ttf") if base else ""
    try:
        if os.path.isfile(reg) and os.path.isfile(bold):
            pdfmetrics.registerFont(TTFont("PINoto", reg))
            pdfmetrics.registerFont(TTFont("PINoto-Bold", bold))
            from reportlab.lib.fonts import addMapping

            addMapping("PINoto", 0, 0, "PINoto")
            addMapping("PINoto", 1, 0, "PINoto-Bold")
            addMapping("PINoto", 0, 1, "PINoto")
            addMapping("PINoto", 1, 1, "PINoto-Bold")
            return "PINoto", "PINoto-Bold", True
    except Exception:
        pass
    return "Helvetica", "Helvetica-Bold", False


# --- rendering ---------------------------------------------------------------------------------------------------------

def _ist(dt):
    try:
        from django.utils import timezone

        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return timezone.localtime(dt)
    except Exception:
        return dt


def _bank_lines() -> list[str]:
    raw = getattr(settings, "PROFORMA_BANK_DETAILS", "") or ""
    if isinstance(raw, dict):
        return [f"{k}: {v}" for k, v in raw.items() if str(v).strip()]
    return [s.strip() for s in str(raw).split("|") if s.strip()]


def render_proforma(pi: Proforma, *, extra_terms: str = "", disclaimer: str = "") -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        KeepTogether,
        PageTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )

    from .document_exports import _amount_in_words

    supplier, look = _supplier_and_look(pi.department)
    sans, sans_b, has_rupee = _fonts()
    R = "₹" if has_rupee else "Rs."
    W, H = A4
    M = 15 * mm
    CW = W - 2 * M

    def hexc(v, fallback):
        try:
            return colors.HexColor(v)
        except Exception:
            return colors.HexColor(fallback)

    navy = hexc(look["brand"], "#153F79")
    gold = hexc(look["accent"], "#C29938")
    ink = colors.HexColor("#1E293B")
    muted = colors.HexColor("#5B6573")
    rule = colors.HexColor("#D6DBE4")
    tint = colors.HexColor("#F3F6FB")
    cream = colors.HexColor("#FEFBF3")
    crest = _crest_path() if (look["show_logo"] or look["show_watermark"]) else None

    def st(name, size=9, font=None, color=ink, align=TA_LEFT, leading=None, **kw):
        return ParagraphStyle(name, fontName=font or sans, fontSize=size, leading=leading or size * 1.32,
                              textColor=color, alignment=align, **kw)

    body = st("body", 8.6)
    body_r = st("body_r", 8.6, align=TA_RIGHT)
    body_c = st("body_c", 8.6, align=TA_CENTER)
    bold = st("bold", 8.8, sans_b)
    bold_r = st("bold_r", 8.8, sans_b, align=TA_RIGHT)
    small = st("small", 7.4, color=muted)
    lab = st("lab", 6.8, sans_b, muted)
    head_w = st("head_w", 7.2, sans_b, colors.white)
    head_wr = st("head_wr", 7.2, sans_b, colors.white, align=TA_RIGHT)
    head_wc = st("head_wc", 7.2, sans_b, colors.white, align=TA_CENTER)
    section = st("section", 8.4, sans_b, navy)

    def P(text, style=body):
        return Paragraph(text, style)

    def E(text):
        s = str(text or "")
        if not has_rupee:
            s = s.replace("₹", "Rs.")
        return escape(s)

    issued = _ist(pi.issued_at)
    valid_until = issued + timedelta(days=max(1, int(pi.valid_days or 30)))
    buyer = pi.buyer
    pos_code = buyer.state_code or state_code_for(gstin=buyer.gstin, state=buyer.state)
    pos_name = STATE_CODES.get(pos_code, buyer.state or "")
    inter_state = bool(pos_code) and pos_code != supplier.state_code

    taxable = sum((ln.taxable for ln in pi.lines), Decimal("0.00"))
    gst = sum((ln.gst for ln in pi.lines), Decimal("0.00"))
    total = (taxable + gst).quantize(TWO)

    story = []

    # Title row: name of the document on the left, "not a tax invoice" stamp on the right.
    stamp = Table([[P("NOT A TAX INVOICE", st("stamp", 8.4, sans_b, colors.HexColor("#8A1C1C"), TA_CENTER))]],
                  colWidths=[42 * mm], rowHeights=[9 * mm])
    stamp.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1.3, colors.HexColor("#8A1C1C")),
                               ("INNERGRID", (0, 0), (-1, -1), 0, colors.white),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    subtitle = look["service"]
    if pi.requested_at:
        subtitle += f"  ·  Estimate requested on {_ist(pi.requested_at).strftime('%d %b %Y, %I:%M %p')} IST"
    title_tbl = Table([[
        [P(E(pi.title.upper()), st("title", 19, "Times-Bold", navy, leading=22)),
         P(E(subtitle), st("sub", 8.2, color=muted))],
        stamp,
    ]], colWidths=[CW - 44 * mm, 44 * mm])
    title_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [title_tbl, Spacer(1, 3 * mm)]

    # Number / dates / place of supply strip.
    cells = [("Proforma no.", pi.number), ("Date of issue", issued.strftime("%d %b %Y")),
             ("Valid until", valid_until.strftime("%d %b %Y")),
             ("Place of supply", f"{pos_name} ({pos_code})" if pos_code else (pos_name or "Not provided"))]
    strip = Table([[[P(E(k.upper()), lab), P(E(v), st("sv", 9.4, sans_b))] for k, v in cells]],
                  colWidths=[CW * 0.31, CW * 0.21, CW * 0.21, CW * 0.27])
    strip.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), tint), ("BOX", (0, 0), (-1, -1), 0.6, rule),
                               ("LINEBEFORE", (1, 0), (-1, -1), 0.5, rule), ("TOPPADDING", (0, 0), (-1, -1), 6),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [strip, Spacer(1, 3.5 * mm)]

    # Supplier and buyer.
    def party_block(heading, p: Party, is_supplier: bool):
        rows = [P(E(heading), section), Spacer(1, 1.5 * mm), P(E(p.name or "—"), st("pn", 10.2, sans_b, leading=13))]
        if p.organisation:
            rows.append(P(E(p.organisation), st("po", 8.8, sans_b if is_supplier else sans)))
        for line in p.address_lines:
            if line:
                rows.append(P(E(line), body))
        facts = []
        if is_supplier:
            facts.append(("GSTIN", p.gstin or "—"))
            pan = pan_from_gstin(p.gstin)
            if pan:
                facts.append(("PAN", pan))
        else:
            facts.append(("GSTIN", valid_gstin(p.gstin) or (p.gstin.strip() if p.gstin else "Unregistered")))
        facts.append(("State", f"{p.state} (code {p.state_code})" if p.state and p.state_code else (p.state or "Not provided")))
        if p.email and not is_supplier:  # the supplier's email is in the page header
            facts.append(("Email", p.email))
        if p.phone:
            facts.append(("Phone", p.phone))
        if p.category:
            facts.append(("Category", p.category))
        rows.append(Spacer(1, 1 * mm))
        fact = lambda k, v: f"<font name='{sans_b}' color='#5B6573' size='6.8'>{E(k.upper())}</font>&nbsp;{E(v)}"  # noqa: E731
        for i in range(0, len(facts), 2):  # two facts a line keeps the block short
            rows.append(P("&nbsp;&nbsp;&nbsp;&nbsp;".join(fact(k, v) for k, v in facts[i:i + 2]), st("fact", 8.2)))
        return rows

    buyer_party = Party(**{**buyer.__dict__, "state": pos_name or buyer.state, "state_code": pos_code})
    parties = Table([[party_block("SUPPLIER (ISSUED BY)", supplier, True), party_block("BILL TO (RECIPIENT)", buyer_party, False)]],
                    colWidths=[CW / 2 - 3 * mm, CW / 2 - 3 * mm], spaceBefore=0)
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEABOVE", (0, 0), (-1, 0), 1.2, navy),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, 0), 6 * mm),
                                 ("TOPPADDING", (0, 0), (-1, -1), 5)]))
    story += [parties, Spacer(1, 3.5 * mm)]

    # Items.
    cols = [8 * mm, None, 15 * mm, 10 * mm, 25 * mm, 14 * mm, 22 * mm, 26 * mm]
    cols[1] = CW - sum(c for c in cols if c)
    head = [P("#", head_wc), P("DESCRIPTION OF SERVICE", head_w), P("SAC", head_wc), P("QTY", head_wc),
            P(f"TAXABLE ({R})", head_wr), P("GST", head_wc), P(f"GST ({R})", head_wr), P(f"AMOUNT ({R})", head_wr)]
    data = [head]
    for i, ln in enumerate(pi.lines, 1):
        desc = [P(E(ln.title), bold)]
        if ln.details:
            desc.append(P("<br/>".join(E(d) for d in ln.details[:8]), st("det", 7.8, color=muted, leading=10)))
        data.append([P(str(i), body_c), desc, P(E(ln.sac or DEFAULT_SAC), body_c), P(E(ln.qty), body_c),
                     P(inr(ln.taxable), body_r), P(ln.rate, body_c), P(inr(ln.gst) if ln.gst else "Nil", body_r),
                     P(inr(ln.total), bold_r)])
    items = Table(data, colWidths=cols, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), navy), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LINEBELOW", (0, 1), (-1, -1), 0.5, rule), ("LINEBELOW", (0, 0), (-1, 0), 1.6, gold),
             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
             ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    for r in range(2, len(data), 2):
        style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#FAFBFD")))
    items.setStyle(TableStyle(style))
    story += [items, Spacer(1, 3 * mm)]

    # Tax summary (left) and totals (right).
    if gst > 0:
        if inter_state:
            tax_rows = [("IGST", gst)]
            tax_note = f"Inter-state supply (Uttarakhand to {pos_name}): IGST applies."
        else:
            cgst = (gst / 2).quantize(TWO, rounding=ROUND_HALF_UP)
            tax_rows = [("CGST", cgst), ("SGST", gst - cgst)]
            tax_note = ("Intra-state supply within Uttarakhand: CGST and SGST apply." if pos_code
                        else "Place of supply not given: shown as within Uttarakhand (CGST + SGST). "
                             "Add your billing state or GSTIN to your profile for the correct split.")
    else:
        tax_rows = []
        tax_note = "GST is not charged on these services for your user category."
    rate_txt = pi.lines[0].rate if pi.lines and len({ln.rate for ln in pi.lines}) == 1 else ""

    def half(r):
        try:
            return f"{(Decimal(r.rstrip('%')) / 2).normalize():f}%"
        except Exception:
            return ""

    totals = [[P("Taxable value", body), P(f"{R} {inr(taxable)}", body_r)]]
    for name, amt in tax_rows:
        pct = (rate_txt if name == "IGST" else half(rate_txt)) if rate_txt and rate_txt != "Nil" else ""
        totals.append([P(f"{name}{' @ ' + pct if pct else ''}", body), P(f"{R} {inr(amt)}", body_r)])
    if not tax_rows:
        totals.append([P("GST", body), P("Nil", body_r)])
    totals.append([P("TOTAL PAYABLE", st("tp", 9.6, sans_b, colors.white)),
                   P(f"{R} {inr(total)}", st("tpv", 12.5, sans_b, colors.white, TA_RIGHT, leading=15))])
    tot_tbl = Table(totals, colWidths=[38 * mm, 40 * mm])
    tot_tbl.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -2), 0.5, rule), ("BACKGROUND", (0, -1), (-1, -1), navy),
                                 ("LINEBEFORE", (0, -1), (0, -1), 3, gold), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                                 ("TOPPADDING", (0, -1), (-1, -1), 7), ("BOTTOMPADDING", (0, -1), (-1, -1), 7)]))
    left = [P("TAX SUMMARY", section), Spacer(1, 1.5 * mm), P(E(tax_note), body), Spacer(1, 1.5 * mm),
            P(f"SAC {DEFAULT_SAC}: technical testing and analysis services.", small),
            P("Reverse charge: No.", small)]
    summary = Table([[left, tot_tbl]], colWidths=[CW - 80 * mm, 80 * mm])
    summary.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, 0), 8 * mm),
                                 ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    words = Table([[P(f"<i>Amount in words: {E(_amount_in_words(total))}</i>", st("w", 9, color=ink))]], colWidths=[CW])
    words.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), tint), ("LINEBEFORE", (0, 0), (0, -1), 2.6, gold),
                               ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                               ("LEFTPADDING", (0, 0), (-1, -1), 9)]))
    story += [KeepTogether([summary, Spacer(1, 3 * mm), words]), Spacer(1, 3.5 * mm)]

    # How to pay, and terms.
    bank = _bank_lines()
    pay = [P("HOW TO PAY", section), Spacer(1, 1.5 * mm),
           P(f"Pay through the {E(look['service'])} ({E(look['website'])}): recharge your wallet online or against "
             f"an offline payment receipt, quoting proforma no. <b>{E(pi.number)}</b>.", body)]
    if bank:
        pay += [Spacer(1, 1.5 * mm), P("Bank transfer", bold)] + [P(E(b), body) for b in bank]
    terms_list = [
        f"This proforma is valid until {valid_until.strftime('%d %b %Y')}. Charges are those of the portal's tariff "
        "on the day of booking and may change after that date.",
        "This is not a tax invoice and no input tax credit can be claimed on it. The GST tax invoice is issued by "
        "SRIC, IIT Roorkee, after the payment is received.",
        "Booking a slot or accepting samples is subject to equipment availability and the facility's sample "
        "acceptance rules.",
        "Refunds, where due, follow the portal's Cancellation and Refund Policy.",
    ]
    custom = (extra_terms or "").strip()
    if custom and custom.lower() != _VAGUE_TERMS:
        terms_list.append(custom)
    terms_list += [n for n in pi.notes if n]
    terms = [P("TERMS AND CONDITIONS", section), Spacer(1, 1.5 * mm)]
    terms += [P(f"{i}.&nbsp;&nbsp;{E(t)}", st(f"t{i}", 7.8, leading=10.2, leftIndent=9, firstLineIndent=-9))
              for i, t in enumerate(terms_list, 1)]
    pt = Table([[pay, terms]], colWidths=[CW * 0.42, CW * 0.58])
    pt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (0, 0), 7 * mm), ("RIGHTPADDING", (1, 0), (1, 0), 0),
                            ("LINEABOVE", (0, 0), (-1, 0), 0.8, rule), ("TOPPADDING", (0, 0), (-1, -1), 6)]))
    story += [pt, Spacer(1, 3.5 * mm)]

    # Declaration and authorised signatory.
    disclaimer = (disclaimer or "").strip() or "This is a computer-generated proforma invoice and does not require a signature."
    decl = [P("DECLARATION", section), Spacer(1, 1.5 * mm),
            P("We declare that this proforma shows the actual price of the services described and that all "
              "particulars are true and correct.", body)]
    sign = [P(f"For {E(supplier.name)}", st("for", 8.6, sans_b, align=TA_RIGHT)),
            P(E(supplier.organisation), st("fo", 7.8, color=muted, align=TA_RIGHT)), Spacer(1, 5 * mm),
            P("Authorised Signatory", st("as", 8.6, sans_b, navy, TA_RIGHT)),
            P(E(disclaimer), st("dis", 6.9, color=muted, align=TA_RIGHT))]
    ds = Table([[decl, sign]], colWidths=[CW * 0.56, CW * 0.44])
    ds.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.9, gold), ("BACKGROUND", (0, 0), (-1, -1), cream),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 8),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 10)]))
    story.append(KeepTogether([ds]))

    # Page chrome: drawn per page, page numbers and watermark added once the page count is known.
    HEADER_H = 34 * mm
    FOOTER_H = 13 * mm

    def draw_header(c, first: bool):
        c.saveState()
        c.setFillColor(navy)
        c.rect(0, H - 3.2 * mm, W, 3.2 * mm, stroke=0, fill=1)
        c.setFillColor(gold)
        c.rect(0, H - 4.1 * mm, W, 0.9 * mm, stroke=0, fill=1)
        top = H - 9 * mm
        size = 21 * mm
        x = M
        if crest and look["show_logo"]:
            c.drawImage(crest, M, top - size, size, size, mask="auto", preserveAspectRatio=True)
            x = M + size + 5 * mm
        name = supplier.name.upper()
        fs = 15
        while c.stringWidth(name, "Times-Bold", fs) > W - M - x and fs > 10:
            fs -= 0.5
        c.setFillColor(navy)
        c.setFont("Times-Bold", fs)
        c.drawString(x, top - 6 * mm, name)
        c.setFillColor(ink)
        c.setFont(sans_b, 9.4)
        c.drawString(x, top - 11 * mm, supplier.organisation[:95])
        c.setFillColor(muted)
        c.setFont(sans, 8)
        c.drawString(x, top - 15.3 * mm, (supplier.address_lines[0] if supplier.address_lines else "")[:120])
        facts = [f"GSTIN: {supplier.gstin}" if supplier.gstin else "", f"PAN: {pan_from_gstin(supplier.gstin)}" if supplier.gstin else "",
                 supplier.email, look["website"]]
        c.drawString(x, top - 19.3 * mm, "   |   ".join(f for f in facts if f)[:150])
        y = H - HEADER_H + 4 * mm
        c.setStrokeColor(navy)
        c.setLineWidth(1.3)
        c.line(M, y, W - M, y)
        c.setStrokeColor(gold)
        c.setLineWidth(0.6)
        c.line(M, y - 1.1 * mm, W - M, y - 1.1 * mm)
        c.restoreState()

    class Numbered(rl_canvas.Canvas):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            n = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._chrome(n)
                super().showPage()
            super().save()

        def _chrome(self, n):
            # Watermark over the content, so filled boxes never hide it: the crest and a diagonal PROFORMA.
            self.saveState()
            if look["show_watermark"]:
                if crest:
                    self.setFillAlpha(0.06)
                    s = 118 * mm
                    self.drawImage(crest, (W - s) / 2, (H - s) / 2 - 12 * mm, s, s, mask="auto", preserveAspectRatio=True)
                self.setFillColor(navy)
                self.setFillAlpha(0.05)
                self.translate(W / 2, H / 2 - 10 * mm)
                self.rotate(35)
                self.setFont(sans_b, 74)
                self.drawCentredString(0, -26, "PROFORMA")
            self.restoreState()
            self.saveState()
            self.setFillColor(gold)
            self.rect(0, FOOTER_H - 1 * mm, W, 0.7 * mm, stroke=0, fill=1)
            self.setFillColor(navy)
            self.rect(0, 0, W, FOOTER_H - 1 * mm, stroke=0, fill=1)
            self.setFillColor(colors.white)
            self.setFont(sans, 7.4)
            self.drawString(M, 4.6 * mm, f"{pi.number}  |  {look['service']}  |  {look['website']}"[:110])
            self.drawRightString(W - M, 4.6 * mm, f"Page {self._pageNumber} of {n}")
            self.restoreState()

    buffer = io.BytesIO()
    doc = BaseDocTemplate(buffer, pagesize=A4, leftMargin=M, rightMargin=M, topMargin=HEADER_H, bottomMargin=FOOTER_H + 4 * mm,
                          title=f"{pi.title} {pi.number}", author=supplier.name, subject=pi.title,
                          creator=look["service"])
    frame = Frame(M, FOOTER_H + 4 * mm, CW, H - HEADER_H - FOOTER_H - 4 * mm, id="body", leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="pi", frames=[frame], onPage=lambda c, d: draw_header(c, d.page == 1))])
    doc.build(story, canvasmaker=Numbered)
    return buffer.getvalue()


# --- building the data ---------------------------------------------------------------------------------------------------

def new_number(issued_at) -> str:
    import secrets

    return f"IIC-PI-{_ist(issued_at):%Y%m%d-%H%M}-{secrets.token_hex(2).upper()}"


def buyer_from(user, billing=None) -> Party:
    """The recipient: billing profile first (legal name, address, GSTIN), then the account itself."""
    from iic_booking.users.display import get_user_display_name

    if user is None:
        return Party(name="Guest (not signed in)", category="Estimate only")
    if billing is None:
        try:
            from iic_booking.users.models import ExternalBillingProfile

            billing = ExternalBillingProfile.objects.filter(user=user).first()
        except Exception:
            billing = None
    person = get_user_display_name(user, fallback_to_email=False) or getattr(user, "email", "") or "—"
    legal = (getattr(billing, "billing_name", "") or "").strip()
    org = ""
    for attr in ("organisation", "organization", "institute_name", "company_name"):
        v = getattr(user, attr, "")
        if isinstance(v, str) and v.strip():
            org = v.strip()
            break
    if not org:
        dept = getattr(user, "department", None)
        org = (getattr(dept, "name", "") or getattr(user, "department_name", "") or "").strip() if dept or hasattr(user, "department_name") else ""
    lines = []
    if billing is not None:
        l1 = ", ".join(x for x in [getattr(billing, "billing_address_line1", ""), getattr(billing, "billing_address_line2", "")] if x)
        l2 = " – ".join(x for x in [getattr(billing, "billing_city", ""), getattr(billing, "billing_pincode", "")] if x)
        lines = [x for x in [l1, l2] if x]
    state = (getattr(billing, "billing_state", "") or "").strip() if billing is not None else ""
    gstin = (getattr(billing, "gstin", "") or "").strip().upper() if billing is not None else ""
    code = state_code_for(gstin=gstin, state=state)
    user_type = str(getattr(user, "user_type", "") or "").replace("_", " ").title()
    return Party(
        name=legal or person,
        organisation=(f"Attn: {person}" if legal and legal != person else org),
        address_lines=lines,
        gstin=gstin,
        state=STATE_CODES.get(code, state),
        state_code=code,
        email=getattr(user, "email", "") or "",
        phone=(getattr(user, "phone_number", "") or "").strip(),
        category=user_type,
    )
