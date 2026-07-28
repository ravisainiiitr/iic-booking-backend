"""Report bridge — attach RA metrics onto booking report payloads."""

from __future__ import annotations


class BookingReportBridge:
    def enrich_booking(self, booking) -> dict:
        workspace = None
        try:
            from iic_booking.equipment.remote_analysis_integration.workspace import BookingWorkspaceFacade

            workspace = BookingWorkspaceFacade().get_for_booking(booking)
        except Exception:
            pass
        reservation = booking.analysis_reservation
        uploads = downloads = 0
        duration_seconds = 0
        if workspace:
            from iic_booking.remote_analysis.workspace_models import WorkspaceTransfer
            from iic_booking.remote_analysis.constants import TransferDirection

            transfers = WorkspaceTransfer.objects.filter(workspace=workspace)
            for t in transfers:
                direction = getattr(t, "direction", "") or ""
                if "PORTAL_TO" in direction or "AGENT_PUSH" in direction:
                    uploads += 1
                else:
                    downloads += 1
        if reservation:
            from iic_booking.remote_analysis.session_models import RemoteDesktopSession, SessionStatistics

            for s in RemoteDesktopSession.objects.filter(reservation=reservation):
                stats = getattr(s, "statistics", None)
                if stats is None:
                    stats = SessionStatistics.objects.filter(session=s).first()
                if stats and getattr(stats, "duration_seconds", None):
                    duration_seconds += int(stats.duration_seconds or 0)
        return {
            "analysis_sessions": booking.analysis_session_count,
            "workspace_size_bytes": getattr(workspace, "current_usage_bytes", 0) if workspace else 0,
            "remote_analysis_duration_seconds": duration_seconds,
            "downloads": downloads,
            "uploads": uploads,
            "workstation_used": getattr(getattr(reservation, "workstation", None), "hostname", None),
            "reservation_status": getattr(reservation, "status", None),
        }
