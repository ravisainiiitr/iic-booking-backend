"""Human-readable, unique, concurrency-safe document numbers: ``PREFIX/FY/NNNNN`` (e.g. ``REQ/2026-27/00001``).

One counter row per (prefix, FY) is locked with ``SELECT ... FOR UPDATE`` inside the caller's transaction,
so two concurrent requests can never receive the same number and numbers are never reused.
"""

from __future__ import annotations

from datetime import date

from django.db import IntegrityError, transaction

from .fy import fy_label
from .models import NumberSequence


def next_number(prefix: str, on_date: date | None = None, financial_year: str | None = None) -> str:
    fy = financial_year or fy_label(on_date)
    with transaction.atomic():
        row = NumberSequence.objects.select_for_update().filter(prefix=prefix, financial_year=fy).first()
        if row is None:
            try:
                with transaction.atomic():
                    NumberSequence.objects.create(prefix=prefix, financial_year=fy, last_value=0)
            except IntegrityError:
                pass
            row = NumberSequence.objects.select_for_update().get(prefix=prefix, financial_year=fy)
        row.last_value += 1
        row.save(update_fields=["last_value"])
        return f"{prefix}/{fy}/{row.last_value:05d}"
