"""Workspace audit + telemetry helpers."""

from __future__ import annotations

from iic_booking.remote_analysis.constants import AuditCategory
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace, WorkspaceAudit, WorkspaceTelemetry


def audit_workspace(
    workspace: AnalysisWorkspace | None,
    action: str,
    *,
    details: str = "",
    actor=None,
    success: bool = True,
) -> WorkspaceAudit:
    row = WorkspaceAudit.objects.create(
        workspace=workspace,
        action=action,
        details=details,
        actor=actor if actor is not None and getattr(actor, "pk", None) else None,
        success=success,
    )
    record_event(
        category=AuditCategory.WORKSPACE,
        action=f"Workspace.{action}",
        details=details or (str(workspace.id) if workspace else ""),
        workstation=workspace.workstation if workspace else None,
        actor=actor if actor is not None and getattr(actor, "is_authenticated", False) else None,
        success=success,
        correlation_id=str(workspace.id) if workspace else "",
    )
    return row


def record_metric(
    name: str,
    value: float,
    *,
    unit: str = "",
    workspace: AnalysisWorkspace | None = None,
    tags: dict | None = None,
) -> WorkspaceTelemetry:
    return WorkspaceTelemetry.objects.create(
        metric_name=name,
        value=value,
        unit=unit,
        workspace=workspace,
        tags=tags or {},
    )
