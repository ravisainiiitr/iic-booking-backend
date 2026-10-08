"""Shared look of exported reports: IIT Roorkee crest masthead, brand colours and portal line."""

from __future__ import annotations

from iic_booking.equipment.export_styles import INK
from iic_booking.equipment.export_styles import MUTED
from iic_booking.equipment.export_styles import XLSX_BRAND

BRAND_HEX = XLSX_BRAND
ACCENT_HEX = "E8EEF7"
INK_HEX = INK.lstrip("#").upper()
MUTED_HEX = MUTED.lstrip("#").upper()
PORTAL_LINE = "Institute Instrumentation Centre (IIC), IIT Roorkee"
PORTAL_HOST = "equip.iitr.ac.in"


def masthead_path() -> str | None:
    """Crest masthead used on invoices and Proforma Invoices (``fonts/iitr-pdf-masthead.png``), else the logo."""
    from iic_booking.equipment.document_exports import _pdf_masthead_path

    return _pdf_masthead_path()


def department_line() -> str:
    from django.conf import settings

    return (getattr(settings, "ORG_DEPARTMENT_NAME", "") or "").strip() or "Institute Instrumentation Centre (IIC)"
