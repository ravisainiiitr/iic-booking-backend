"""Bookings export as an A4 report.

Cover page (IIT Roorkee letterhead, filters, counts by status), a summary table of every booking, then a
details card per booking: header band with Booking ID, equipment and status, the user / slot details the
page shows for the viewer's role, every user input (tables as tables, one block per sample set), uploaded
file names and the charge breakdown when the viewer may see charges. Text uses the Noto Sans fonts in the
repository's ``fonts`` folder (₹ and Devanagari), falling back to Helvetica with "Rs." when they are missing.
"""

from __future__ import annotations

import io

from .booking_list_export import PORTAL_HEADER
from .booking_list_export import _cell_text
from .export_styles import BRAND
from .export_styles import DEFAULT_STATUS_COLOURS
from .export_styles import LABEL_BG
from .export_styles import MUTED
from .export_styles import RULE
from .export_styles import SET_BG
from .export_styles import STATUS_COLOURS
from .export_styles import STRIPE_BG
from .export_styles import LONG_CELL_TEXT
from .export_styles import TABLE_HEAD_BG
from .export_styles import Fonts
from .export_styles import link_markup
from .export_styles import markup
from .export_styles import money_text
from .export_styles import pdf_styles
from .export_styles import register_fonts

# Card fields that are already in the header band.
_BAND_KEYS = {"sno", "booking_id", "equipment", "status"}
# A table row cannot split across pages: longer values go in a paragraph, table cells are capped.
_LONG_TEXT = 500
_CELL_MAX_LINES = 50


