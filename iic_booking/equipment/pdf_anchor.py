"""Named destinations and outline entries for ReportLab story PDFs (internal links use ``#<key>``)."""

from __future__ import annotations

from reportlab.platypus.flowables import Flowable


class Anchor(Flowable):
    """Zero-size named destination at the current position, optionally listed in the PDF outline."""

    def __init__(self, key: str, title: str = "", level: int = 0, closed: bool = False):
        super().__init__()
        self.key = key
        self.title = title
        self.level = level
        self.closed = closed
        self.width = self.height = 0

    def wrap(self, *_args):
        return 0, 0

    def draw(self):
        self.canv.bookmarkHorizontal(self.key, 0, 10)
        if self.title:
            self.canv.addOutlineEntry(self.title, self.key, level=self.level, closed=self.closed or None)
            self.canv.showOutline()


def internal_link(text_markup: str, key: str, *, color: str = "#1d4ed8") -> str:
    """Paragraph markup linking ``text_markup`` (already escaped) to the destination ``key`` in this PDF."""
    return f'<link href="#{key}" color="{color}"><u>{text_markup}</u></link>'
