"""PDF fonts: Noto Sans (Latin and ₹) and Noto Sans Devanagari, vendored under ``BASE_DIR/fonts`` (SIL OFL 1.1).

Registered under the same reportlab names as the View Booking PDF export (``IICNotoSans``, ``IICNotoDeva``) so
both modules share one registration. Falls back to DejaVu / Segoe UI, then Helvetica with "Rs.".
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_DEVANAGARI_CHARS = "\u0900-\u097F\uA8E0-\uA8FF\u1CD0-\u1CFF"
DEVANAGARI_RUN = re.compile(f"[{_DEVANAGARI_CHARS}]+(?:[\\s\u200c\u200d]+[{_DEVANAGARI_CHARS}]+)*")
_ASCII_FALLBACK = {"₹": "Rs.", "–": "-", "—": "-", "·": "-", "×": "x", "…": "...", "’": "'", "“": '"', "”": '"'}


@dataclass(frozen=True)
class Fonts:
    regular: str
    bold: str
    devanagari: str | None
    unicode: bool

    @property
    def rupee(self) -> str:
        return "₹" if self.unicode else "Rs."


def register_fonts() -> Fonts:
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
            logger.warning("Report export: font %s not available in %s", regular_file, font_dir)
            return False
        pdfmetrics.registerFontFamily(name, normal=name, bold=f"{name}-Bold", italic=name, boldItalic=f"{name}-Bold")
        return True

    if family("IICNotoSans", "NotoSans-Regular.ttf", "NotoSans-Bold.ttf"):
        deva = "IICNotoDeva" if family(
            "IICNotoDeva", "NotoSansDevanagari-Regular.ttf", "NotoSansDevanagari-Bold.ttf",
        ) else None
        return Fonts("IICNotoSans", "IICNotoSans-Bold", deva, True)

    from iic_booking.equipment.document_exports import _register_pdf_rupee_font

    fallback = _register_pdf_rupee_font()
    if fallback:
        return Fonts(fallback, fallback, None, True)
    return Fonts("Helvetica", "Helvetica-Bold", None, False)


def plain(text, fonts: Fonts) -> str:
    text = "" if text is None else str(text)
    if not fonts.unicode:
        for char, replacement in _ASCII_FALLBACK.items():
            text = text.replace(char, replacement)
        text = text.encode("latin-1", "replace").decode("latin-1")
    return text


def needs_markup(text: str, fonts: Fonts) -> bool:
    return bool(fonts.devanagari) and bool(DEVANAGARI_RUN.search(text or ""))


def markup(text, fonts: Fonts) -> str:
    """Paragraph markup: escaped, Devanagari runs in the Devanagari font, newlines as line breaks."""
    text = plain(text, fonts)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if fonts.devanagari:
        text = DEVANAGARI_RUN.sub(lambda m: f'<font name="{fonts.devanagari}">{m.group(0)}</font>', text)
    return text.replace("\n", "<br/>")
