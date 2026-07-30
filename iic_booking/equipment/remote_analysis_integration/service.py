"""Primary orchestration service — delegates to Remote Analysis reservation/session APIs."""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.remote_analysis_integration.audit import BookingAuditBridge
from iic_booking.equipment.remote_analysis_integration.eligibility import BookingAnalysisEligibilityService
from iic_booking.equipment.remote_analysis_integration.notifications import BookingNotificationBridge
from iic_booking.equipment.remote_analysis_integration.timeline import BookingTimelineIntegrationService
from iic_booking.equipment.remote_analysis_integration.workspace import BookingWorkspaceFacade
from iic_booking.remote_analysis.constants import NotificationType, ReservationStatus


TERMINAL_RESERVATION = {
    ReservationStatus.COMPLETED,
    ReservationStatus.EXPIRED,
    ReservationStatus.CANCELLED,
    ReservationStatus.FAILED,
}


class BookingRemoteAnalysisService:
    def __init__(self):
        self.eligibility = BookingAnalysisEligibilityService()
        self.workspace = BookingWorkspaceFacade()
        self.notifications = BookingNotificationBridge()
        self.audit = BookingAuditBridge()
        self.timeline = BookingTimelineIntegrationService()

    def get_summary(self, booking) -> dict:
        elig = self.eligibility.evaluate(booking)
        reservation = booking.analysis_reservation
        workspace = self.workspace.get_for_booking(booking)
        session = None
        if reservation:
            from iic_booking.remote_analysis.session_models import RemoteDesktopSession

            session = (
                RemoteDesktopSession.objects.filter(reservation=reservation).order_by("-created_at").first()
            )
        return {
            "eligibility": elig.as_dict(),
            "analysis_available": booking.analysis_available,
            "analysis_available_from": booking.analysis_available_from.isoformat()
            if booking.analysis_available_from
            else None,
            "analysis_expiry": booking.analysis_expiry.isoformat() if booking.analysis_expiry else None,
            "analysis_session_count": booking.analysis_session_count,
            "analysis_last_session": booking.analysis_last_session.isoformat()
            if booking.analysis_last_session
            else None,
            "reservation": self._serialize_reservation(reservation),
            "workspace": self._serialize_workspace(workspace),
            "session": self._serialize_session(session),
            "timeline": self.timeline.build(booking),
            "files": self.workspace.list_files(booking, limit=50),
        }

    @transaction.atomic
    def on_booking_completed(self, booking, *, actor=None) -> dict:
        """Evaluate eligibility and optionally create reservation (idempotent)."""
        elig = self.eligibility.evaluate(booking)
        self.audit.log(booking, "EligibilityEvaluation", details=elig.reason, actor=actor, success=elig.eligible)
        if not elig.eligible:
            booking.analysis_available = False
            booking.save(update_fields=["analysis_available", "updated_at"])
            return {"eligible": False, "reason": elig.reason}

        hours = int(getattr(booking.equipment, "analysis_access_duration", 72) or 72)
        now = timezone.now()
        booking.analysis_available = True
        booking.analysis_available_from = booking.analysis_available_from or now
        if not booking.analysis_expiry:
            booking.analysis_expiry = now + timedelta(hours=hours)
        booking.save(
            update_fields=[
                "analysis_available",
                "analysis_available_from",
                "analysis_expiry",
                "updated_at",
            ]
        )
        self.notifications.notify(
            booking.user,
            NotificationType.RESERVATION_CONFIRMED,
            "Analysis Available",
            f"Remote Analysis is available for booking {booking.booking_id}.",
            metadata={"booking_id": booking.booking_id},
        )
        reservation = self.ensure_reservation(booking, actor=actor)
        return {"eligible": True, "reservation_id": str(reservation.id) if reservation else None}

    @transaction.atomic
    def ensure_reservation(self, booking, *, actor=None, auto_allocate: bool = True):
        """Idempotent AnalysisReservation creation via ReservationService."""
        elig = self.eligibility.evaluate(booking)
        if not elig.eligible:
            raise ValueError(elig.reason)

        from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
        from iic_booking.remote_analysis.services.reservation import ReservationService
        from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService

        existing = (
            AnalysisReservation.objects.filter(booking=booking)
            .exclude(status__in=TERMINAL_RESERVATION)
            .order_by("-created_at")
            .first()
        )
        if existing:
            self._link_booking(booking, existing)
            return existing

        if booking.analysis_reservation_id and booking.analysis_reservation.status not in TERMINAL_RESERVATION:
            return booking.analysis_reservation

        svc = ReservationService()
        start = timezone.now()
        end = start + timedelta(hours=int(getattr(booking.equipment, "analysis_access_duration", 72) or 72))
        try:
            reservation = svc.create_reservation(
                user=booking.user,
                requested_start=start,
                requested_end=end,
                booking=booking,
                created_by=actor,
                auto_allocate=auto_allocate,
            )
        except ValueError as exc:
            # Race: another active reservation appeared
            existing = (
                AnalysisReservation.objects.filter(booking=booking)
                .exclude(status__in=TERMINAL_RESERVATION)
                .first()
            )
            if existing:
                self._link_booking(booking, existing)
                return existing
            raise

        self._link_booking(booking, reservation)
        try:
            workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=actor)
            booking.analysis_workspace = workspace
            booking.save(update_fields=["analysis_workspace", "updated_at"])
        except Exception:
            pass

        self.audit.log(booking, "ReservationCreated", details=str(reservation.id), actor=actor)
        self.notifications.notify(
            booking.user,
            NotificationType.SESSION_STARTING,
            "Desktop Ready",
            f"Analysis reservation ready for booking {booking.booking_id}.",
            metadata={"booking_id": booking.booking_id, "reservation_id": str(reservation.id)},
        )
        return reservation

    def launch_session(self, booking, *, user, client_ip: str | None = None):
        reservation = self.ensure_reservation(booking, actor=user)
        from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator

        session = SessionOrchestrator().create_session(
            reservation=reservation,
            user=user,
            client_ip=client_ip,
        )
        booking.analysis_session_count = int(booking.analysis_session_count or 0) + 1
        booking.analysis_last_session = timezone.now()
        booking.save(update_fields=["analysis_session_count", "analysis_last_session", "updated_at"])
        self.audit.log(booking, "Launch", details=str(session.id), actor=user)
        return session

    def archive_workspace(self, booking, *, actor=None):
        archive = self.workspace.archive(booking, actor=actor)
        booking.analysis_available = False
        booking.save(update_fields=["analysis_available", "updated_at"])
        self.audit.log(booking, "WorkspaceArchive", details=str(getattr(archive, "id", "")), actor=actor)
        self.notifications.notify(
            booking.user,
            NotificationType.WORKSPACE_SYNCED,
            "Workspace Archived",
            f"Analysis workspace archived for booking {booking.booking_id}.",
            metadata={"booking_id": booking.booking_id},
        )
        return archive

    def sync_from_reservation(self, reservation) -> None:
        booking = reservation.booking
        if not booking:
            return
        self._link_booking(booking, reservation)

    def _link_booking(self, booking, reservation) -> None:
        fields = ["analysis_reservation", "updated_at"]
        booking.analysis_reservation = reservation
        from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

        ws = AnalysisWorkspace.objects.filter(reservation=reservation).first()
        if ws:
            booking.analysis_workspace = ws
            fields.append("analysis_workspace")
        if not booking.analysis_available:
            booking.analysis_available = True
            fields.append("analysis_available")
        booking.save(update_fields=fields)

    def _serialize_reservation(self, reservation) -> dict | None:
        if not reservation:
            return None
        return {
            "id": str(reservation.id),
            "status": reservation.status,
            "workstation": getattr(reservation.workstation, "hostname", None),
            "requested_start": reservation.requested_start.isoformat() if reservation.requested_start else None,
            "requested_end": reservation.requested_end.isoformat() if reservation.requested_end else None,
        }

    def _serialize_workspace(self, workspace) -> dict | None:
        if not workspace:
            return None
        from iic_booking.remote_analysis.workspace_models import WorkspaceFile

        output_files = list(
            WorkspaceFile.objects.filter(
                workspace=workspace,
                deleted=False,
                is_current=True,
                relative_path__startswith="Processed/",
            ).values("id", "original_name", "relative_path", "size", "sha256")[:50]
        )
        return {
            "id": str(workspace.id),
            "status": workspace.status,
            "sync_phase": getattr(workspace, "sync_phase", None),
            "sync_progress_percent": getattr(workspace, "sync_progress_percent", 0),
            "sync_message": getattr(workspace, "sync_message", ""),
            "usage_bytes": workspace.current_usage_bytes,
            "quota_gb": workspace.quota_gb,
            "archived_at": workspace.archived_at.isoformat() if workspace.archived_at else None,
            "output_files": [
                {
                    "id": str(f["id"]),
                    "name": f["original_name"],
                    "relative_path": f["relative_path"],
                    "size": f["size"],
                    "sha256": f["sha256"],
                }
                for f in output_files
            ],
        }

    def _serialize_session(self, session) -> dict | None:
        if not session:
            return None
        return {
            "id": str(session.id),
            "status": session.status,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "launch_time": session.launch_time.isoformat() if getattr(session, "launch_time", None) else None,
        }
