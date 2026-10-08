"""Bookings export as an A4 report.

Cover page (IIT Roorkee letterhead, filters, counts by status), a summary table of every booking, then a
details card per booking: header band with Booking ID, equipment and status, the user / slot details the
page shows for the viewer's role, every user input (tables as tables, one block per sample set), uploaded
file names and the charge breakdown when the viewer may see charges. Text uses the Noto Sans fonts in the
repository's ``fonts`` folder (₹ and Devanagari), falling back to Helvetica with "Rs." when they are missing.
"""

from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass
from types import SimpleNamespace

from .booking_list_export import PORTAL_HEADER
from .booking_list_export import _cell_text

logger = logging.getLogger(__name__)

BRAND = "#153f79"
INK = "#1e293b"
MUTED = "#64748b"
RULE = "#cbd5e1"
LABEL_BG = "#f1f5f9"
STRIPE_BG = "#f8fafc"
TABLE_HEAD_BG = "#e3ebf6"
SET_BG = "#eef3fa"

# (text, background) of the status badge
_STATUS_COLOURS = {
    "BOOKED": ("#1d4ed8", "#dbeafe"),
    "COMPLETED": ("#15803d", "#dcfce7"),
    "PROCESSING": ("#0f766e", "#ccfbf1"),
    "PENDING": ("#b45309", "#fef3c7"),
    "PENDING_PAYMENT": ("#b45309", "#fef3c7"),
    "HOLD": ("#b45309", "#fef3c7"),
    "DISRUPTION_PENDING": ("#c2410c", "#ffedd5"),
    "UNDER_MAINTENANCE": ("#c2410c", "#ffedd5"),
    "OTHER_DISRUPTION": ("#c2410c", "#ffedd5"),
    "ABSENT": ("#c2410c", "#ffedd5"),
    "BOOKING_NOT_UTILIZED": ("#c2410c", "#ffedd5"),
    "CANCELLED": ("#b91c1c", "#fee2e2"),
    "REFUNDED": ("#475569", "#e2e8f0"),
    "WAITLISTED": ("#7e22ce", "#f3e8ff"),
}
_DEFAULT_STATUS_COLOURS = ("#475569", "#e2e8f0")

_DEVANAGARI_CHARS = "\u0900-\u097F\uA8E0-\uA8FF\u1CD0-\u1CFF"
_DEVANAGARI_RUN = re.compile(
    f"[{_DEVANAGARI_CHARS}]+(?:[\\s\u200c\u200d]+[{_DEVANAGARI_CHARS}]+)*",
)
_ASCII_FALLBACK = {"₹": "Rs.", "–": "-", "—": "-", "·": "-", "×": "x", "…": "..."}

# Card fields that are already in the header band.
_BAND_KEYS = {"sno", "booking_id", "equipment", "status"}


@dataclass(frozen=True)
class Fonts:
    regular: str
    bold: str
    devanagari: str | None
    unicode: bool


def register_fonts() -> Fonts:
    """Noto Sans (Latin, ₹) and Noto Sans Devanagari from ``BASE_DIR/fonts``; DejaVu or Helvetica otherwise."""
    from django.conf import settings
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font_dir = os.path.join(str(getattr(settings, "BASE_DIR", "")), "fonts")

    def family(name: str, regular_file: str, bold_file: str) -> bool:
        registered = set(pdfmetrics.getRegisteredFontNames())
        try:
            for font_name, filename in ((name, regular_file), (f"{name}-Bold", bold_file)):
                if font_name not in registered:
                    pdfmetrics.registerFont(TTFont(font_name, os.path.join(font_dir, filename)))
        except Exception:  # noqa: BLE001 - missing or unreadable font file
            logger.warning("Bookings PDF export: font %s not available in %s", regular_file, font_dir)
            return False
        pdfmetrics.registerFontFamily(
            name, normal=name, bold=f"{name}-Bold", italic=name, boldItalic=f"{name}-Bold",
        )
        return True

    if family("IICNotoSans", "NotoSans-Regular.ttf", "NotoSans-Bold.ttf"):
        deva = "IICNotoDeva" if family(
            "IICNotoDeva", "NotoSansDevanagari-Regular.ttf", "NotoSansDevanagari-Bold.ttf",
        ) else None
        return Fonts("IICNotoSans", "IICNotoSans-Bold", deva, True)

    from .document_exports import _register_pdf_rupee_font

    dejavu = _register_pdf_rupee_font()
    if dejavu:
        return Fonts(dejavu, dejavu, None, True)
    return Fonts("Helvetica", "Helvetica-Bold", None, False)


