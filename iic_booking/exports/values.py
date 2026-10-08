"""Cell values: lookup, type coercion and display text shared by the CSV, Excel and PDF renderers."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from decimal import Decimal
from decimal import InvalidOperation
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.dateparse import parse_datetime

from iic_booking.equipment.export_styles import guard_formula  # noqa: F401

from . import spec

IST = ZoneInfo("Asia/Kolkata")


def lookup(row, key: str):
    value = row
    for part in key.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, (list, tuple)) and part.isdigit():
            index = int(part)
            value = value[index] if index < len(value) else None
        else:
            value = getattr(value, part, None)
        if value is None:
            return None
    return value


def raw_value(row, column: spec.Column):
    if column.value is not None:
        return column.value(row)
    return lookup(row, column.key)


def to_ist_datetime(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        parsed = parse_datetime(value.strip())
        if parsed is None:
            day = parse_date(value.strip())
            return datetime(day.year, day.month, day.day) if day else None
        value = parsed
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            return value
        return value.astimezone(IST).replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return None


def to_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return to_ist_datetime(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        day = parse_date(value.strip()[:10])
        if day is not None and len(value.strip()) > 10:
            moment = to_ist_datetime(value)
            return moment.date() if moment else day
        return day
    return None


def to_number(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        return float(Decimal(str(value).replace(",", "").replace("₹", "").strip()))
    except (InvalidOperation, ValueError):
        return None


def to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(to_text(v) for v in value if v not in (None, ""))
    if isinstance(value, dict):
        return "; ".join(f"{k}: {to_text(v)}" for k, v in value.items() if v not in (None, ""))
    if isinstance(value, datetime):
        moment = to_ist_datetime(value)
        return moment.strftime("%d-%m-%Y %H:%M") if moment else ""
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")
    return str(value).strip()


def typed_value(value, column_type: str):
    """Python value for an Excel cell (numbers stay numbers, dates become naive IST datetimes)."""
    if column_type == spec.DATETIME:
        moment = to_ist_datetime(value)
        return moment if moment is not None else (to_text(value) or None)
    if column_type == spec.DATE:
        day = to_date(value)
        return day if day is not None else (to_text(value) or None)
    if column_type in spec.NUMERIC_TYPES:
        number = to_number(value)
        if number is None:
            return to_text(value) or None
        if column_type == spec.INTEGER and float(number).is_integer():
            return int(number)
        return number
    if column_type == spec.BOOL:
        if value in (None, ""):
            return None
        return "Yes" if value is True or str(value).lower() in ("true", "1", "yes") else "No"
    return to_text(value) or None


def indian_grouping(number: float, decimals: int = 2) -> str:
    negative = number < 0
    text = f"{abs(number):.{decimals}f}"
    whole, _, frac = text.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    out = f"{whole}.{frac}" if decimals else whole
    return f"-{out}" if negative else out


def display_text(value, column_type: str, *, rupee: str = "₹", machine: bool = False) -> str:
    """Text for CSV (``machine``: plain numbers, ISO-like dates) or PDF (grouped, ₹, dd-Mon-yyyy)."""
    if value in (None, ""):
        return ""
    if column_type == spec.DATETIME:
        moment = to_ist_datetime(value)
        if moment is None:
            return to_text(value)
        return moment.strftime("%Y-%m-%d %H:%M" if machine else "%d %b %Y, %H:%M")
    if column_type == spec.DATE:
        day = to_date(value)
        if day is None:
            return to_text(value)
        return day.strftime("%Y-%m-%d" if machine else "%d %b %Y")
    if column_type in spec.NUMERIC_TYPES:
        number = to_number(value)
        if number is None:
            return to_text(value)
        if column_type == spec.PERCENT:
            return f"{number * 100:.1f}%"
        if column_type == spec.INTEGER and float(number).is_integer():
            return str(int(number)) if machine else indian_grouping(number, 0)
        if machine:
            return f"{number:.2f}"
        if column_type == spec.CURRENCY:
            return f"{rupee}{indian_grouping(number)}"
        return indian_grouping(number) if not float(number).is_integer() else indian_grouping(number, 0)
    if column_type == spec.BOOL:
        return typed_value(value, spec.BOOL) or ""
    return to_text(value)


def now_ist() -> datetime:
    return timezone.now().astimezone(IST)
