"""Append-only audit trail. Unlike notifications, an audit write failure aborts the action: the module is
audit-first, so a change that cannot be recorded must not happen."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.db import models

from .models import ProcurementAuditLog


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, models.Model):
        return value.pk
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "hex") and type(value).__name__ == "UUID":
        return str(value)
    return value


def snapshot(obj, fields) -> dict:
    return {f: _jsonable(getattr(obj, f"{f}_id" if _is_fk(obj, f) else f, None)) for f in fields}


def _is_fk(obj, field_name: str) -> bool:
    try:
        field = obj._meta.get_field(field_name)
    except Exception:
        return False
    return isinstance(field, models.ForeignKey)


def _client_ip(request) -> str | None:
    if request is None:
        return None
    from iic_booking.users.mobile_sessions import client_ip

    return client_ip(request)


def record(
    actor,
    action: str,
    obj,
    *,
    department=None,
    old=None,
    new=None,
    reason: str = "",
    request=None,
) -> ProcurementAuditLog:
    dept = department if department is not None else getattr(obj, "department", None)
    ua = ""
    if request is not None:
        ua = (request.META.get("HTTP_USER_AGENT") or "")[:255]
    return ProcurementAuditLog.objects.create(
        department=dept,
        actor=actor if getattr(actor, "pk", None) else None,
        action=action,
        object_type=type(obj).__name__,
        object_id=str(getattr(obj, "pk", "") or ""),
        object_number=str(getattr(obj, "number", "") or getattr(obj, "code", "") or "")[:40],
        old_value=_jsonable(old or {}),
        new_value=_jsonable(new or {}),
        reason=reason or "",
        ip_address=_client_ip(request),
        user_agent=ua,
    )


def diff(before: dict, after: dict) -> tuple[dict, dict]:
    changed = [k for k in after if before.get(k) != after.get(k)]
    return {k: before.get(k) for k in changed}, {k: after.get(k) for k in changed}