def markup(text, fonts: Fonts) -> str:
    """Paragraph markup for plain text: escaped, Devanagari runs in the Devanagari font, newlines as breaks."""
    text = "" if text is None else str(text)
    if not fonts.unicode:
        for char, replacement in _ASCII_FALLBACK.items():
            text = text.replace(char, replacement)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if fonts.devanagari:
        text = _DEVANAGARI_RUN.sub(lambda m: f'<font name="{fonts.devanagari}">{m.group(0)}</font>', text)
    return text.replace("\n", "<br/>")


def _money_text(amount, fonts: Fonts) -> str:
    if amount is None:
        return ""
    return f"{'₹' if fonts.unicode else 'Rs. '}{amount:,.2f}"


def _styles(fonts: Fonts):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle

    def style(name, *, bold=False, size=8.0, leading=None, color=INK, **kw):
        return ParagraphStyle(
            name, fontName=fonts.bold if bold else fonts.regular, fontSize=size,
            leading=leading or round(size * 1.3, 1), textColor=colors.HexColor(color), **kw,
        )

    return SimpleNamespace(
        title=style("exp_title", bold=True, size=22, leading=27, color=BRAND, alignment=TA_CENTER),
        subtitle=style("exp_subtitle", size=10, color=MUTED, alignment=TA_CENTER),
        h2=style("exp_h2", bold=True, size=12.5, leading=16, color=BRAND, spaceBefore=4, spaceAfter=5),
        panel_title=style("exp_panel_title", bold=True, size=9, color=BRAND),
        section=style("exp_section", bold=True, size=9.2, leading=12, color=BRAND),
        label=style("exp_label", size=7.3, leading=9.2, color=MUTED),
        value=style("exp_value", size=8, leading=10.2),
        cell=style("exp_cell", size=7.4, leading=9.3),
        cell_small=style("exp_cell_small", size=6.6, leading=8.3),
        cell_right=style("exp_cell_right", size=7.4, leading=9.3, alignment=TA_RIGHT),
        cell_bold_right=style("exp_cell_bold_right", bold=True, size=7.6, leading=9.5, alignment=TA_RIGHT),
        cell_bold=style("exp_cell_bold", bold=True, size=7.6, leading=9.5),
        head=style("exp_head", bold=True, size=7.4, leading=9.3, color="#ffffff"),
        table_head=style("exp_table_head", bold=True, size=7.2, leading=9, color=BRAND),
        table_head_small=style("exp_table_head_small", bold=True, size=6.5, leading=8.2, color=BRAND),
        table_label=style("exp_table_label", bold=True, size=7.6, leading=9.6, color="#334155", spaceBefore=3,
                          spaceAfter=2),
        band_id=style("exp_band_id", bold=True, size=11.5, leading=14, color="#ffffff"),
        band_sub=style("exp_band_sub", size=8.5, leading=11, color="#dbe7f7"),
        badge=style("exp_badge", bold=True, size=7.4, leading=9, alignment=TA_CENTER),
        set_title=style("exp_set_title", bold=True, size=8.4, leading=10.5, color=BRAND),
        note=style("exp_note", size=7.6, leading=9.8, color=MUTED),
        count=style("exp_count", bold=True, size=8, leading=10, alignment=TA_RIGHT),
    )


