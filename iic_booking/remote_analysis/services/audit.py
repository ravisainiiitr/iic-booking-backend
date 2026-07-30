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
    if not correlation_id:
        try:
            from iic_booking.remote_analysis.operations.commissioning_observability import (
                active_run_id_for_workstation,
                get_commissioning_run_id,
            )

            correlation_id = get_commissioning_run_id() or active_run_id_for_workstation(workstation) or ""
        except Exception:  # noqa: BLE001
            correlation_id = ""
    if correlation_id:
        tag = f"[commissioning_run={correlation_id}]"
        if tag not in (details or ""):
            details = f"{tag} {details}" if details else tag
    return WorkstationEvent.objects.create(
        workstation=workstation,
        category=category,
        action=action,
        details=details or "",
        success=success,
        actor=actor if getattr(actor, "pk", None) else None,
        correlation_id=correlation_id or "",
    )
