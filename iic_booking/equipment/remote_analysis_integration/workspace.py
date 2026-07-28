"""Workspace facade — thin wrappers over Remote Analysis workspace services."""

from __future__ import annotations


class BookingWorkspaceFacade:
    def get_for_booking(self, booking):
        if booking.analysis_workspace_id:
            return booking.analysis_workspace
        from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

        if booking.analysis_reservation_id:
            ws = AnalysisWorkspace.objects.filter(reservation_id=booking.analysis_reservation_id).first()
            if ws:
                return ws
        return AnalysisWorkspace.objects.filter(booking=booking).order_by("-created_at").first()

    def list_files(self, booking, *, limit: int = 100) -> list:
        workspace = self.get_for_booking(booking)
        if not workspace:
            return []
        from iic_booking.remote_analysis.workspace_models import WorkspaceFile

        rows = (
            WorkspaceFile.objects.filter(workspace=workspace, deleted=False, is_current=True)
            .order_by("relative_path")[:limit]
        )
        return [
            {
                "id": str(f.id),
                "name": f.original_name or f.relative_path,
                "relative_path": f.relative_path,
                "size": f.size,
                "updated_at": f.modified_at.isoformat() if getattr(f, "modified_at", None) else None,
            }
            for f in rows
        ]

    def archive(self, booking, *, actor=None):
        workspace = self.get_for_booking(booking)
        if not workspace:
            raise ValueError("No analysis workspace linked to this booking")
        from iic_booking.remote_analysis.workspace.storage import StorageManager

        return StorageManager().archive(workspace, actor=actor, note="Archived via booking integration")
