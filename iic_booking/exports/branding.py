"""Shared look of exported reports: IIT Roorkee crest masthead, brand colour and portal line."""

from __future__ import annotations

BRAND_HEX = "153F79"
ACCENT_HEX = "E8EEF7"
INK_HEX = "1E293B"
MUTED_HEX = "64748B"
PORTAL_LINE = "Institute Instrumentation Centre (IIC), IIT Roorkee"
PORTAL_HOST = "equip.iitr.ac.in"


def masthead_path() -> str | None:
    """Crest masthead used on invoices and Proforma Invoices (``fonts/iitr-pdf-masthead.png``), else the logo."""
    from iic_booking.equipment.document_exports import _pdf_masthead_path

    return _pdf_masthead_path()


def department_line() -> str:
    from django.conf import settings

    return (getattr(settings, "ORG_DEPARTMENT_NAME", "") or "").strip() or "Institute Instrumentation Centre (IIC)"
