"""Timeline merge — booking events + remote analysis session timeline."""

from __future__ import annotations


class BookingTimelineIntegrationService:
    def build(self, booking) -> list[dict]:
        events: list[dict] = []

        def add(ts, stage: str, detail: str = "", source: str = "booking"):
            if ts is None:
                return
            events.append(
                {
                    "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    "stage": stage,
                    "detail": detail,
                    "source": source,
                }
            )

        add(booking.created_at, "BookingCreated", booking.status)
        add(booking.completed_at, "ExperimentCompleted", "Booking completed")
        add(booking.analysis_available_from, "RemoteAnalysisAvailable", "Analysis available")

        if booking.analysis_reservation_id:
            res = booking.analysis_reservation
            add(res.created_at, "WorkstationAllocated", f"reservation={res.status}", "reservation")
            add(res.allocated_at, "ReservationAllocated", getattr(res.workstation, "hostname", ""), "reservation")

        workspace = None
        try:
            from iic_booking.equipment.remote_analysis_integration.workspace import BookingWorkspaceFacade

            workspace = BookingWorkspaceFacade().get_for_booking(booking)
        except Exception:
            workspace = None
        if workspace:
            add(workspace.created_at, "WorkspaceCreated", workspace.status, "workspace")
            add(workspace.last_synced_at, "WorkspaceUpdated", "Synchronized", "workspace")
            add(workspace.archived_at, "WorkspaceArchived", workspace.archive_status, "workspace")

        try:
            from iic_booking.remote_analysis.session_models import RemoteDesktopSession
            from iic_booking.remote_analysis.timeline import TimelineService

            session = None
            if booking.analysis_reservation_id:
                session = (
                    RemoteDesktopSession.objects.filter(reservation_id=booking.analysis_reservation_id)
                    .order_by("-created_at")
                    .first()
                )
            if session:
                tl = TimelineService().build_for_session(session)
                for e in tl.get("events") or []:
                    events.append({**e, "source": e.get("source") or "session"})
        except Exception:
            pass

        events.sort(key=lambda e: e.get("timestamp") or "")
        return events
