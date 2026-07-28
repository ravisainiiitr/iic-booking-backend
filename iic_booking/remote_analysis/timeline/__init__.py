"""Session timeline builder — reservation → archive lifecycle."""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from iic_booking.remote_analysis.collaboration_models import CollaborationTelemetry
from iic_booking.remote_analysis.constants import AuditCategory
from iic_booking.remote_analysis.services.audit import record_event


class TimelineService:
    def build_for_session(self, session) -> dict[str, Any]:
        events: list[dict[str, Any]] = []

        def add(ts, stage: str, detail: str = "", source: str = ""):
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

        reservation = session.reservation
        add(reservation.created_at if reservation else None, "Reservation", f"status={getattr(reservation, 'status', '')}", "reservation")
        workspace = getattr(reservation, "workspace", None) if reservation else None
        if workspace is None and reservation:
            from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

            workspace = AnalysisWorkspace.objects.filter(reservation=reservation).first()
        if workspace:
            add(workspace.created_at, "Workspace", f"status={workspace.status}", "workspace")
            add(workspace.last_synced_at, "Synchronization", "Workspace synced", "workspace")
            add(workspace.archived_at, "Archive", f"archive={workspace.archive_status}", "workspace")

        add(session.created_at, "SessionCreated", f"status={session.status}", "session")
        for h in session.state_history.all().order_by("created_at")[:50]:
            add(h.created_at, h.to_status, h.reason or f"{h.from_status}->{h.to_status}", "state_history")
        add(session.launch_time, "Launch", "Browser launch", "session")
        add(session.connected_at, "Connection", "Remote desktop connected", "session")

        if workspace is not None:
            for t in workspace.transfers.order_by("created_at")[:30]:
                direction = getattr(t, "direction", "") or ""
                stage = "Uploads" if "PORTAL_TO" in direction or "AGENT_PUSH" in direction else "Downloads"
                add(t.created_at, stage, f"{direction} {t.status}", "transfer")

        if session.cleanup_command_id:
            add(session.cleanup_command.created_at, "Cleanup", session.cleanup_command.command_type, "command")
        add(session.disconnected_at, "SessionEnd", session.termination_reason or session.status, "session")

        events.sort(key=lambda e: e["timestamp"])
        CollaborationTelemetry.objects.create(metric_name="timeline_generation", value=1.0)
        record_event(
            category=AuditCategory.COLLABORATION,
            action="TimelineGenerated",
            details=str(session.id),
            success=True,
            correlation_id=str(session.id),
        )
        return {
            "session_id": str(session.id),
            "generated_at": timezone.now().isoformat(),
            "events": events,
        }

    def build_for_reservation(self, reservation) -> dict[str, Any]:
        from iic_booking.remote_analysis.session_models import RemoteDesktopSession

        session = RemoteDesktopSession.objects.filter(reservation=reservation).order_by("-created_at").first()
        if session:
            return self.build_for_session(session)
        events = [
            {
                "timestamp": reservation.created_at.isoformat(),
                "stage": "Reservation",
                "detail": reservation.status,
                "source": "reservation",
            }
        ]
        return {"reservation_id": str(reservation.id), "generated_at": timezone.now().isoformat(), "events": events}