class _Builder:
    def __init__(self, fonts: Fonts, width: float, charges: bool):
        self.fonts = fonts
        self.S = _styles(fonts)
        self.width = width
        self.charges = charges

    def p(self, text, style):
        from reportlab.platypus import Paragraph

        return Paragraph(markup(text, self.fonts), style)

    # -- small pieces ------------------------------------------------------

    def badge(self, status_code: str, text: str):
        from reportlab.lib import colors
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from reportlab.platypus import Paragraph
        from reportlab.platypus import Table
        from reportlab.platypus import TableStyle

        fg, bg = _STATUS_COLOURS.get(status_code or "", _DEFAULT_STATUS_COLOURS)
        label = text or status_code or ""
        style = self.S.badge.clone("exp_badge_c", textColor=colors.HexColor(fg))
        width = min(stringWidth(label, self.fonts.bold, style.fontSize) + 16, 150)
        badge = Table([[Paragraph(markup(label, self.fonts), style)]], colWidths=[width], cornerRadii=[6, 6, 6, 6])
        badge.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(bg)),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        badge.hAlign = "RIGHT"
        return badge

    def section(self, title: str):
        from reportlab.lib import colors
        from reportlab.platypus import Table
        from reportlab.platypus import TableStyle

        heading = Table([[self.p(title, self.S.section)]], colWidths=[self.width])
        heading.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.8, colors.HexColor(BRAND)),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ]))
        return heading

    def kv_table(self, pairs, *, columns: int = 1):
        """Label / value rows; ``columns=2`` puts two pairs side by side."""
        from reportlab.lib import colors
        from reportlab.platypus import Table
        from reportlab.platypus import TableStyle

        if columns == 2:
            label_w = 2.75 * 28.35
            value_w = self.width / 2 - label_w
            widths = [label_w, value_w, label_w, value_w]
            data = []
            for i in range(0, len(pairs), 2):
                chunk = list(pairs[i:i + 2]) + [("", "")] * (2 - len(pairs[i:i + 2]))
                row = []
                for label, value in chunk:
                    row.extend([self.p(label, self.S.label), self.p(value, self.S.value)])
                data.append(row)
            label_cols = [0, 2]
        else:
            label_w = min(6.4 * 28.35, self.width * 0.38)
            widths = [label_w, self.width - label_w]
            data = [[self.p(label, self.S.label), self.p(value, self.S.value)] for label, value in pairs]
            label_cols = [0]
        table = Table(data, colWidths=widths)
        style = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(RULE)),
            ("LINEBELOW", (0, 0), (-1, -2), 0.35, colors.HexColor(RULE)),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]
        for col in label_cols:
            style.append(("BACKGROUND", (col, 0), (col, -1), colors.HexColor(LABEL_BG)))
        if columns == 2:
            style.append(("LINEBEFORE", (2, 0), (2, -1), 0.35, colors.HexColor(RULE)))
        table.setStyle(TableStyle(style))
        return table

    def data_table(self, columns, rows, *, width=None, right_align=()):
        """Bordered table with a shaded header row (no header when ``columns`` is empty)."""
        from reportlab.lib import colors
        from reportlab.platypus import Table
        from reportlab.platypus import TableStyle

        width = width or self.width
        n = max([len(columns), *(len(r) for r in rows)] or [1])
        small = n > 8
        cell_style = self.S.cell_small if small else self.S.cell
        head_style = self.S.table_head_small if small else self.S.table_head
        lengths = [len(str(columns[i])) if i < len(columns) else 0 for i in range(n)]
        for r in rows[:200]:
            for i in range(n):
                if i < len(r):
                    lengths[i] = max(lengths[i], min(len(str(r[i])), 40))
        shares = [max(min(length, 40), 4) for length in lengths]
        total = sum(shares)
        widths = [width * s / total for s in shares]
        data = []
        if columns:
            data.append([self.p(columns[i] if i < len(columns) else "", head_style) for i in range(n)])
        for r in rows:
            data.append([
                self.p(r[i] if i < len(r) and str(r[i]).strip() else "—",
                       self.S.cell_right if i in right_align else cell_style)
                for i in range(n)
            ])
        table = Table(data, colWidths=widths, repeatRows=1 if columns else 0)
        style = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor(RULE)),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]
        first_body = 1 if columns else 0
        if columns:
            style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(TABLE_HEAD_BG)))
            style.append(("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(BRAND)))
        if len(data) > first_body:
            style.append(("ROWBACKGROUNDS", (0, first_body), (-1, -1), [colors.white, colors.HexColor(STRIPE_BG)]))
        table.setStyle(TableStyle(style))
        return table

    # -- booking card ------------------------------------------------------

    def band(self, row):
        from reportlab.lib import colors
        from reportlab.platypus import Table
        from reportlab.platypus import TableStyle

        badge_w = 4.6 * 28.35
        left = [self.p(row.get("booking_id") or "", self.S.band_id), self.p(row.get("equipment") or "", self.S.band_sub)]
        band = Table(
            [[left, self.badge(row.get("status_code") or "", row.get("status") or "")]],
            colWidths=[self.width - badge_w, badge_w],
            cornerRadii=[5, 5, 0, 0],
        )
        band.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(BRAND)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        return band

    def set_title(self, text: str):
        from reportlab.lib import colors
        from reportlab.platypus import Table
        from reportlab.platypus import TableStyle

        strip = Table([[self.p(text, self.S.set_title)]], colWidths=[self.width])
        strip.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(SET_BG)),
            ("LINEBEFORE", (0, 0), (0, -1), 2.2, colors.HexColor(BRAND)),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        return strip

    def input_block(self, fields, set_index: int, extra_pairs=()) -> list:
        """One sample set's inputs: label / value rows, with table inputs as their own bordered tables."""
        from reportlab.platypus import KeepTogether
        from reportlab.platypus import Spacer

        flow: list = []
        pairs: list[tuple[str, str]] = []

        def flush():
            if pairs:
                flow.append(self.kv_table(list(pairs)))
                flow.append(Spacer(1, 3))
                pairs.clear()

        for item in fields:
            value = item.values[set_index] if set_index < len(item.values) else {"kind": "empty"}
            kind = value.get("kind")
            if kind == "text":
                pairs.append((item.label, value.get("text") or ""))
            elif kind == "table":
                flush()
                table = self.data_table(list(value.get("columns") or []), [list(r) for r in value.get("rows") or []])
                flow.append(KeepTogether([self.p(item.label, self.S.table_label), table, Spacer(1, 4)]))
        pairs.extend(extra_pairs)
        flush()
        return flow

    def card(self, row, columns) -> list:
        from reportlab.platypus import Spacer

        from .booking_export_details import BookingDetail

        detail: BookingDetail | None = row.get("detail")
        flow: list = [self.band(row), Spacer(1, 4)]
        pairs = []
        for col in columns:
            if col.key in _BAND_KEYS:
                continue
            value = row.get(col.key)
            text = _money_text(value, self.fonts) if col.kind == "money" else _cell_text(col, value)
            if text:
                label = col.label.replace(" (₹)", "")
                pairs.append((label, text))
        if pairs:
            flow.append(self.kv_table(pairs, columns=2))

        if detail is None:
            flow.append(Spacer(1, 4))
            flow.append(self.p("Waitlist entry: no booking details until a slot is confirmed.", self.S.note))
            return flow

        extras = [("Atmosphere-sensitive sample", "Yes (submit at slot start)" if detail.atmosphere_sensitive else "No")]
        if detail.comments:
            extras.append(("Any other requirements", detail.comments))
        title = "Sample information"
        if detail.sets > 1:
            title += f" · {detail.sets} sample sets"
        flow.append(self.section(title))
        flow.append(Spacer(1, 3))
        if not detail.fields and not detail.comments:
            flow.append(self.p("No user inputs were recorded for this booking.", self.S.note))
            flow.append(Spacer(1, 3))
            flow.append(self.kv_table(extras[:1]))
        elif detail.sets > 1:
            for index in range(detail.sets):
                flow.append(self.set_title(f"Sample set {index + 1}"))
                flow.append(Spacer(1, 3))
                flow.extend(self.input_block(detail.fields, index))
            flow.append(self.kv_table(extras))
        else:
            flow.extend(self.input_block(detail.fields, 0, extras))

        if detail.files:
            flow.append(self.section("Uploaded files"))
            flow.append(Spacer(1, 3))
            flow.append(self.data_table(
                ["#", "File name", "Part", "Material", "Quantity"],
                [[str(i), f.name, f.part, f.material, str(f.quantity)] for i, f in enumerate(detail.files, start=1)],
                right_align=(4,),
            ))
        if self.charges and detail.charges:
            flow.append(self.section("Charges"))
            flow.append(Spacer(1, 3))
            flow.append(self.charges_table(detail.charges, row.get("amount")))
        return flow

    def charges_table(self, lines, total):
        from reportlab.lib import colors
        from reportlab.platypus import Table
        from reportlab.platypus import TableStyle

        amount_w = 3.4 * 28.35
        rupee = "₹" if self.fonts.unicode else "Rs."
        data = [[self.p("Description", self.S.table_head), self.p(f"Amount ({rupee})", self.S.table_head)]]
        for description, amount in lines:
            data.append([
                self.p(description or "Charge", self.S.cell),
                self.p("" if amount is None else f"{amount:,.2f}", self.S.cell_right),
            ])
        if total is not None:
            data.append([
                self.p("Total charged", self.S.cell_bold),
                self.p(f"{total:,.2f}", self.S.cell_bold_right),
            ])
        table = Table(data, colWidths=[self.width - amount_w, amount_w])
        style = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor(RULE)),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(TABLE_HEAD_BG)),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(BRAND)),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]
        if total is not None:
            style += [
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor(LABEL_BG)),
                ("LINEABOVE", (0, -1), (-1, -1), 0.9, colors.HexColor(BRAND)),
            ]
        table.setStyle(TableStyle(style))
        return table

    # -- cover -------------------------------------------------------------

    def summary_table(self, rows, columns) -> list:
        from reportlab.lib import colors
        from reportlab.platypus import Table
        from reportlab.platypus import TableStyle

        keys = {c.key for c in columns}
        rupee = "₹" if self.fonts.unicode else "Rs."
        spec = [("sno", "#", 0.45), ("booking_id", "Booking ID", 2.2), ("equipment", "Equipment", 2.3)]
        if "department" in keys:
            spec.append(("user", "User", 1.8))
        spec += [("slot_dates", "Slot date(s)", 1.55), ("status", "Status", 1.55), ("samples", "Samples", 1.05)]
        if "amount" in keys:
            spec.append(("amount", f"Amount ({rupee})", 1.05))
        total = sum(s for _, _, s in spec)
        widths = [self.width * s / total for _, _, s in spec]
        header = [self.p(label, self.S.head) for _, label, _ in spec]
        style = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(BRAND)),
            ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor(RULE)),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(RULE)),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor(STRIPE_BG)]),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 3.5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3.5),
        ]
        out = []
        # Large tables split slowly in reportlab; chunks keep rendering linear and still repeat the header.
        for start in range(0, len(rows), 200):
            data = [header]
            for row in rows[start:start + 200]:
                cells = []
                for key, _, _ in spec:
                    value = row.get(key)
                    if key == "amount":
                        cells.append(self.p("" if value is None else f"{value:,.2f}", self.S.cell_right))
                    elif key == "status":
                        fg, _bg = _STATUS_COLOURS.get(row.get("status_code") or "", _DEFAULT_STATUS_COLOURS)
                        cells.append(self.p(value or "", self.S.cell_bold.clone("exp_status_c",
                                                                                textColor=colors.HexColor(fg))))
                    else:
                        cells.append(self.p("" if value is None else value, self.S.cell))
                data.append(cells)
            table = Table(data, colWidths=widths, repeatRows=1)
            table.setStyle(TableStyle(style))
            out.append(table)
        return out

    def cover_panels(self, rows, *, summary, generated_at, view_label, status_counts):
        from reportlab.lib import colors
        from reportlab.platypus import Table
        from reportlab.platypus import TableStyle

        gap = 0.5 * 28.35
        left_w = self.width * 0.6 - gap / 2
        right_w = self.width - left_w - gap
        details = [("Generated at", f"{generated_at} IST"), ("Exported from", view_label),
                   ("Bookings", f"{len(rows):,}")]
        if self.charges:
            amounts = [r.get("amount") for r in rows if r.get("amount") is not None]
            if amounts:
                details.append(("Total amount", _money_text(sum(amounts), self.fonts)))
        details += list(summary)

        def panel(title, data_rows, widths, label_style, value_style, value_align="LEFT"):
            data = [[self.p(title, self.S.panel_title), ""]]
            data += [[self.p(a, label_style), self.p(b, value_style)] for a, b in data_rows]
            table = Table(data, colWidths=widths)
            table.setStyle(TableStyle([
                ("SPAN", (0, 0), (-1, 0)),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(TABLE_HEAD_BG)),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(BRAND)),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(RULE)),
                ("LINEBELOW", (0, 1), (-1, -2), 0.35, colors.HexColor(RULE)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 1), (1, -1), value_align),
                ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]))
            return table

        left = panel("Export details", details, [left_w * 0.34, left_w * 0.66], self.S.label, self.S.value)
        status_rows = [[self.badge(code, label), self.p(f"{count:,}", self.S.count)] for code, label, count in
                       status_counts]
        right_data = [[self.p("Bookings by status", self.S.panel_title), ""], *status_rows]
        if not status_rows:
            right_data.append([self.p("No bookings", self.S.note), ""])
        right = Table(right_data, colWidths=[right_w * 0.72, right_w * 0.28])
        right.setStyle(TableStyle([
            ("SPAN", (0, 0), (-1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(TABLE_HEAD_BG)),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(BRAND)),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(RULE)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 1), (0, -1), "LEFT"),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        for item in status_rows:
            item[0].hAlign = "LEFT"
        outer = Table([[left, "", right]], colWidths=[left_w, gap, right_w])
        outer.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        return outer


def _numbered_canvas(fonts: Fonts, generated_at: str):
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas as rl_canvas

    class NumberedCanvas(rl_canvas.Canvas):
        """Page header (from page 2) and footer with "Page X of Y", drawn once the page count is known."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_pages = []

        def showPage(self):
            self._saved_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_pages)
            for state in self._saved_pages:
                self.__dict__.update(state)
                width, height = self._pagesize
                left, right = 1.6 * cm, width - 1.6 * cm
                self.setStrokeColor(colors.HexColor(RULE))
                self.setLineWidth(0.6)
                if self._pageNumber > 1:
                    self.setFont(fonts.bold, 8)
                    self.setFillColor(colors.HexColor(BRAND))
                    self.drawString(left, height - 1.15 * cm, "IIC, IIT Roorkee · Bookings" if fonts.unicode
                                    else "IIC, IIT Roorkee - Bookings")
                    self.setFont(fonts.regular, 7.5)
                    self.setFillColor(colors.HexColor(MUTED))
                    self.drawRightString(right, height - 1.15 * cm, f"Generated {generated_at} IST")
                    self.line(left, height - 1.35 * cm, right, height - 1.35 * cm)
                self.line(left, 1.25 * cm, right, 1.25 * cm)
                self.setFont(fonts.regular, 7.5)
                self.setFillColor(colors.HexColor(MUTED))
                self.drawString(left, 0.8 * cm, PORTAL_HEADER)
                self.drawRightString(right, 0.8 * cm, f"Page {self._pageNumber} of {total}")
                super().showPage()
            super().save()

    return NumberedCanvas


def render_pdf(columns, rows, *, summary, generated_at, status_counts=(), view_label="", charges=False) -> bytes:
    from django.conf import settings
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import KeepTogether
    from reportlab.platypus import PageBreak
    from reportlab.platypus import SimpleDocTemplate
    from reportlab.platypus import Spacer

    from .document_exports import _pdf_letterhead_story_lines
    from .models import BookingStatus

    fonts = register_fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.6 * cm,
        rightMargin=1.6 * cm,
        topMargin=1.8 * cm,
        bottomMargin=1.7 * cm,
        title="Bookings",
        author=PORTAL_HEADER,
        subject=f"{view_label} export" if view_label else "Bookings export",
    )
    b = _Builder(fonts, doc.width, charges)
    S = b.S

    label_to_code = {str(label): code for code, label in BookingStatus.choices}
    counts = [(label_to_code.get(label, ""), label, count) for label, count in status_counts]

    dept = getattr(settings, "ORG_DEPARTMENT_NAME", "") or "Institute Instrumentation Centre (IIC)"
    story = list(_pdf_letterhead_story_lines(department_name=dept))
    story.append(Spacer(1, 0.2 * cm))
    story.append(b.p("Bookings", S.title))
    noun = "booking" if len(rows) == 1 else "bookings"
    story.append(b.p(f"{view_label} · {len(rows):,} {noun}" if view_label else f"{len(rows):,} {noun}", S.subtitle))
    story.append(Spacer(1, 0.55 * cm))
    story.append(b.cover_panels(rows, summary=summary, generated_at=generated_at, view_label=view_label,
                                status_counts=counts))
    story.append(Spacer(1, 0.6 * cm))
    story.append(b.p("Summary", S.h2))
    if rows:
        story.extend(b.summary_table(rows, columns))
    else:
        story.append(b.p("No bookings match these filters.", S.note))

    if rows:
        story.append(PageBreak())
        story.append(b.p("Booking details", S.h2))
        for row in rows:
            story.append(KeepTogether(b.card(row, columns)))
            story.append(Spacer(1, 0.55 * cm))

    doc.build(story, canvasmaker=_numbered_canvas(fonts, generated_at))
    return buf.getvalue()
