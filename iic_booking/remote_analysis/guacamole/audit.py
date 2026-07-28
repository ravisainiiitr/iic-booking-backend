"""Session audit helpers."""

from __future__ import annotations

from iic_booking.remote_analysis.constants import AuditCategory
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.session_models import RemoteDesktopSession, SessionAudit


def audit_session(
    session: RemoteDesktopSession | None,
    action: str,
    *,
    details: str = "",
    actor=None,
    success: bool = True,
) -> SessionAudit:
    row = SessionAudit.objects.create(
        session=session,
        action=action,
        details=details,
        actor=actor if actor is not None and getattr(actor, "pk", None) else None,
        success=success,
    )
    record_event(
        category=AuditCategory.SESSION,
        action=action,
        details=details or (str(session.id) if session else ""),
        workstation=session.workstation if session else None,
        actor=actor if actor is not None and getattr(actor, "is_authenticated", False) else None,
        success=success,
        correlation_id=str(session.id) if session else "",
    )
    return row
