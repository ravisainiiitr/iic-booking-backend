"""Invoice numbers for the sandbox demonstration: PREFIX/YY-YY/NNNN (e.g. EBP/26-27/0001), one series per financial
year, allocated inside a row lock so concurrent approvals never share a number. Draft invoices carry a draft reference
instead (DRAFT-xxxxxx), so a draft that is rejected or abandoned never consumes a number.

The series is a proposal for demonstration only: it is separate from, and never consumes, SRIC's own invoice series.
"""

from __future__ import annotations

import secrets
from datetime import date

from django.db import transaction

from .models import EInvoice, InvoiceSeries

DEMO_PREFIX = "EBP"


def financial_year(day: date) -> str:
    """Indian financial year (April-March) as '26-27'."""
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def new_draft_reference() -> str:
    while True:
        ref = f"DRAFT-{secrets.token_hex(3).upper()}"
        if not EInvoice.objects.filter(draft_reference=ref).exists():
            return ref


def format_number(series: InvoiceSeries, n: int) -> str:
    return f"{series.prefix}/{series.financial_year}/{n:0{series.width}d}"


def allocate(day: date, *, prefix: str = DEMO_PREFIX) -> tuple[InvoiceSeries, str]:
    """(series, number). Must run inside the caller's transaction; the series row is locked until it commits."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Invoice numbers are allocated inside a transaction.")
    fy = financial_year(day)
    InvoiceSeries.objects.get_or_create(prefix=prefix, financial_year=fy, mode="sandbox")
    series = InvoiceSeries.objects.select_for_update().get(prefix=prefix, financial_year=fy, mode="sandbox")
    series.last_number += 1
    series.save(update_fields=["last_number"])
    number = format_number(series, series.last_number)
    if len(number) > 16:  # the IRP's DocDtls.No limit
        raise ValueError(f"Invoice number {number} is longer than the 16 characters the IRP accepts.")
    return series, number
