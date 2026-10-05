"""Shared API plumbing: the endpoint decorator, input parsing and pagination.

Money is always returned as strings (DRF's JSON encoder would turn ``Decimal`` into float).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.dateparse import parse_date
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import access
from . import constants as c
from .errors import ProcurementError

MAX_PAGE_SIZE = 200
TWO = Decimal("0.01")
THREE = Decimal("0.001")


def pm_api(methods, *, require_module: bool = True, pilot_bootstrap: bool = False):
    """DRF function view with authentication, the per-request ``UserScope`` and error mapping.

    ``require_module`` refuses users with no enabled department (403 ``procurement_disabled``). Users outside the
    pilot get that same refusal from every endpoint; only the bootstrap (``pilot_bootstrap``) answers them, with the
    empty "disabled" payload."""

    def deco(fn):
        @wraps(fn)
        def inner(request, *args, **kwargs):
            scope = access.scope_for(request.user)
            request.pm_scope = scope
            try:
                if scope.blocked and not pilot_bootstrap:
                    raise ProcurementError(
                        "Procurement & Assets is not enabled for your account.", status=403, code=c.DISABLED_CODE
                    )
                if require_module:
                    scope.require_any_department()
                return fn(request, *args, **kwargs)
            except ProcurementError as exc:
                return Response({"detail": exc.message, "code": exc.code, **exc.extra}, status=exc.status)
            except DjangoValidationError as exc:
                detail = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
                return Response({"detail": detail, "code": "invalid"}, status=400)

        return api_view(methods)(permission_classes([IsAuthenticated])(inner))

    return deco


def data_of(request) -> dict:
    data = request.data
    if hasattr(data, "dict"):
        try:
            return data.dict()
        except Exception:
            pass
    return dict(data or {})


def req_str(data: dict, name: str, *, max_len: int = 255, required: bool = True, default: str = "") -> str:
    raw = data.get(name)
    value = "" if raw is None else str(raw).strip()
    if required and not value:
        raise ProcurementError(f"{name} is required.", code="required", field=name)
    if len(value) > max_len:
        raise ProcurementError(f"{name} is too long (max {max_len}).", code="too_long", field=name)
    return value or default


def req_reason(data: dict, name: str = "reason") -> str:
    value = str(data.get(name) or "").strip()
    if not value:
        raise ProcurementError("A reason is required.", code="reason_required", field=name)
    return value


def parse_money(value, name: str, *, required: bool = True, allow_zero: bool = True) -> Decimal | None:
    if value is None or value == "":
        if required:
            raise ProcurementError(f"{name} is required.", code="required", field=name)
        return None
    if isinstance(value, float):
        value = repr(value)
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProcurementError(f"{name} must be a number.", code="invalid_number", field=name)
    if not d.is_finite() or d < 0 or (not allow_zero and d == 0):
        raise ProcurementError(f"{name} must be {'positive' if not allow_zero else 'zero or more'}.", code="invalid_number", field=name)
    if d != d.quantize(TWO):
        raise ProcurementError(f"{name} can have at most 2 decimal places.", code="invalid_number", field=name)
    if d >= Decimal("1000000000000"):
        raise ProcurementError(f"{name} is too large.", code="invalid_number", field=name)
    return d.quantize(TWO)


def parse_qty(value, name: str = "quantity") -> Decimal:
    if value is None or value == "":
        raise ProcurementError(f"{name} is required.", code="required", field=name)
    if isinstance(value, float):
        value = repr(value)
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProcurementError(f"{name} must be a number.", code="invalid_number", field=name)
    if not d.is_finite() or d <= 0:
        raise ProcurementError(f"{name} must be greater than zero.", code="invalid_number", field=name)
    if d != d.quantize(THREE):
        raise ProcurementError(f"{name} can have at most 3 decimal places.", code="invalid_number", field=name)
    return d.quantize(THREE)


def parse_rate(value, name: str = "gst_rate") -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")
    d = parse_money(value, name)
    if d > Decimal("100"):
        raise ProcurementError(f"{name} cannot exceed 100.", code="invalid_number", field=name)
    return d


def parse_day(value, name: str, *, required: bool = False) -> date | None:
    if value in (None, ""):
        if required:
            raise ProcurementError(f"{name} is required.", code="required", field=name)
        return None
    if isinstance(value, date):
        return value
    d = parse_date(str(value))
    if d is None:
        raise ProcurementError(f"{name} must be a date (YYYY-MM-DD).", code="invalid_date", field=name)
    return d


def parse_int(value, name: str, *, required: bool = False) -> int | None:
    if value in (None, ""):
        if required:
            raise ProcurementError(f"{name} is required.", code="required", field=name)
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        raise ProcurementError(f"{name} must be an id.", code="invalid_id", field=name)


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def choice(value, allowed, name: str, *, default=None):
    if value in (None, ""):
        if default is not None:
            return default
        raise ProcurementError(f"{name} is required.", code="required", field=name)
    if value not in allowed:
        raise ProcurementError(f"{name} must be one of {', '.join(map(str, allowed))}.", code="invalid_choice", field=name)
    return value


def paginate(request, qs, serialize):
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except ValueError:
        page = 1
    try:
        size = min(MAX_PAGE_SIZE, max(1, int(request.query_params.get("page_size") or 50)))
    except ValueError:
        size = 50
    total = qs.count()
    rows = qs[(page - 1) * size : page * size]
    return Response({"count": total, "page": page, "page_size": size, "results": [serialize(r) for r in rows]})
