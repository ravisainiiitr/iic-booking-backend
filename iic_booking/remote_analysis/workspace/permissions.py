"""Workspace access permissions."""

from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace, WorkspaceShare


def _is_manager(user) -> bool:
    return CanManageRemoteAnalysis().has_permission(
        type("R", (), {"user": user, "method": "GET"})(), None
    )


def can_access_workspace(user, workspace: AnalysisWorkspace) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if workspace.user_id == getattr(user, "pk", None):
        return True
    if _is_manager(user):
        return True

    now = timezone.now()
    active = Q(revoked_at__isnull=True) & (Q(expires_at__isnull=True) | Q(expires_at__gte=now))
    if WorkspaceShare.objects.filter(active, workspace=workspace, shared_with=user).exists():
        return True
    dept_id = getattr(user, "department_id", None)
    if dept_id and workspace.department_id == dept_id:
        if WorkspaceShare.objects.filter(active, workspace=workspace, department_only=True).exists():
            return True
    return False


def can_write_workspace(user, workspace: AnalysisWorkspace) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if workspace.read_only and not _is_manager(user):
        return False
    if workspace.user_id == getattr(user, "pk", None):
        return True
    return _is_manager(user)
