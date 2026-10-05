"""Append-only audit trail for department module switches (same conventions as the procurement audit)."""

from __future__ import annotations

from .models import DepartmentModuleAuditLog


def _client_ip(request) -> str | None:
    if request is None:
        return None
    from iic_booking.users.mobile_sessions import client_ip

    return client_ip(request)


def record(actor, department, module_key: str, action: str, *, old=None, new=None, reason: str = "", request=None):
    ua = ""
    if request is not None:
        ua = (request.META.get("HTTP_USER_AGENT") or "")[:255]
    return DepartmentModuleAuditLog.objects.create(
        department=department,
        department_label=(getattr(department, "code", None) or getattr(department, "name", "") or "")[:255],
        module_key=module_key,
        actor=actor if getattr(actor, "pk", None) else None,
        action=action,
        old_value=old or {},
        new_value=new or {},
        reason=reason or "",
        ip_address=_client_ip(request),
        user_agent=ua,
    )