class _Builder:
    def __init__(self, fonts: Fonts, width: float, charges: bool):
        self.fonts = fonts
        self.S = pdf_styles(fonts)
        self.width = width
        self.charges = charges

    def p(self, text, style, *, link: str = "", link_color: str | None = None):
        from reportlab.platypus import Paragraph

        text_markup = markup(text, self.fonts)
        if link:
            text_markup = link_markup(text_markup, link, **({"color": link_color} if link_color else {}))
        return Paragraph(text_markup, style)

    def cell_p(self, text, *, small: bool = False):
        """Table cell: centred, except longer free text which stays left-aligned."""
        if len(text) > LONG_CELL_TEXT:
            return self.p(text, self.S.cell_small if small else self.S.cell)
        return self.p(text, self.S.cell_small_center if small else self.S.cell_center)

    # -- small pieces ------------------------------------------------------

    def badge(self, status_code: str, text: str):
        from reportlab.lib import colors
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from reportlab.platypus import Paragraph
        from reportlab.platypus import Table
        from reportlab.platypus import TableStyle

        fg, bg = STATUS_COLOURS.get(status_code or "", DEFAULT_STATUS_COLOURS)
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
        heading.keepWithNext = True
        heading.spaceAfter = 3
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

    def data_table(self, columns, rows, *, width=None):
        """Bordered table with a shaded header row (no header when ``columns`` is empty); cells centred."""
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
        caps = [max(80, int(w / (cell_style.fontSize * 0.5) * _CELL_MAX_LINES)) for w in widths]

        def cell(r, i):
            text = str(r[i]) if i < len(r) and r[i] is not None and str(r[i]).strip() else "—"
            if len(text) > caps[i]:
                text = text[:caps[i]] + "… (shortened; full text in the Excel export)"
            return self.cell_p(text, small=small)

        data = []
        if columns:
            data.append([self.p(columns[i] if i < len(columns) else "", head_style) for i in range(n)])
        for r in rows:
            data.append([cell(r, i) for i in range(n)])
        table = Table(data, colWidths=widths, repeatRows=1 if columns else 0)
        style = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
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
        left = [
            self.p(row.get("booking_id") or "", self.S.band_id, link=row.get("link") or "", link_color="#ffffff"),
            self.p(row.get("equipment") or "", self.S.band_sub),
        ]
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
        strip.keepWithNext = True
        strip.spaceAfter = 3
        return strip

    def input_block(self, fields, set_index: int, extra_pairs=()) -> list:
        """One sample set's inputs: label / value rows, with table inputs as their own bordered tables."""
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
            if kind == "text" and len(value.get("text") or "") > _LONG_TEXT:
                flush()
                flow.extend([self.p(item.label, self.S.table_label), self.p(value["text"], self.S.value),
                             Spacer(1, 4)])
            elif kind == "text":
                pairs.append((item.label, value.get("text") or ""))
            elif kind == "table":
                flush()
                table = self.data_table(list(value.get("columns") or []), [list(r) for r in value.get("rows") or []])
                # No nested KeepTogether: inside the card's KeepTogether it would measure as endlessly tall.
                flow.extend([self.p(item.label, self.S.table_label), table, Spacer(1, 4)])
        for label, text in extra_pairs:
            if len(text) > _LONG_TEXT:
                flush()
                flow.extend([self.p(label, self.S.table_label), self.p(text, self.S.value), Spacer(1, 4)])
            else:
                pairs.append((label, text))
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
            text = money_text(value, self.fonts) if col.kind == "money" else _cell_text(col, value)
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
        if not detail.fields and not detail.comments:
            flow.append(self.p("No user inputs were recorded for this booking.", self.S.note))
            flow.append(Spacer(1, 3))
            flow.append(self.kv_table(extras[:1]))
        elif detail.sets > 1:
            for index in range(detail.sets):
                flow.append(self.set_title(f"Sample set {index + 1}"))
                flow.extend(self.input_block(detail.fields, index))
            flow.append(self.kv_table(extras))
        else:
            flow.extend(self.input_block(detail.fields, 0, extras))

        if detail.files:
            flow.append(self.section("Uploaded files"))
            flow.append(self.data_table(
                ["S.No.", "File name", "Part", "Material", "Quantity"],
                [[str(i), f.name, f.part, f.material, str(f.quantity)] for i, f in enumerate(detail.files, start=1)],
            ))
        if self.charges and detail.charges:
            flow.append(self.section("Charges"))
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
                self.cell_p(description or "Charge"),
                self.p("" if amount is None else f"{amount:,.2f}", self.S.cell_center),
            ])
        if total is not None:
            data.append([
                self.p("Total charged", self.S.cell_bold.clone("exp_cell_bold_c", alignment=1)),
                self.p(f"{total:,.2f}", self.S.cell_bold.clone("exp_cell_bold_amount_c", alignment=1)),
            ])
        table = Table(data, colWidths=[self.width - amount_w, amount_w], repeatRows=1)
        style = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor(RULE)),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(TABLE_HEAD_BG)),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(BRAND)),
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
        spec = [("sno", "S.No.", 0.55), ("booking_id", "Booking ID", 2.2), ("equipment", "Equipment", 2.3)]
        if "department" in keys:
            spec.append(("user", "User", 1.8))
        spec += [("slot_dates", "Slot date(s)", 1.55), ("status", "Status", 1.55), ("samples", "Samples", 1.05)]
        if "amount" in keys:
            spec.append(("amount", f"Amount ({rupee})", 1.05))
        total = sum(s for _, _, s in spec)
        widths = [self.width * s / total for _, _, s in spec]
        header = [self.p(label, self.S.head) for _, label, _ in spec]
        style = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(BRAND)),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor(RULE)),
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
                        cells.append(self.p("" if value is None else f"{value:,.2f}", self.S.cell_center))
                    elif key == "status":
                        fg, _bg = STATUS_COLOURS.get(row.get("status_code") or "", DEFAULT_STATUS_COLOURS)
                        cells.append(self.p(value or "", self.S.cell_bold.clone(
                            "exp_status_c", textColor=colors.HexColor(fg), alignment=1)))
                    elif key == "booking_id":
                        cells.append(self.p(value or "", self.S.cell_center, link=row.get("link") or ""))
                    else:
                        cells.append(self.p("" if value is None else value, self.S.cell_center))
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
                details.append(("Total amount", money_text(sum(amounts), self.fonts)))
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
