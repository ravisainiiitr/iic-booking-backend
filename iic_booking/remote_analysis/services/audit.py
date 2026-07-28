"""Audit / event logging for Remote Analysis."""

from __future__ import annotations

from typing import Any

from iic_booking.remote_analysis.models import AnalysisWorkstation, WorkstationEvent


def record_event(
    *,
    category: str,
    action: str,
    details: str = "",
    success: bool = True,
    workstation: AnalysisWorkstation | None = None,
    actor: Any = None,
    correlation_id: str = "",
) -> WorkstationEvent:
    return WorkstationEvent.objects.create(
        workstation=workstation,
        category=category,
        action=action,
        details=details or "",
        success=success,
        actor=actor if getattr(actor, "pk", None) else None,
        correlation_id=correlation_id or "",
    )
