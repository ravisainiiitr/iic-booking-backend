"""Agent workspace synchronization orchestration (Portal side)."""

from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    AGENT_INPUT_PORTAL_FOLDERS,
    AGENT_LAYOUT_FOLDERS,
    AGENT_OUTPUT_PORTAL_FOLDERS,
    PORTAL_TO_AGENT_FOLDER,
    WORKSPACE_INPUT_READY_PHASES,
    WORKSPACE_UPLOAD_VERIFIED_PHASES,
    CommandType,
    NotificationType,
    TransferDirection,
    TransferStatus,
    WorkspaceStatus,
    WorkspaceSyncPhase,
    normalize_sync_phase,
)
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

    def ensure_for_reservation(self, reservation, *, actor=None, ingest: bool = True) -> AnalysisWorkspace:
        workspace = self.storage.create_for_reservation(reservation, actor=actor)
        if workspace.workstation_id is None and getattr(reservation, "workstation_id", None):
            workspace.workstation = reservation.workstation
            workspace.save(update_fields=["workstation", "updated_at"])
        if ingest and (workspace.booking_id or getattr(reservation, "booking_id", None)):
            if not workspace.booking_id and reservation.booking_id:
                workspace.booking = reservation.booking
                workspace.save(update_fields=["booking", "updated_at"])
            try:
                from iic_booking.remote_analysis.workspace.booking_ingest import BookingResultIngestService

                BookingResultIngestService().ingest(workspace, actor=actor)
            except Exception:
                logger.exception("Booking result ingest failed for workspace %s", workspace.id)
        return workspace

    def set_sync_phase(
        self,
        workspace: AnalysisWorkspace,
        phase: str,
        *,
        percent: int | None = None,
        message: str = "",
        actor=None,
    ) -> None:
        phase = normalize_sync_phase(phase)
        previous = workspace.sync_phase
        workspace.sync_phase = phase
        if percent is not None:
            workspace.sync_progress_percent = min(100, max(0, int(percent)))
        if message:
            workspace.sync_message = message[:512]
        update_fields = ["sync_phase", "sync_progress_percent", "sync_message", "updated_at"]
        workspace.save(update_fields=update_fields)
        if previous != phase:
            audit_workspace(
                workspace,
                "SYNC",
                details=f"lifecycle {previous} → {phase}" + (f": {message}" if message else ""),
                actor=actor,
                success=phase
                not in {
                    WorkspaceSyncPhase.PREPARATION_FAILED,
                    WorkspaceSyncPhase.UPLOAD_FAILED,
                    WorkspaceSyncPhase.CLEANUP_FAILED,
                    WorkspaceSyncPhase.CANCELLED,
                },
            )

    def build_manifest(
        self,
        workspace: AnalysisWorkspace,
        *,
        scope: str = "input",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Manifest-driven sync catalog (sha256 + size). scope: input | output | all."""
        files = list(
            WorkspaceFile.objects.filter(workspace=workspace, deleted=False, is_current=True).order_by(
                "relative_path"
            )
        )
        settings = self.settings
        booking_id = ""
        if workspace.booking_id:
            booking_id = str(workspace.booking_id)
        elif getattr(workspace.reservation, "booking_id", None):
            booking_id = str(workspace.reservation.booking_id)

        def _include(f: WorkspaceFile) -> bool:
            top = f.relative_path.split("/", 1)[0]
            if scope == "input":
                return top in AGENT_INPUT_PORTAL_FOLDERS or f.category in {"RAW", "METADATA"}
            if scope == "output":
                return top in AGENT_OUTPUT_PORTAL_FOLDERS or f.category in {
                    "PROCESSED",
                    "REPORT",
                    "EXPORT",
                    "LOG",
                }
            return True

        selected = [f for f in files if _include(f)]
        return {
            "bookingId": booking_id,
            "booking_id": booking_id,
            "sessionId": session_id or str(workspace.reservation_id),
            "session_id": session_id or str(workspace.reservation_id),
            "workspaceId": str(workspace.id),
            "workspace_id": str(workspace.id),
            "reservation_id": str(workspace.reservation_id),
            "storage_key": workspace.storage_key,
            "local_agent_path": workspace.local_agent_path,
            "status": workspace.status,
            "sync_phase": workspace.sync_phase,
            "read_only": workspace.read_only,
            "scope": scope,
            "agent_layout": list(AGENT_LAYOUT_FOLDERS),
            "portal_to_agent_folder": dict(PORTAL_TO_AGENT_FOLDER),
            "download_folders": list(AGENT_INPUT_PORTAL_FOLDERS),
            "upload_folders": list(AGENT_OUTPUT_PORTAL_FOLDERS),
            "transfer_max_retries": int(getattr(settings, "transfer_max_retries", 3) or 3),
            "compression_enabled": bool(getattr(settings, "compression_enabled", False)),
            "compression_min_bytes": int(getattr(settings, "compression_min_bytes", 0) or 0),
            "bandwidth_limit_kbps": int(getattr(settings, "bandwidth_limit_kbps", 0) or 0),
            "files": [
                {
                    "id": str(f.id),
                    "relativePath": f.relative_path,
                    "relative_path": f.relative_path,
                    "agent_relative_path": self._agent_relative_path(f.relative_path),
                    "size": f.size,
                    "sha256": f.sha256,
                    "lastModifiedUtc": (f.modified_at or f.uploaded_at).isoformat()
                    if (f.modified_at or f.uploaded_at)
                    else "",
                    "last_modified_utc": (f.modified_at or f.uploaded_at).isoformat()
                    if (f.modified_at or f.uploaded_at)
                    else "",
                    "status": "Ready",
                    "version": f.version,
                    "category": f.category,
                }
                for f in selected
            ],
        }

    @staticmethod
    def _agent_relative_path(portal_relative: str) -> str:
        parts = portal_relative.replace("\\", "/").split("/", 1)
        folder = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        agent_folder = PORTAL_TO_AGENT_FOLDER.get(folder, "Input")
        return f"{agent_folder}/{rest}" if rest else agent_folder

    def prepare_payload(self, workspace: AnalysisWorkspace, *, session_id: str = "") -> dict[str, Any]:
        """Payload embedded in PREPARE_WORKSTATION so agent creates layout + pulls Input."""
        return {
            "session_id": session_id or str(workspace.reservation_id),
            "workspace_id": str(workspace.id),
            "reservation_id": str(workspace.reservation_id),
            "local_path": workspace.local_agent_path,
            "download_folders": list(AGENT_INPUT_PORTAL_FOLDERS),
            "agent_layout": list(AGENT_LAYOUT_FOLDERS),
            "manifest": self.build_manifest(workspace, scope="input", session_id=session_id),
            "sync_action": "download_input",
            "transfer_max_retries": int(getattr(self.settings, "transfer_max_retries", 3) or 3),
            "compression_enabled": bool(getattr(self.settings, "compression_enabled", False)),
            "compression_min_bytes": int(getattr(self.settings, "compression_min_bytes", 0) or 0),
            "bandwidth_limit_kbps": int(getattr(self.settings, "bandwidth_limit_kbps", 0) or 0),
        }

    def issue_sync_command(self, workspace: AnalysisWorkspace, *, actor=None) -> Any:
        if not workspace.workstation_id:
            raise ValueError("Workspace has no workstation for sync")
        workspace.status = WorkspaceStatus.SYNCING
        workspace.save(update_fields=["status", "updated_at"])
        self.set_sync_phase(
            workspace,
            WorkspaceSyncPhase.DOWNLOADING_INPUT,
            percent=20,
            message="Downloading input to workstation",
            actor=actor,
        )
        payload = {
            "workspace_id": str(workspace.id),
            "reservation_id": str(workspace.reservation_id),
            "local_path": workspace.local_agent_path,
            "session_id": str(workspace.reservation_id),
            "manifest": self.build_manifest(workspace, scope="input"),
            "sync_action": "download_input",
            "agent_layout": list(AGENT_LAYOUT_FOLDERS),
            "transfer_max_retries": int(getattr(self.settings, "transfer_max_retries", 3) or 3),
        }
        cmd = CommandService().create_command(
            workspace.workstation,
            CommandType.SYNC_WORKSPACE,
            payload=payload,
            created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
        )
        WorkspaceTransfer.objects.create(
            workspace=workspace,
            direction=TransferDirection.AGENT_PULL,
            status=TransferStatus.IN_PROGRESS,
            started_at=timezone.now(),
            created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
        )
        audit_workspace(workspace, "SYNC", details=f"command={cmd.id}", actor=actor)
        self._notify(workspace, NotificationType.WORKSPACE_SYNC_STARTED, "Synchronization Started", "Input download started.")
        return cmd

    def issue_collect_command(self, workspace: AnalysisWorkspace, *, actor=None) -> Any:
        if not workspace.workstation_id:
            return None
        workspace.status = WorkspaceStatus.COLLECTING
        workspace.save(update_fields=["status", "updated_at"])
        self.set_sync_phase(
            workspace,
            WorkspaceSyncPhase.COLLECTING_OUTPUT,
            percent=55,
            message="Collecting output from workstation",
            actor=actor,
        )
        self.set_sync_phase(
            workspace,
            WorkspaceSyncPhase.UPLOADING_OUTPUT,
            percent=60,
            message="Uploading results to portal",
            actor=actor,
        )
        payload = {
            "workspace_id": str(workspace.id),
            "reservation_id": str(workspace.reservation_id),
            "local_path": workspace.local_agent_path,
            "session_id": str(workspace.reservation_id),
            "upload_folders": list(AGENT_LAYOUT_FOLDERS),
            "upload_agent_folders": ["Output", "Logs"],
            "portal_folder_map": {"Output": "Processed", "Logs": "Logs"},
            "sync_action": "upload_output",
            "manifest": self.build_manifest(workspace, scope="output"),
            "transfer_max_retries": int(getattr(self.settings, "transfer_max_retries", 3) or 3),
            "compression_enabled": bool(getattr(self.settings, "compression_enabled", False)),
            "compression_min_bytes": int(getattr(self.settings, "compression_min_bytes", 0) or 0),
            "bandwidth_limit_kbps": int(getattr(self.settings, "bandwidth_limit_kbps", 0) or 0),
        }
        cmd = CommandService().create_command(
            workspace.workstation,
            CommandType.COLLECT_WORKSPACE,
            payload=payload,
            created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
        )
        WorkspaceTransfer.objects.create(
            workspace=workspace,
            direction=TransferDirection.AGENT_PUSH,
            status=TransferStatus.IN_PROGRESS,
            started_at=timezone.now(),
            created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
        )
        audit_workspace(workspace, "SYNC", details=f"collect={cmd.id}", actor=actor)
        return cmd

    def mark_prepared(self, workspace: AnalysisWorkspace, *, success: bool = True, message: str = "") -> None:
        """Called after agent finishes Input download + checksum verify (prepare/sync)."""
        if success:
            self.set_sync_phase(
                workspace,
                WorkspaceSyncPhase.VERIFYING_INPUT,
                percent=45,
                message="Verifying input checksums",
            )
            workspace.status = WorkspaceStatus.READY
            workspace.last_synced_at = timezone.now()
            workspace.save(update_fields=["status", "last_synced_at", "updated_at"])
            self.set_sync_phase(
                workspace,
                WorkspaceSyncPhase.INPUT_READY,
                percent=50,
                message=message or "Input ready",
            )
            self._notify(workspace, NotificationType.WORKSPACE_READY, "Workspace Ready", "Analysis files are ready on the workstation.")
        else:
            workspace.status = WorkspaceStatus.FAILED
            workspace.save(update_fields=["status", "updated_at"])
            self.set_sync_phase(
                workspace,
                WorkspaceSyncPhase.PREPARATION_FAILED,
                percent=0,
                message=message or "Preparation failed",
            )
            self._notify(
                workspace,
                NotificationType.WORKSPACE_SYNC_FAILED,
                "Synchronization Failed",
                message or "Workspace preparation failed.",
            )

    def mark_session_starting(self, workspace: AnalysisWorkspace) -> None:
        if workspace.sync_phase == WorkspaceSyncPhase.INPUT_READY:
            self.set_sync_phase(
                workspace, WorkspaceSyncPhase.SESSION_STARTING, percent=55, message="Starting remote session"
            )

    def mark_session_active(self, workspace: AnalysisWorkspace) -> None:
        if workspace.sync_phase in {
            WorkspaceSyncPhase.INPUT_READY,
            WorkspaceSyncPhase.SESSION_STARTING,
        }:
            workspace.status = WorkspaceStatus.ACTIVE
            workspace.save(update_fields=["status", "updated_at"])
            self.set_sync_phase(
                workspace, WorkspaceSyncPhase.SESSION_ACTIVE, percent=58, message="Session active"
            )

    def mark_synced(self, workspace: AnalysisWorkspace, *, success: bool = True, message: str = "") -> None:
        """Finalize SYNC/COLLECT command (not per-file upload)."""
        workspace.last_synced_at = timezone.now()
        collecting = workspace.status == WorkspaceStatus.COLLECTING
        syncing = workspace.status == WorkspaceStatus.SYNCING
        if success:
            if collecting:
                workspace.status = WorkspaceStatus.ACTIVE
                workspace.upload_verified_at = timezone.now()
                workspace.save(update_fields=["last_synced_at", "status", "upload_verified_at", "updated_at"])
                self.set_sync_phase(
                    workspace,
                    WorkspaceSyncPhase.UPLOAD_VERIFIED,
                    percent=90,
                    message=message or "Upload verified",
                )
                self._notify(
                    workspace,
                    NotificationType.FILES_AVAILABLE,
                    "Files Available",
                    "Processed analysis files are available for download.",
                )
                self._issue_verified_cleanup(workspace)
                workspace.refresh_from_db()
                if workspace.sync_phase != WorkspaceSyncPhase.CLEANUP_FAILED:
                    self.set_sync_phase(
                        workspace,
                        WorkspaceSyncPhase.COMPLETED,
                        percent=100,
                        message=message or "Synchronization completed",
                    )
            elif syncing:
                self.mark_prepared(workspace, success=True, message=message)
                return
            else:
                workspace.status = (
                    WorkspaceStatus.ACTIVE
                    if workspace.status
                    in {
                        WorkspaceStatus.READY,
                        WorkspaceStatus.ACTIVE,
                    }
                    else workspace.status
                )
                workspace.save(update_fields=["last_synced_at", "status", "updated_at"])
        else:
            workspace.status = WorkspaceStatus.FAILED
            workspace.save(update_fields=["last_synced_at", "status", "updated_at"])
            phase = WorkspaceSyncPhase.UPLOAD_FAILED if collecting else WorkspaceSyncPhase.PREPARATION_FAILED
            if collecting:
                self.set_sync_phase(
                    workspace, WorkspaceSyncPhase.RETRY_PENDING, percent=workspace.sync_progress_percent, message=message
                )
            else:
                self.set_sync_phase(workspace, phase, percent=workspace.sync_progress_percent, message=message)
            self._notify(
                workspace,
                NotificationType.WORKSPACE_SYNC_FAILED,
                "Synchronization Failed",
                message or "Transfer failed.",
            )

        WorkspaceTransfer.objects.create(
            workspace=workspace,
            direction=TransferDirection.AGENT_PUSH if collecting else TransferDirection.AGENT_PULL,
            status=TransferStatus.COMPLETED if success else TransferStatus.FAILED,
            error_message=(message or "")[:2000],
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        if success:
            try:
                from iic_booking.remote_analysis.collaboration.hooks import on_workspace_synced

                on_workspace_synced(workspace)
            except Exception:
                pass
            try:
                from iic_booking.remote_analysis.notifications import NotificationEngine

                NotificationEngine().notify(
                    workspace.user,
                    NotificationType.WORKSPACE_SYNCED,
                    "Synchronization Completed",
                    message or "Workspace synchronized.",
                    metadata={"workspace_id": str(workspace.id)},
                )
            except Exception:
                pass

    def _issue_verified_cleanup(self, workspace: AnalysisWorkspace, *, actor=None) -> None:
        """After UploadVerified, allow agent to delete Output (portal already has files)."""
        if not workspace.workstation_id:
            return
        self.set_sync_phase(workspace, WorkspaceSyncPhase.CLEANUP, percent=95, message="Cleaning workstation")
        try:
            CommandService().create_command(
                workspace.workstation,
                CommandType.CLEAN_WORKSTATION,
                payload={
                    "session_id": str(workspace.reservation_id),
                    "workspace_id": str(workspace.id),
                    "local_path": workspace.local_agent_path,
                    "reason": "upload_verified",
                    "defer_output_cleanup": False,
                    "delete_folders": ["Input", "Working", "Output", "Logs", "Temp"],
                },
                created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
            )
        except Exception:
            logger.exception("Verified cleanup command failed for workspace %s", workspace.id)
            self.set_sync_phase(
                workspace,
                WorkspaceSyncPhase.CLEANUP_FAILED,
                percent=90,
                message="Cleanup command failed after upload verify",
            )

    def retry_failed_transfers(self, workspace: AnalysisWorkspace, *, actor=None) -> Any:
        """Re-issue collect (or sync) after failure — keeps Output until success."""
        self.set_sync_phase(
            workspace, WorkspaceSyncPhase.RETRY_PENDING, percent=55, message="Retrying transfer", actor=actor
        )
        if workspace.status in {WorkspaceStatus.COLLECTING, WorkspaceStatus.FAILED, WorkspaceStatus.ACTIVE}:
            if workspace.workstation_id:
                return self.issue_collect_command(workspace, actor=actor)
        if workspace.workstation_id:
            return self.issue_sync_command(workspace, actor=actor)
        raise ValueError("Workspace has no workstation")

    def has_failed_collect(self, workspace: AnalysisWorkspace) -> bool:
        last = (
            workspace.transfers.filter(direction=TransferDirection.AGENT_PUSH)
            .order_by("-created_at")
            .first()
        )
        return bool(last and last.status in {TransferStatus.FAILED, TransferStatus.RETRYING})

    def defer_output_cleanup(self, workspace: AnalysisWorkspace) -> bool:
        """Never delete Output until upload is verified on the portal."""
        phase = normalize_sync_phase(workspace.sync_phase)
        if phase in WORKSPACE_UPLOAD_VERIFIED_PHASES or workspace.upload_verified_at:
            return False
        return True

    def is_input_ready(self, workspace: AnalysisWorkspace) -> bool:
        return normalize_sync_phase(workspace.sync_phase) in WORKSPACE_INPUT_READY_PHASES

    @staticmethod
    def _notify(workspace: AnalysisWorkspace, ntype: str, title: str, body: str) -> None:
        try:
            from iic_booking.remote_analysis.notifications import NotificationEngine

            NotificationEngine().notify(
                workspace.user,
                ntype,
                title,
                body,
                metadata={"workspace_id": str(workspace.id)},
            )
        except Exception:
            logger.debug("Workspace notify skipped", exc_info=True)
