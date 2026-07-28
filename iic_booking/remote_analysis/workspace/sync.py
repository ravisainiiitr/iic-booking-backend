"""Agent workspace synchronization orchestration (Portal side)."""

from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

from iic_booking.remote_analysis.constants import CommandType, TransferDirection, TransferStatus, WorkspaceStatus
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.remote_analysis.workspace.audit import audit_workspace
from iic_booking.remote_analysis.workspace.storage import StorageManager
from iic_booking.remote_analysis.workspace_models import (
    AnalysisWorkspace,
    WorkspaceFile,
    WorkspaceTransfer,
)

logger = logging.getLogger(__name__)


class WorkspaceSyncService:
    """
    Portal-side sync: assign workspace to agent, expose manifest, issue SYNC/COLLECT commands.
    Agent never receives another reservation's workspace.
    """

    def __init__(self, settings_obj: RemoteAnalysisSettings | None = None):
        self.settings = settings_obj or RemoteAnalysisSettings.get_solo()
        self.storage = StorageManager(self.settings)

    def ensure_for_reservation(self, reservation, *, actor=None) -> AnalysisWorkspace:
        return self.storage.create_for_reservation(reservation, actor=actor)

    def build_manifest(self, workspace: AnalysisWorkspace) -> dict[str, Any]:
        files = list(
            WorkspaceFile.objects.filter(workspace=workspace, deleted=False, is_current=True).order_by("relative_path")
        )
        return {
            "workspace_id": str(workspace.id),
            "reservation_id": str(workspace.reservation_id),
            "storage_key": workspace.storage_key,
            "local_agent_path": workspace.local_agent_path,
            "status": workspace.status,
            "read_only": workspace.read_only,
            "files": [
                {
                    "id": str(f.id),
                    "relative_path": f.relative_path,
                    "size": f.size,
                    "sha256": f.sha256,
                    "version": f.version,
                    "category": f.category,
                }
                for f in files
            ],
        }

    def issue_sync_command(self, workspace: AnalysisWorkspace, *, actor=None) -> Any:
        if not workspace.workstation_id:
            raise ValueError("Workspace has no workstation for sync")
        workspace.status = WorkspaceStatus.SYNCING
        workspace.save(update_fields=["status", "updated_at"])
        payload = {
            "workspace_id": str(workspace.id),
            "reservation_id": str(workspace.reservation_id),
            "local_path": workspace.local_agent_path,
            "manifest": self.build_manifest(workspace),
        }
        cmd = CommandService().create_command(
            workspace.workstation,
            CommandType.SYNC_WORKSPACE,
            payload=payload,
            created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
        )
        audit_workspace(workspace, "SYNC", details=f"command={cmd.id}", actor=actor)
        return cmd

    def issue_collect_command(self, workspace: AnalysisWorkspace, *, actor=None) -> Any:
        if not workspace.workstation_id:
            return None
        workspace.status = WorkspaceStatus.COLLECTING
        workspace.save(update_fields=["status", "updated_at"])
        payload = {
            "workspace_id": str(workspace.id),
            "reservation_id": str(workspace.reservation_id),
            "local_path": workspace.local_agent_path,
            "upload_folders": ["Processed", "Reports", "Exports", "Logs"],
        }
        cmd = CommandService().create_command(
            workspace.workstation,
            CommandType.COLLECT_WORKSPACE,
            payload=payload,
            created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
        )
        audit_workspace(workspace, "SYNC", details=f"collect={cmd.id}", actor=actor)
        return cmd

    def mark_synced(self, workspace: AnalysisWorkspace, *, success: bool = True, message: str = "") -> None:
        workspace.last_synced_at = timezone.now()
        if success:
            workspace.status = WorkspaceStatus.ACTIVE if workspace.status in {
                WorkspaceStatus.SYNCING,
                WorkspaceStatus.READY,
                WorkspaceStatus.COLLECTING,
            } else workspace.status
            if workspace.status == WorkspaceStatus.COLLECTING:
                workspace.status = WorkspaceStatus.ACTIVE
        else:
            workspace.status = WorkspaceStatus.FAILED
        workspace.save(update_fields=["last_synced_at", "status", "updated_at"])
        WorkspaceTransfer.objects.create(
            workspace=workspace,
            direction=TransferDirection.AGENT_PULL,
            status=TransferStatus.COMPLETED if success else TransferStatus.FAILED,
            error_message=message[:2000],
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        if success:
            try:
                from iic_booking.remote_analysis.collaboration.hooks import on_workspace_synced

                on_workspace_synced(workspace)
            except Exception:
                pass
