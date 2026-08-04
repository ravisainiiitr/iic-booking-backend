"""Session cleanup: Guacamole teardown, CLEAN_WORKSTATION, archive stats."""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    AuditCategory,
    CommandType,
    ReservationStatus,
    SessionStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.guacamole.audit import audit_session
from iic_booking.remote_analysis.guacamole.connection import ConnectionManager
from iic_booking.remote_analysis.models import WorkstationStateHistory
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.session_models import (
    RemoteDesktopSession,
    SessionStatistics,
    SessionTermination,
    SessionToken,
)

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    SessionStatus.COMPLETED,
    SessionStatus.EXPIRED,
    SessionStatus.FAILED,
    SessionStatus.TERMINATED,
}


class SessionCleanupService:
    def cleanup(
        self,
        session: RemoteDesktopSession,
        *,
        reason: str = "",
        actor=None,
        final_status: str = SessionStatus.TERMINATED,
        release_reservation: bool = True,
    ) -> RemoteDesktopSession:
        if session.status in TERMINAL_STATUSES and getattr(session, "termination", None):
            return session

        guac_ok = False
        cleanup_cmd_ok = False
        released = False

        try:
            ConnectionManager().destroy(session)
            guac_ok = True
        except Exception:
            logger.exception("Guacamole destroy failed for session %s", session.id)

        SessionToken.objects.filter(session=session, consumed_at__isnull=True, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )

        # Milestone 5+: collect outputs; never delete Output until UploadVerified
        workspace_id = ""
        local_path = ""
        defer_output = True
        try:
            from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
            from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

            ws_obj = AnalysisWorkspace.objects.filter(reservation_id=session.reservation_id).first()
            if ws_obj:
                workspace_id = str(ws_obj.id)
                local_path = ws_obj.local_agent_path
                sync_svc = WorkspaceSyncService()
                try:
                    sync_svc.issue_collect_command(ws_obj, actor=actor)
                except Exception:
                    logger.exception("COLLECT_WORKSPACE failed for session %s", session.id)
                ws_obj.refresh_from_db()
                # Always defer Output at session end; verified cleanup runs after COLLECT succeeds.
                defer_output = sync_svc.defer_output_cleanup(ws_obj)
                if not defer_output:
                    try:
                        from iic_booking.remote_analysis.workspace.storage import StorageManager

                        StorageManager().archive(ws_obj, actor=actor, note=f"Session end: {reason}"[:500])
                    except Exception:
                        logger.exception("Workspace archive failed for session %s", session.id)
        except Exception:
            logger.exception("Workspace cleanup hook failed for session %s", session.id)
            defer_output = True

        try:
            cmd = CommandService().create_command(
                session.workstation,
                CommandType.CLEAN_WORKSTATION,
                payload={
                    "session_id": str(session.id),
                    "reason": reason,
                    "workspace_id": workspace_id,
                    "local_path": local_path,
                    "defer_output_cleanup": defer_output,
                    # Always strip Input/Working/Temp; retain Output (+ Logs until verified)
                    "delete_folders": ["Input", "Working", "Temp"]
                    if defer_output
                    else ["Input", "Working", "Output", "Temp", "Logs"],
                },
                created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
            )
            session.cleanup_command = cmd
            cleanup_cmd_ok = True
        except Exception:
            logger.exception("CLEAN_WORKSTATION failed to queue for session %s", session.id)

        ws = session.workstation
        try:
            if ws.status not in {WorkstationStatus.DISABLED, WorkstationStatus.MAINTENANCE}:
                WorkstationStateHistory.objects.create(
                    workstation=ws,
                    from_status=ws.status,
                    to_status=WorkstationStatus.AVAILABLE,
                    reason=f"Session cleanup: {reason}"[:500],
                )
                ws.status = WorkstationStatus.AVAILABLE
                ws.save(update_fields=["status", "updated_at"])
        except Exception:
            logger.exception("Workstation release failed for session %s", session.id)

        if release_reservation and session.reservation_id:
            try:
                reservation = session.reservation
                if reservation.status in {
                    ReservationStatus.ACTIVE,
                    ReservationStatus.READY,
                    ReservationStatus.PREPARING,
                    ReservationStatus.RESERVED,
                }:
                    from iic_booking.remote_analysis.scheduler_models import ReservationHistory

                    ReservationHistory.objects.create(
                        reservation=reservation,
                        from_status=reservation.status,
                        to_status=ReservationStatus.COMPLETED,
                        reason=f"Session ended: {reason}"[:500],
                        changed_by=actor if actor is not None and getattr(actor, "pk", None) else None,
                    )
                    reservation.status = ReservationStatus.COMPLETED
                    reservation.released_at = timezone.now()
                    reservation.save(update_fields=["status", "released_at", "updated_at"])
                    released = True
            except Exception:
                logger.exception("Reservation release failed for session %s", session.id)

        now = timezone.now()
        try:
            if not session.disconnected_at:
                session.disconnected_at = now
            session.termination_reason = reason[:512]
            session.status = final_status
            session.save(
                update_fields=[
                    "status",
                    "termination_reason",
                    "disconnected_at",
                    "cleanup_command",
                    "updated_at",
                ]
            )
        except Exception:
            logger.exception("Session terminal save failed for session %s", session.id)

        duration = 0.0
        if session.connected_at:
            duration = (now - session.connected_at).total_seconds()
        elif session.launch_time:
            duration = (now - session.launch_time).total_seconds()

        try:
            SessionStatistics.objects.update_or_create(
                session=session,
                defaults={
                    "duration_seconds": max(0.0, duration),
                    "reconnect_count": session.reconnect_count,
                },
            )
        except Exception:
            logger.exception("SessionStatistics update failed for session %s", session.id)

        try:
            SessionTermination.objects.update_or_create(
                session=session,
                defaults={
                    "reason": reason[:512],
                    "terminated_by": actor if actor is not None and getattr(actor, "pk", None) else None,
                    "cleanup_completed": cleanup_cmd_ok,
                    "guacamole_destroyed": guac_ok,
                    "reservation_released": released,
                },
            )
        except Exception:
            logger.exception("SessionTermination update failed for session %s", session.id)

        try:
            audit_session(session, "Cleanup", details=reason, actor=actor, success=guac_ok and cleanup_cmd_ok)
            record_event(
                category=AuditCategory.GUACAMOLE,
                action="SessionCleanup",
                details=reason,
                workstation=session.workstation,
                actor=actor if actor is not None and getattr(actor, "is_authenticated", False) else None,
                correlation_id=str(session.id),
            )
        except Exception:
            logger.exception("Cleanup audit failed for session %s", session.id)
        return session

    @transaction.atomic
    def cleanup_expired(self) -> int:
        now = timezone.now()
        qs = RemoteDesktopSession.objects.select_for_update().filter(
            expires_at__lt=now,
            status__in=[
                SessionStatus.CREATED,
                SessionStatus.PREPARING,
                SessionStatus.READY,
                SessionStatus.TOKEN_GENERATED,
                SessionStatus.LAUNCHED,
                SessionStatus.CONNECTING,
                SessionStatus.CONNECTED,
                SessionStatus.ACTIVE,
                SessionStatus.IDLE,
            ],
        )
        count = 0
        for session in qs:
            self.cleanup(session, reason="Session expired", final_status=SessionStatus.EXPIRED)
            count += 1
        return count

    @transaction.atomic
    def cleanup_idle(self) -> int:
        now = timezone.now()
        count = 0
        qs = RemoteDesktopSession.objects.select_for_update().filter(
            status__in=[SessionStatus.ACTIVE, SessionStatus.CONNECTED, SessionStatus.IDLE],
        )
        for session in qs:
            idle_minutes = session.idle_timeout_minutes or 15
            anchor = session.last_activity_at or session.connected_at or session.launch_time
            if not anchor:
                continue
            idle_for = (now - anchor).total_seconds() / 60.0
            if idle_for >= idle_minutes:
                self.cleanup(session, reason="Idle timeout", final_status=SessionStatus.TERMINATED)
                count += 1
            elif idle_for >= max(1, idle_minutes - 1) and session.status != SessionStatus.IDLE:
                from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator

                SessionOrchestrator().transition(session, SessionStatus.IDLE, reason="Approaching idle timeout")
        return count
