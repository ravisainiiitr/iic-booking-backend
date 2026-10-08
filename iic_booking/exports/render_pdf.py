"""PDF report: IIT Roorkee letterhead, title, filters, KPI cards and zebra-striped tables.

A4 portrait, or landscape when a table is wide. Every page after the first carries a slim running header;
every page has a footer with the generation time and "Page X of Y". Text uses Noto Sans (₹) with
Devanagari runs in Noto Sans Devanagari.
"""

from __future__ import annotations

import io

from . import spec
from .branding import ACCENT_HEX
from .branding import BRAND_HEX
from .branding import INK_HEX
from .branding import MUTED_HEX
from .branding import PORTAL_HOST
from .branding import PORTAL_LINE
from .branding import department_line
from .branding import masthead_path
from .fonts import Fonts
from .fonts import markup
from .fonts import needs_markup
from .fonts import plain
from .fonts import register_fonts
from .fonts import rupee
from .values import display_text
from .values import raw_value
from .values import to_number

_CHUNK = 250
_LANDSCAPE_WIDTH_UNITS = 9.0


def _fit_lines(text: str, font: str, size: float, width: float) -> str:
    """Word-wrap ``text`` to ``width`` points, breaking single words (e.g. emails) that are still too wide."""
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfbase.pdfmetrics import stringWidth

    lines = []
    for paragraph in text.split("\n"):
        for line in simpleSplit(paragraph, font, size, width) or [""]:
            while len(line) > 1 and stringWidth(line, font, size) > width:
                cut = len(line) - 1
                while cut > 1 and stringWidth(line[:cut], font, size) > width:
                    cut -= 1
                lines.append(line[:cut])
                line = line[cut:]
            lines.append(line)
    return "\n".join(lines)


def _wants_landscape(document: spec.Document) -> bool:
    if document.landscape is not None:
        return document.landscape
    return any(
        len(t.columns) > 7 or sum(c.width for c in t.columns) > _LANDSCAPE_WIDTH_UNITS for t in document.tables
    )


class _Styles:
    def __init__(self, fonts: Fonts, body_size: float):
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.styles import ParagraphStyle

        brand = colors.HexColor(f"#{BRAND_HEX}")
        muted = colors.HexColor(f"#{MUTED_HEX}")
        ink = colors.HexColor(f"#{INK_HEX}")
        self.dept = ParagraphStyle("x_dept", fontName=fonts.bold, fontSize=12, leading=15, alignment=TA_CENTER,
                                   textColor=brand)
        self.title = ParagraphStyle("x_title", fontName=fonts.bold, fontSize=16, leading=20, alignment=TA_CENTER,
                                    textColor=ink, spaceBefore=2)
        self.subtitle = ParagraphStyle("x_sub", fontName=fonts.regular, fontSize=9.5, leading=12,
                                       alignment=TA_CENTER, textColor=muted)
        self.meta = ParagraphStyle("x_meta", fontName=fonts.regular, fontSize=8, leading=10, alignment=TA_CENTER,
                                   textColor=muted)
        self.filter = ParagraphStyle("x_filter", fontName=fonts.regular, fontSize=8, leading=10, textColor=ink,
                                     alignment=TA_LEFT)
        self.section = ParagraphStyle("x_section", fontName=fonts.bold, fontSize=11.5, leading=14, textColor=brand,
                                      spaceBefore=10, spaceAfter=4, keepWithNext=1)
        self.note = ParagraphStyle("x_note", fontName=fonts.regular, fontSize=7.5, leading=9.5, textColor=muted,
                                   spaceBefore=3)
        self.empty = ParagraphStyle("x_empty", fontName=fonts.regular, fontSize=8.5, leading=11, textColor=muted,
                                    spaceBefore=2, spaceAfter=4)
        self.kpi_label = ParagraphStyle("x_kpi_l", fontName=fonts.regular, fontSize=7, leading=9, textColor=muted)
        self.kpi_value = ParagraphStyle("x_kpi_v", fontName=fonts.bold, fontSize=13, leading=16, textColor=brand)
        self.kpi_hint = ParagraphStyle("x_kpi_h", fontName=fonts.regular, fontSize=6.5, leading=8, textColor=muted)
        self.cell = ParagraphStyle("x_cell", fontName=fonts.regular, fontSize=body_size, leading=body_size + 1.8,
                                   textColor=ink)


