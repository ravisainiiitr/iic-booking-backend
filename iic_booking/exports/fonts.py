"""PDF fonts of report exports: the shared registration of :mod:`iic_booking.equipment.export_styles`.

Noto Sans (Latin and ₹) and Noto Sans Devanagari vendored under ``BASE_DIR/fonts`` (SIL OFL 1.1), registered
once for both the View Booking export and these reports; DejaVu, then Helvetica with "Rs." otherwise.
"""

from __future__ import annotations

from iic_booking.equipment.export_styles import _ASCII_FALLBACK
from iic_booking.equipment.export_styles import _DEVANAGARI_RUN as DEVANAGARI_RUN
from iic_booking.equipment.export_styles import Fonts
from iic_booking.equipment.export_styles import register_fonts

__all__ = ["DEVANAGARI_RUN", "Fonts", "markup", "needs_markup", "plain", "register_fonts", "rupee"]

_EXTRA_FALLBACK = {"’": "'", "“": '"', "”": '"'}


def rupee(fonts: Fonts) -> str:
    return "₹" if fonts.unicode else "Rs."


def plain(text, fonts: Fonts) -> str:
    text = "" if text is None else str(text)
    if not fonts.unicode:
        for char, replacement in {**_ASCII_FALLBACK, **_EXTRA_FALLBACK}.items():
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