def _letterhead(document: spec.Document, styles: _Styles, fonts: Fonts, width: float, generated_at: str) -> list:
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import HRFlowable
    from reportlab.platypus import Image
    from reportlab.platypus import Paragraph
    from reportlab.platypus import Spacer

    story = []
    path = masthead_path()
    if path:
        iw, ih = ImageReader(path).getSize()
        draw_w = 5.6 * cm
        story.append(Image(path, width=draw_w, height=draw_w * ih / iw, hAlign="CENTER"))
        story.append(Spacer(1, 0.1 * cm))
    else:
        story.append(Paragraph(markup("Indian Institute of Technology Roorkee", fonts), styles.title))
    story.append(Paragraph(markup(document.department or department_line(), fonts), styles.dept))
    story.append(Paragraph(markup(document.title, fonts), styles.title))
    if document.subtitle:
        story.append(Paragraph(markup(document.subtitle, fonts), styles.subtitle))
    story.append(Spacer(1, 0.15 * cm))
    story.append(HRFlowable(width="100%", thickness=1.2, color=colors.HexColor(f"#{BRAND_HEX}"), spaceAfter=4))
    rows = document.row_count
    meta = [f"Generated {generated_at} IST"]
    if document.generated_by:
        meta.append(f"by {document.generated_by}")
    meta.append(f"{rows:,} record{'s' if rows != 1 else ''}")
    story.append(Paragraph(markup(" · ".join(meta), fonts), styles.meta))
    story.append(Spacer(1, 0.2 * cm))
    return story


def _filters_block(document: spec.Document, styles: _Styles, fonts: Fonts, width: float) -> list:
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph
    from reportlab.platypus import Spacer
    from reportlab.platypus import Table
    from reportlab.platypus import TableStyle

    pairs = document.filters or [("Filters", "None (all records you can see)")]
    per_row = 3 if width > 20 * cm else 2
    cells = [
        Paragraph(f'<font name="{fonts.bold}">{markup(label, fonts)}:</font> {markup(value, fonts)}', styles.filter)
        for label, value in pairs
    ]
    while len(cells) % per_row:
        cells.append("")
    data = [cells[i:i + per_row] for i in range(0, len(cells), per_row)]
    table = Table(data, colWidths=[width / per_row] * per_row)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ROUNDEDCORNERS", [3, 3, 3, 3]),
    ]))
    return [table, Spacer(1, 0.3 * cm)]


def _kpi_cards(document: spec.Document, styles: _Styles, fonts: Fonts, width: float) -> list:
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph
    from reportlab.platypus import Spacer
    from reportlab.platypus import Table
    from reportlab.platypus import TableStyle

    per_row = 5 if width > 20 * cm else 4
    gap = 0.25 * cm
    card_w = (width - gap * (per_row - 1)) / per_row
    out = []
    kpis = document.kpis
    for start in range(0, len(kpis), per_row):
        row = []
        widths = []
        for i, kpi in enumerate(kpis[start:start + per_row]):
            if i:
                row.append("")
                widths.append(gap)
            value = display_text(kpi.value, kpi.type, rupee=rupee(fonts)) or "—"
            content = [
                Paragraph(markup(kpi.label.upper(), fonts), styles.kpi_label),
                Paragraph(markup(value, fonts), styles.kpi_value),
            ]
            if kpi.hint:
                content.append(Paragraph(markup(kpi.hint, fonts), styles.kpi_hint))
            card = Table([[content]], colWidths=[card_w])
            card.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(f"#{ACCENT_HEX}")),
                ("LINEBEFORE", (0, 0), (0, -1), 2.5, colors.HexColor(f"#{BRAND_HEX}")),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            row.append(card)
            widths.append(card_w)
        grid = Table([row], colWidths=widths, hAlign="LEFT")
        grid.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        out.extend([grid, Spacer(1, gap)])
    out.append(Spacer(1, 0.15 * cm))
    return out


def _data_table(document, table: spec.Table, styles: _Styles, fonts: Fonts, width: float, body_size: float) -> list:
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph
    from reportlab.platypus import Table
    from reportlab.platypus import TableStyle

    story = []
    if len(document.tables) > 1 or table.title != document.title:
        count = len(table.rows)
        story.append(Paragraph(
            f'{markup(table.title, fonts)} <font size="8" color="#{MUTED_HEX}">'
            f'· {count:,} row{"s" if count != 1 else ""}</font>',
            styles.section,
        ))
    columns = table.columns
    if not table.rows or not columns:
        story.append(Paragraph(markup(table.empty_message, fonts), styles.empty))
        return story

    share = sum(c.width for c in columns) or 1
    col_widths = [width * c.width / share for c in columns]
    pad = 3

    def cell(text: str, col_width: float, font: str):
        if needs_markup(text, fonts):
            return Paragraph(markup(text, fonts), styles.cell)
        return _fit_lines(plain(text, fonts), font, body_size, col_width - 2 * pad)

    header = [cell(c.header, w, fonts.bold) for c, w in zip(columns, col_widths)]
    numeric = [i for i, c in enumerate(columns) if c.type in spec.NUMERIC_TYPES]
    totals = {i: 0.0 for i, c in enumerate(columns) if c.total}

    base_style = [
        ("FONTNAME", (0, 0), (-1, -1), fonts.regular),
        ("FONTSIZE", (0, 0), (-1, -1), body_size),
        ("LEADING", (0, 0), (-1, -1), body_size + 1.8),
        ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor(f"#{INK_HEX}")),
        ("FONTNAME", (0, 0), (-1, 0), fonts.bold),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(f"#{BRAND_HEX}")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("VALIGN", (0, 1), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7FB")]),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("LEFTPADDING", (0, 0), (-1, -1), pad),
        ("RIGHTPADDING", (0, 0), (-1, -1), pad),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
    ]
    for i in numeric:
        base_style.append(("ALIGN", (i, 0), (i, -1), "RIGHT"))

    rows = table.rows
    for start in range(0, len(rows), _CHUNK):
        data = [header]
        for row in rows[start:start + _CHUNK]:
            out = []
            for index, (column, col_width) in enumerate(zip(columns, col_widths)):
                raw = raw_value(row, column)
                if index in totals:
                    number = to_number(raw)
                    if number is not None:
                        totals[index] += number
                out.append(cell(display_text(raw, column.type, rupee=rupee(fonts)), col_width, fonts.regular))
            data.append(out)
        style = list(base_style)
        if totals and start + _CHUNK >= len(rows):
            total_row = []
            for index, (column, col_width) in enumerate(zip(columns, col_widths)):
                if index == 0:
                    total_row.append("Total")
                elif index in totals:
                    total_row.append(display_text(totals[index], column.type, rupee=rupee(fonts)))
                else:
                    total_row.append("")
            data.append(total_row)
            style += [
                ("FONTNAME", (0, -1), (-1, -1), fonts.bold),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor(f"#{ACCENT_HEX}")),
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor(f"#{BRAND_HEX}")),
            ]
        grid = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
        grid.setStyle(TableStyle(style))
        story.append(grid)
    if table.note:
        story.append(Paragraph(markup(table.note, fonts), styles.note))
    return story


def render_pdf(document: spec.Document, *, generated_at: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.pagesizes import landscape
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import SimpleDocTemplate

    fonts = register_fonts()
    is_landscape = _wants_landscape(document)
    widest = max((len(t.columns) for t in document.tables), default=0)
    body_size = 7.5 if widest <= 8 else (7 if widest <= 12 else 6.5)
    styles = _Styles(fonts, body_size)
    running_title = plain(document.title, fonts)
    footer_left = plain(f"{PORTAL_LINE} · Generated {generated_at} IST", fonts)

    class _NumberedCanvas(rl_canvas.Canvas):
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
                page_w, page_h = self._pagesize
                muted = colors.HexColor(f"#{MUTED_HEX}")
                rule = colors.HexColor("#CBD5E1")
                if self._pageNumber > 1:
                    self.setFont(fonts.bold, 8)
                    self.setFillColor(colors.HexColor(f"#{BRAND_HEX}"))
                    self.drawString(1.2 * cm, page_h - 0.8 * cm, running_title[:110])
                    self.setFont(fonts.regular, 7.5)
                    self.setFillColor(muted)
                    self.drawRightString(page_w - 1.2 * cm, page_h - 0.8 * cm, plain("IIC · IIT Roorkee", fonts))
                    self.setStrokeColor(rule)
                    self.setLineWidth(0.5)
                    self.line(1.2 * cm, page_h - 0.95 * cm, page_w - 1.2 * cm, page_h - 0.95 * cm)
                self.setStrokeColor(rule)
                self.setLineWidth(0.5)
                self.line(1.2 * cm, 1.05 * cm, page_w - 1.2 * cm, 1.05 * cm)
                self.setFont(fonts.regular, 7)
                self.setFillColor(muted)
                self.drawString(1.2 * cm, 0.65 * cm, footer_left)
                if page_w > 25 * cm:
                    self.drawCentredString(page_w / 2, 0.65 * cm, PORTAL_HOST)
                self.drawRightString(page_w - 1.2 * cm, 0.65 * cm, f"Page {self._pageNumber} of {total}")
                super().showPage()
            super().save()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4) if is_landscape else A4,
        leftMargin=1.2 * cm,
        rightMargin=1.2 * cm,
        topMargin=1.25 * cm,
        bottomMargin=1.4 * cm,
        title=document.title,
        author=PORTAL_LINE,
        subject=document.subtitle or document.title,
    )
    width = doc.width
    story = _letterhead(document, styles, fonts, width, generated_at)
    story += _filters_block(document, styles, fonts, width)
    if document.kpis:
        story += _kpi_cards(document, styles, fonts, width)
    for table in document.tables:
        story += _data_table(document, table, styles, fonts, width, body_size)
    doc.build(story, canvasmaker=_NumberedCanvas)
    return buf.getvalue()
